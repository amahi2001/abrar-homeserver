"""Repair the two TokenEye receipt fields affected by the Buildfleet home rename."""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from token_eye_update import Deferred, Updater, atomic, command, locked


CA = ".token-eye-headroom-ca.pem"
KEY = "NODE_EXTRA_CA_CERTS"
KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", KEY}
HOST = "/agent/host-claude"
BOX = "/agent/claude"
BOX_DATA = "/agent/token-eye/data"


# Run in the exact container selected from this project's locked metadata.
BOX_CHECK = """import json, os, sys
from pathlib import Path
def unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate key")
    return value
scope = json.loads(os.environ["TOKEN_EYE_CLAUDE_ACCOUNTS"], object_pairs_hook=unique)
expected_scope = [{"root": k, "clientRoot": v} for k, v in json.loads(sys.argv[3]).items()]
if (len(scope) != len(expected_scope) or any(account not in scope for account in expected_scope)
        or os.environ.get("TOKEN_EYE_DATA_DIR") != "/agent/token-eye/data"):
    raise SystemExit(1)
for path, expected in json.loads(sys.argv[1]).items():
    info = os.stat(path)
    if [info.st_dev, info.st_ino] != expected:
        raise SystemExit(1)
if sys.argv[2] == "offline":
    for name in ("tcp", "tcp6"):
        for row in Path("/proc/net", name).read_text().splitlines()[1:]:
            _, local, _, state, *_ = row.split()
            if int(local.split(":")[1], 16) in (8787, 8788) and state not in ("06", "07"):
                raise SystemExit(1)
"""


def require(condition):
    if not condition:
        raise ValueError("unexpected_state")


def unique_fields(pairs):
    fields = dict(pairs)
    require(len(fields) == len(pairs))
    return fields


def invalid_constant(value):
    raise ValueError("invalid JSON number")


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique_fields, parse_constant=invalid_constant)


def check_box(container, home, data, offline):
    files = {}
    for mounted, local, names in ((HOST, home / ".claude", ("settings.json", CA)),
                                  (BOX, home / ".local/state/dev/accounts/claude", ("settings.json", CA)),
                                  (BOX_DATA, data, ("claude-routing-owner.json",))):
        for name in ("", *names):
            info = (local / name).stat()
            files[str(Path(mounted) / name)] = [info.st_dev, info.st_ino]
    command("docker", "exec", "--workdir", "/", container, "/usr/bin/python3", "-IS", "-c",
            BOX_CHECK, json.dumps(files),
            "offline" if offline else "check",
            json.dumps({HOST: str(home / ".claude"), BOX: BOX}), timeout=10)


def contents(path, limit, private=False):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & (0o077 if private else 0o022) and info.st_size <= limit)
        value = stream.read(limit + 1)
        require(len(value) <= limit and os.path.samestat(info, path.lstat()))
        return value


def proposal(raw, home, legacy):
    old, new = legacy / ".claude", home / ".claude"
    box = home / ".local/state/dev/accounts/claude"
    require(home == home.resolve() and old.samefile(new) and legacy.samefile(home))
    require(new == new.resolve() and box == box.resolve())
    require(all(d.stat().st_uid == os.getuid() and not d.stat().st_mode & 0o022
                for d in (home, new, box)))
    require((old / CA).samefile(new / CA))
    record = decode(raw)
    require(isinstance(record, dict) and set(record) == {"version", "enabled", "accounts"}
            and type(record["version"]) is int and record["version"] == 1
            and record["enabled"] is True and isinstance(record["accounts"], list))
    for entry in record["accounts"]:
        require(isinstance(entry, dict)
                and set(entry) == {"account", "env_existed", "previous", "installed", "certificate_hash"}
                and type(entry["env_existed"]) is bool
                and isinstance(entry["account"], dict) and set(entry["account"]) == {"root", "clientRoot"}
                and isinstance(entry["installed"], dict) and isinstance(entry["previous"], dict))
    entries = {o["account"]["root"]: o for o in record["accounts"]}
    require(len(record["accounts"]) == 2 and set(entries) == {BOX, HOST})
    for name, root in ((BOX, box), (HOST, new)):
        entry = entries[name]
        installed = entry["installed"]
        require(set(installed) == KEYS and set(entry["previous"]) == KEYS)
        require(all(isinstance(v, str) for v in installed.values()))
        settings = decode(contents(root / "settings.json", 1 << 20))["env"]
        require(isinstance(settings, dict))
        actual = {k: settings.get(k) for k in KEYS}
        if name == HOST:
            require(entry["account"]["clientRoot"] in (str(old), str(new)))
            require(installed[KEY] == str(Path(entry["account"]["clientRoot"]) / CA))
            require(actual == {**installed, KEY: str(new / CA)})
        else:
            require(entry["account"]["clientRoot"] == BOX and actual == installed)
            require(installed[KEY] == str(Path(BOX) / CA))
        certificate = contents(root / CA, 1 << 20)
        require(hashlib.sha256(certificate).hexdigest() == entry["certificate_hash"])
    host = entries[HOST]
    if host["account"]["clientRoot"] == str(new):
        return raw
    host["account"]["clientRoot"] = str(new)
    host["installed"][KEY] = str(new / CA)
    return json.dumps(record, indent=2, ensure_ascii=False).encode()


