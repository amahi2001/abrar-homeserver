"""Manage the upstream user installation while systemd owns the foreground worker."""
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

HOME = Path.home()
DATA = HOME / ".claude-mem"
CACHE = HOME / ".claude/plugins/cache/thedotmack/claude-mem"
RECEIPT = DATA / "nixos-version"
PENDING = DATA / "nixos-candidate"
ACTIVE = Path("/run/claude-mem/version")
PLUGINS = HOME / ".claude/plugins"
BOOTSTRAP = "16.1.5"
PROMOTED = "https://raw.githubusercontent.com/thedotmack/claude-mem/main/releases/promoted-release.json"


def version(text):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text):
        raise ValueError("Expected a stable Claude Mem version")
    return tuple(map(int, text.split(".")))


def write_atomic(path, text):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
    os.replace(tmp.name, path)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def settings_target(settings):
    env = settings.get("env")
    if isinstance(env, dict) and any(k.startswith("CLAUDE_MEM_") for k in env):
        return env
    return settings


def check_location(target):
    location = os.environ.get("CLAUDE_MEM_DATA_DIR") or target.get("CLAUDE_MEM_DATA_DIR")
    if location and Path(location).expanduser().resolve() != DATA.resolve():
        raise ValueError(f"This host manages Claude Mem data in {DATA}; found a different data directory")


def configure():
    path = DATA / "settings.json"
    settings = read_json(path)
    target = settings_target(settings)
    check_location(target)
    key = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "gateway-key").read_text().strip()
    if not key:
        raise ValueError("Empty 9Router key")
    target.update({
        "CLAUDE_MEM_PROVIDER": "openai-compatible",
        "CLAUDE_MEM_OPENAI_COMPAT_BASE_URL": "http://127.0.0.1:20128/v1",
        "CLAUDE_MEM_OPENAI_COMPAT_MODEL": "claude-mem",
        "CLAUDE_MEM_OPENAI_COMPAT_API_KEY": key,
        "CLAUDE_MEM_QUOTA_FALLBACK_PROVIDER": "off",
        "CLAUDE_MEM_PROVIDER_BY_SOURCE": "",
        "CLAUDE_MEM_WORK_VIEW_AGENT": "api",
        "CLAUDE_MEM_CORPUS_AGENT": "api",
        "CLAUDE_MEM_WORK_VIEW_API_MODEL": "claude-mem",
        "CLAUDE_MEM_WORK_VIEW_COPY_PROVIDER": "openai-compatible",
        "CLAUDE_MEM_WORK_VIEW_COPY_MODEL": "claude-mem",
        "CLAUDE_MEM_WORKER_URL": "",
        "CLAUDE_MEM_WORKER_HOST": "127.0.0.1",
        "CLAUDE_MEM_WORKER_PORT": "37777",
        "CLAUDE_MEM_WORKER_AUTOSTART": "false",
        "CLAUDE_MEM_AUTO_UPDATE": "false",
    })
    token = target.get("CLAUDE_MEM_LOCAL_API_TOKEN")
    if not isinstance(token, str) or not token.strip():
        target["CLAUDE_MEM_LOCAL_API_TOKEN"] = secrets.token_urlsafe(32)
    write_atomic(path, json.dumps(settings, indent=2) + "\n")


def registered():
    registry = read_json(PLUGINS / "installed_plugins.json")
    entries = registry.get("plugins", {}).get("claude-mem@thedotmack", [])
    return next((e["version"] for e in entries if e.get("scope") == "user"), None)


def worker_cached(release):
    cache = CACHE / release
    return (cache / "scripts/worker-service.cjs").is_file() and (cache / "node_modules").is_dir()


def complete(release):
    codex = HOME / ".codex/plugins/cache/claude-mem-local/claude-mem" / release
    if (registered() != release or not worker_cached(release)
            or not (codex / ".codex-plugin/plugin.json").is_file()):
        return False
    hooks = read_json(HOME / ".codex/hooks.json").get("hooks", {})
    for event, action in (("UserPromptSubmit", "session-init"), ("PostToolUse", "observation")):
        if not any(f"hook codex {action}" in h.get("command", "")
                   for group in hooks.get(event, []) for h in group.get("hooks", [])):
            return False
    launcher = shutil.which("claude-mem")
    if not launcher or Path(launcher).resolve() != (HOME / ".local/bin/claude-mem").resolve():
        return False
    result = subprocess.run([launcher, "--version"], text=True, capture_output=True, timeout=10)
    return result.returncode == 0 and result.stdout.strip().isdigit()


def install(release):
    version(release)
    registry = "https://registry.npmjs.org"
    with urlopen(f"{registry}/claude-mem/{release}", timeout=30) as response:
        package = json.load(response)
    provenance = package.get("dist", {}).get("attestations", {}).get("provenance", {})
    if (package.get("version") != release or not package.get("gitHead")
            or provenance.get("predicateType") != "https://slsa.dev/provenance/v1"):
        raise RuntimeError("Claude Mem release is missing npm publication metadata")
    # --ide codex-cli also registers and enables the shared Claude Code plugin.
    with tempfile.TemporaryDirectory(prefix="claude-mem-installer-") as work:
        cli = ["bunx", "--registry", registry, "claude-mem@" + release]
        subprocess.run([*cli, "install", "--ide", "codex-cli", "--runtime", "worker",
                        "--no-auto-start"], check=True, cwd=work)
    if not complete(release):
        raise RuntimeError("Incomplete Claude Mem client installation")