def reconcile(mode, updater, legacy, deployment_lock):
    home = updater.home
    data = updater.project / "home/.local/share/com.amahi.tokeneye.desktop"
    owner = data / "claude-routing-owner.json"
    backup = updater.state / "host-ca-home-rename.preimage"
    for directory in (data, updater.state):
        info = directory.lstat()
        require(directory.resolve() == directory and stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid() and not info.st_mode & 0o077)
    with ExitStack() as locks:
        if mode != "check":
            for path, shared in ((updater.state / ".lock", False), (deployment_lock, True),
                                 (updater.project / "startup.lock", False),
                                 (updater.project / "home/.agent-session.lock", False),
                                 (data / "headroom-proxy/.operations.lock", False),
                                 (data / "server-forward/.operations.lock", False),
                                 (data / ".operations.lock", False)):
                require(path.parent.resolve() == path.parent and path.is_file())
                locks.enter_context(locked(path, shared))
        container = updater.box_ready()
        # Native owner locks prevent a new supervised listener during a mutation.
        check_box(container, home, data, mode != "check")
        require(not os.path.lexists(updater.pending)
                and not os.path.lexists(data / "claude-routing-pending.json"))
        routing = decode(contents(data / "routing-owner.json", 16 << 20, private=True))
        require(routing["applying"] is False)
        raw = contents(owner, 16 << 20, private=True)
        candidate = proposal(raw, home, legacy)
        backup_exists = os.path.lexists(backup)
        if raw != candidate and backup_exists:
            require(contents(backup, 16 << 20, private=True) == raw)
        if mode == "check":
            return "current" if raw == candidate else "eligible"
        if mode == "apply":
            if raw == candidate:
                return "current"
            replacement = candidate
        else:
            preimage = contents(backup, 16 << 20, private=True)
            if raw == preimage:
                return "already_restored"
            require(proposal(preimage, home, legacy) == raw and raw == candidate)
            replacement = preimage
        def unchanged():
            require(proposal(raw, home, legacy) == candidate)
            require(contents(owner, 16 << 20, private=True) == raw)

        def before_replace():
            check_box(container, home, data, True)
            unchanged()
            if mode == "apply" and not backup_exists:
                atomic(backup, raw)
                created = backup.lstat()
                try:
                    unchanged()
                except Exception:
                    if (os.path.samestat(created, backup.lstat())
                            and contents(backup, 16 << 20, private=True) == raw):
                        backup.unlink()
                    raise

        # Stage and sync the replacement before the final checks and rename.
        atomic(owner, replacement, before_replace=before_replace)
        return "applied" if mode == "apply" else "restored"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("check", "apply", "rollback"), default="check")
    try:
        print(reconcile(parser.parse_args().mode, Updater(), Path("/home/buildfleet"),
                        Path("/run/lock/host-deployment.lock")))
    except Deferred:
        raise SystemExit("deferred_busy") from None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        # File contents and native output may contain account credentials.
        raise SystemExit("deferred_or_changed_state") from None