def worker_ready(release):
    token = settings_target(read_json(DATA / "settings.json"))["CLAUDE_MEM_LOCAL_API_TOKEN"].strip()
    def get(path):
        req = Request("http://127.0.0.1:37777/api/" + path, headers={"Authorization": "Bearer " + token})
        with urlopen(req, timeout=3) as resp:
            return json.load(resp)
    for _ in range(60):
        try:
            health = get("health")
            if health["pid"] != int(os.environ["MAINPID"]):
                raise RuntimeError("Another worker owns port 37777; stop it before activating the service")
            if health["version"] == release and get("readiness")["status"] == "ready":
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("Claude Mem failed its authenticated version/readiness check")


def current_version():
    release = RECEIPT.read_text().strip() if RECEIPT.exists() else registered() or BOOTSTRAP
    version(release)
    return release


def eligible(candidate, current, pending):
    return (RECEIPT.exists() and version(candidate) > version(current)
            and version(candidate) >= version(pending.get("version", current)))


def snapshot_database():
    db = DATA / "claude-mem.db"
    if db.is_file():
        backup = DATA / "nixos-pre-upgrade.db.tmp"
        with closing(sqlite3.connect(db)) as src, closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
        os.replace(backup, DATA / "nixos-pre-upgrade.db")


def main(command):
    os.umask(0o077)
    os.environ["PATH"] += f":{HOME}/.local/bin:/run/current-system/sw/bin"
    DATA.mkdir(mode=0o700, exist_ok=True)
    # prepare pins this service start so concurrent staging cannot change it.
    if command in ("run", "ready"):
        release = ACTIVE.read_text().strip()
        version(release)
        if command == "run":
            worker = CACHE / release / "scripts/worker-service.cjs"
            os.execvp("bun", ["bun", str(worker), "--daemon"])
        worker_ready(release)

    repair = DATA / "nixos-client-repair"
    with (DATA / "nixos-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = read_json(PENDING)
        current = current_version()
        if command == "prepare":
            release = pending.get("version", current)
            version(release)
            configure()
            # Staging validates both clients before publishing a candidate. A
            # partial later installer may change their registry while the
            # previously verified worker is still cached and bootable offline.
            cache = CACHE / release
            saved = CACHE / (".nixos-repair-" + release)
            if saved.exists():  # An interrupted repair left the usable worker here.
                if cache.exists():
                    shutil.rmtree(cache)
                saved.rename(cache)
            if repair.is_file() and repair.read_text() == release:
                repair.unlink()
                if worker_cached(release):
                    cache.rename(saved)
                install(release)
                if saved.exists():
                    # Retire the old cache atomically before cleaning it up.
                    with tempfile.TemporaryDirectory(dir=CACHE) as trash:
                        saved.rename(Path(trash) / "old")
            elif not worker_cached(release) or (not RECEIPT.exists() and not complete(release)):
                install(release)
            if read_json(DATA / "telemetry.json").get("enabled") is not False:
                subprocess.run(["node", PLUGINS / "marketplaces/thedotmack/dist/npx-cli/index.js",
                                "telemetry", "disable"], check=True)
            # ExecStartPre runs with the previous worker stopped. Preserve the
            # last good snapshot through retries and superseding candidates.
            if pending and pending["snapshot_from"] != current:
                snapshot_database()
                write_atomic(PENDING, json.dumps({**pending, "snapshot_from": current}))
            write_atomic(ACTIVE, release)
        elif command == "ready":
            if release != ACTIVE.read_text().strip():
                raise RuntimeError("Worker changed during readiness check")
            write_atomic(RECEIPT, release + "\n")
            if pending.get("version") == release:
                PENDING.unlink()
        elif command == "stage":
            with urlopen(PROMOTED, timeout=30) as response:
                promotion = json.load(response)
            if promotion["schemaVersion"] != 1:
                raise ValueError("Unknown promotion document")
            candidate = promotion["promotedVersion"]
            check_location(settings_target(read_json(DATA / "settings.json")))
            if not eligible(candidate, current, pending):
                if RECEIPT.exists() and not pending and not complete(current):
                    # The installer replaces the same-version cache. Repair it
                    # on a stopped worker and preserve its offline boot copy.
                    write_atomic(repair, current)
                    return 0
                return 1  # systemd ExecCondition: no upgrade
            if pending.get("version") == candidate and ACTIVE.exists() and ACTIVE.read_text() == candidate:
                return 1  # systemd already launched or is launching this candidate
            if pending.get("version") != candidate:
                install(candidate)
                write_atomic(PENDING, json.dumps({
                    "version": candidate, "snapshot_from": pending.get("snapshot_from"),
                }))
        else:
            raise ValueError("Unknown operation")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception:
        # 255 distinguishes a failed version check from a skipped ExecCondition.
        import traceback
        traceback.print_exc()
        sys.exit(255)
