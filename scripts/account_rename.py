#!/usr/bin/env python3
"""One-time, reversible buildfleet -> amahi migration, controlled by root."""

import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import time

import account_rename_dev as development
import account_rename_samba as samba


OLD, NEW = "buildfleet", "amahi"
OLD_HOME, NEW_HOME = Path("/home/buildfleet"), Path("/home/amahi")
STATE = Path("/var/lib/nixos-account-rename")
PRIVATE = STATE / "private"
MANIFEST = PRIVATE / "manifest.json"
SCRIPT = STATE / "code/account_rename.py"
PYTHON = Path("/run/current-system/sw/bin/python3")
PAUSED_TIMERS = (
    "codex-remote-control.timer", "codex-app-server-refresh.timer",
    "dev-runtime-maintenance.timer", "critical-services-recovery.timer",
    "nixos-flake-update.timer", "codex-cli-update.timer", "hermes-flake-update.timer",
    "github-actions-maintenance.timer", "docker-prune.timer", "nix-gc.timer",
)
ACCOUNT_FILES = (
    "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow",
    "/etc/subuid", "/etc/subgid", "/var/lib/nixos/uid-map",
    "/var/lib/nixos/gid-map", "/var/lib/nixos/declarative-users",
    "/var/lib/nixos/declarative-groups", "/var/lib/nixos/auto-subuid-map",
    "/var/lib/systemd/linger/buildfleet", "/var/lib/systemd/linger/amahi",
    "/var/lib/AccountsService/users/buildfleet", "/var/lib/AccountsService/users/amahi",
)
CRITICAL = (
    "sshd", "tailscaled", "nginx", "docker", "docker-adguardhome",
    "docker-gluetun", "docker-transmission-vpn", "vaultwarden", "jellyfin",
    "prowlarr", "flaresolverr", "postgresql", "phpfpm-nextcloud", "redis-nextcloud",
    "samba-smbd", "samba-nmbd", "samba-wsdd", "hermes-agent", "hermes-dashboard",
    "shopping-research-mcp",
)


def run(args, *, check=True, timeout=180, **kwargs):
    result = subprocess.run(list(map(str, args)), text=True, capture_output=True,
                            timeout=timeout, **kwargs)
    if check and result.returncode:
        # Commands may read private files; never echo their output or arguments.
        raise RuntimeError(f"{Path(str(args[0])).name} failed with status {result.returncode}")
    return result


def atomic(path, content, *, mode=None, owner=None):
    path = Path(path)
    previous = path.stat() if path.exists() and not path.is_symlink() else None
    temporary = path.with_name(path.name + ".account-rename.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode if mode is not None else stat.S_IMODE(previous.st_mode) if previous else 0o600)
        uid, gid = owner if owner else (previous.st_uid, previous.st_gid) if previous else (0, 0)
        os.chown(temporary, uid, gid)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save(state, phase=None):
    if phase:
        state["phase"] = phase
    state["updated_at"] = time.time()
    atomic(MANIFEST, json.dumps(state, indent=2) + "\n", mode=0o600)
    public = {key: state[key] for key in ("phase", "updated_at", "target", "previous") if key in state}
    if "error" in state:
        public["error"] = state["error"]
    atomic(STATE / "status.json", json.dumps(public, indent=2) + "\n", mode=0o644)
    print("Account migration:", state["phase"], flush=True)


def load():
    return json.loads(MANIFEST.read_text())


def present(path):
    return Path(path).exists() or Path(path).is_symlink()


def backup(path, destination):
    path, destination = Path(path), Path(destination)
    if not present(path):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run(["cp", "-a", "--preserve=all", "--", path, destination])
    return True


def restore(path, saved, existed):
    path, saved = Path(path), Path(saved)
    if not existed:
        if path.is_file() or path.is_symlink():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".account-rename.restore")
    if present(temporary):
        raise RuntimeError("Unexpected restore temporary path")
    run(["cp", "-a", "--preserve=all", "--", saved, temporary])
    temporary.replace(path)


def shadow_tail(username):
    for line in Path("/etc/shadow").read_text().splitlines():
        name, _, tail = line.partition(":")
        if name == username:
            return tail
    raise RuntimeError("Account shadow entry missing")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def foreign_session_processes(proc_root=Path("/proc")):
    """A sudo-launched app can retain the old user's session despite its UID."""
    found = []
    ancestors = set()
    pid = os.getpid()
    while pid:
        ancestors.add(pid)
        try:
            parent = next(line for line in (proc_root / str(pid) / "status").read_text().splitlines()
                          if line.startswith("PPid:"))
            pid = int(parent.split()[1])
        except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration):
            break
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cgroup = (entry / "cgroup").read_text()
            if "/user-1000.slice/" not in cgroup:
                continue
            uid_line = next(line for line in (entry / "status").read_text().splitlines()
                            if line.startswith("Uid:"))
            uids = list(map(int, uid_line.split()[1:4]))
            if int(entry.name) in ancestors or uids == [1000, 1000, 1000]:
                continue
            if uids == [0, 0, 0] and (entry / "comm").read_text().strip() == "sshd-session":
                continue
            found.append({"pid": int(entry.name), "uid": uids[1]})
        except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration):
            continue
    return found


def preflight(old_home=OLD_HOME, new_home=NEW_HOME):
    account = pwd.getpwnam(OLD)
    if (account.pw_uid, account.pw_gid, account.pw_dir) != (1000, 100, str(old_home)):
        raise RuntimeError("Original account identity changed")
    try:
        pwd.getpwnam(NEW)
    except KeyError:
        pass
    else:
        raise RuntimeError("Destination account already exists")
    if present(new_home) or old_home.is_symlink():
        raise RuntimeError("Destination home exists or original home is a symlink")
    if old_home.stat().st_dev != new_home.parent.stat().st_dev:
        raise RuntimeError("Home move would cross filesystems")
    if foreign_session_processes():
        raise RuntimeError("Foreign-UID applications depend on the old login session; isolate them before cutover")
    return account


def user_command(username, args, *, check=True, timeout=180):
    return run(["runuser", "--login", username, "--command", shlex.join(list(map(str, args)))],
               check=check, timeout=timeout)


def runtime_paths(home):
    relatives = {
        ".codex/config.toml", ".claude/settings.json", ".claude.json", ".nix-profile",
        ".local/bin/claude", ".bashrc", ".bash_profile", ".profile", ".zshrc",
        ".gitconfig", ".config/git/config", ".config/fish/config.fish",
        ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml",
    }
    relatives.update(str(path.relative_to(home)) for path in (home / ".local/share/applications").glob("*.desktop"))
    return sorted(relative for relative in relatives if present(home / relative))


def rebase_config(text, relative):
    # Preserve old project trust entries for existing Codex chats as well.
    legacy = []
    if relative == ".codex/config.toml":
        block = []
        for line in text.splitlines(keepends=True) + ["[end]\n"]:
            if line.lstrip().startswith("["):
                if block:
                    legacy.append("".join(block))
                    block = []
                if line.startswith('[projects."/home/buildfleet'):
                    block = [line]
            elif block:
                block.append(line)
    rebased = text.replace(str(OLD_HOME), str(NEW_HOME))
    if legacy:
        rebased += "\n# Legacy paths remain valid through the home compatibility link.\n" + "\n".join(legacy)
    return rebased


def storage_state():
    return {target: run(["findmnt", "--json", "--target", target,
                         "--output", "TARGET,SOURCE,FSTYPE"]).stdout.strip()
            for target in ("/srv/data", "/srv/storage")}


def history_state(home):
    for database in sorted((home / ".codex").glob("state_*.sqlite")):
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if {"cwd", "rollout_path"}.issubset(columns):
                count = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
                rows = connection.execute("SELECT cwd, rollout_path FROM threads WHERE cwd = ? OR cwd LIKE ?",
                                          (str(OLD_HOME), str(OLD_HOME) + "/%"))
                row = next((row for row in rows if Path(row[0]).is_dir() and Path(row[1]).is_file()), None)
                return {"database": str(database.relative_to(home)), "count": count,
                        "sample": list(row) if row else None}
    raise RuntimeError("Existing Codex history database not found")


def application_health(state):
    source = Path(state["previous_source"]) / "scripts/system-update.py"
    spec = importlib.util.spec_from_file_location("_rename_existing_health", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def checked(args):
        output = run(args).stdout
        if args[-1] == "https://cloud.amahi.dev/status.php":
            status = json.loads(output)
            if status.get("maintenance") != state["nextcloud_maintenance"]:
                raise RuntimeError("Nextcloud maintenance state changed")
            # The existing outage predates this migration; preserve it explicitly.
            if state["nextcloud_maintenance"]:
                status["maintenance"] = False
                output = json.dumps(status)
        return output
    module.health_check(checked)
    if storage_state() != state["storage"]:
        raise RuntimeError("Storage mounts changed")


def failed_units():
    return sorted(row["unit"] for row in json.loads(run([
        "systemctl", "list-units", "--state=failed", "--all", "--no-pager", "--output=json",
    ]).stdout))


def prepare(target):
    preflight()
    if MANIFEST.exists():
        state = load()
        if state["phase"] == "prepared" and state["target"] == str(target):
            return
        raise RuntimeError("Existing migration needs inspection")
    target = Path(target).resolve(strict=True)
    if target.parent != Path("/nix/store") or not (target / "bin/switch-to-configuration").is_file():
        raise RuntimeError("Target is not a built NixOS generation")
    state = {"phase": "preparing", "target": str(target),
             "previous": str(Path("/run/current-system").resolve()),
             "previous_source": str(Path("/etc/nixos-deployed").resolve()),
             "groups": sorted(os.getgrouplist(OLD, 100)),
             "shadow_digest": hashlib.sha256(shadow_tail(OLD).encode()).hexdigest(),
             "timers": [], "account_files": {}, "runtime_files": {}, "credentials": {}}
    for unit in CRITICAL:
        run(["systemctl", "is-active", "--quiet", unit + ".service"])
    state["storage"] = storage_state()
    state["failed_units"] = failed_units()
    nextcloud = json.loads(run(["curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                               "--resolve", "cloud.amahi.dev:443:127.0.0.1",
                               "https://cloud.amahi.dev/status.php"]).stdout)
    state["nextcloud_maintenance"] = nextcloud.get("maintenance")
    application_health(state)
    state["history"] = history_state(OLD_HOME)
    for relative in [".ssh/authorized_keys", ".codex/auth.json", ".claude/.credentials.json"]:
        path = OLD_HOME / relative
        if path.is_file():
            state["credentials"][relative] = digest(path)
    for relative in runtime_paths(OLD_HOME):
        path = OLD_HOME / relative
        state["runtime_files"][relative] = backup(path, PRIVATE / "runtime" / relative)
    state["development"] = development.capture(OLD_HOME, PRIVATE / "development")
    state["samba"] = samba.snapshot()
    state["booted_kernel"] = str((Path("/run/booted-system") / "kernel").resolve())
    state["target_kernel"] = str((target / "kernel").resolve())
    if state["booted_kernel"] != state["target_kernel"]:
        raise RuntimeError("Candidate changes the running kernel")
    save(state, "prepared")


def pause(state):
    for timer in PAUSED_TIMERS:
        if run(["systemctl", "is-active", "--quiet", timer], check=False).returncode == 0:
            state["timers"].append(timer)
            save(state)
            run(["systemctl", "stop", timer])
    for unit in ["codex-remote-control", "codex-app-server-refresh", "dev-runtime-maintenance"]:
        run(["systemctl", "stop", unit + ".service"])


def resume_timers(state):
    for timer in state["timers"]:
        run(["systemctl", "start", timer])


def unix_rename():
    state = load()
    if state["phase"] != "quiesced":
        raise RuntimeError("UNIX rename is only valid inside the armed transaction")
    preflight()
    run(["usermod", "--login", NEW, "--home", NEW_HOME, "--move-home",
         "--comment", "Amahi", OLD], timeout=1800)
    if not OLD_HOME.exists():
        OLD_HOME.symlink_to(NEW_HOME)
    for filename in ["/etc/subuid", "/etc/subgid"]:
        path = Path(filename)
        lines = path.read_text().splitlines(keepends=True)
        atomic(path, "".join(NEW + line[len(OLD):] if line.startswith(OLD + ":") else line for line in lines))
    old = Path("/var/lib/AccountsService/users") / OLD
    new = old.with_name(NEW)
    if old.exists():
        if new.exists():
            raise RuntimeError("Destination AccountsService entry already exists")
        old.rename(new)
    save(state, "unix-renamed")


def migrate_runtime(state):
    for relative in state["runtime_files"]:
        path = NEW_HOME / relative
        if path.is_symlink():
            target = os.readlink(path)
            if target.startswith(str(OLD_HOME) + "/"):
                path.unlink()
                path.symlink_to(target.replace(str(OLD_HOME), str(NEW_HOME), 1))
                os.lchown(path, 1000, 100)
        elif path.is_file():
            before = path.read_text()
            after = rebase_config(before, relative)
            if before != after:
                atomic(path, after)


def activate(target, allowed_failed=()):
    run(["nix-env", "--profile", "/nix/var/nix/profiles/system", "--set", target], timeout=180)
    result = run([Path(target) / "bin/switch-to-configuration", "switch"], timeout=1200, check=False)
    unexpected = set(failed_units()) - set(allowed_failed)
    if unexpected or (result.returncode and not (
            result.returncode == 4 and allowed_failed
            and str(Path("/run/current-system").resolve()) == str(target))):
        raise RuntimeError("Generation activation failed or introduced failed units")


def host_uid_processes():
    root = Path("/").stat()
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != 1000:
                continue
            process_root = (entry / "root").stat()
            if (root.st_dev, root.st_ino) == (process_root.st_dev, process_root.st_ino):
                result.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return result


def start_user_runtime(username):
    run(["loginctl", "enable-linger", username])
    run(["systemctl", "start", "user@1000.service"])
    for _ in range(50):
        runtime = Path("/run/user/1000")
        if runtime.is_dir() and runtime.stat().st_uid == 1000 and (runtime / "bus").exists():
            return
        time.sleep(0.2)
    raise RuntimeError("User runtime directory/bus did not become ready")


def stop_user_runtime():
    run(["loginctl", "disable-linger", "1000"], check=False)
    run(["loginctl", "terminate-user", "1000"], check=False)
    run(["systemctl", "stop", "user@1000.service", "user-runtime-dir@1000.service"], check=False)
    for _ in range(40):
        for pid in host_uid_processes():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        cached = run(["loginctl", "show-user", "1000"], check=False).returncode == 0
        if not cached and not host_uid_processes():
            return
        time.sleep(0.25)
    raise RuntimeError("Old user processes/logind identity did not disappear")


def apply():
    state = load()
    if state["phase"] == "awaiting-confirmation":
        verify(state)
        return
    if state["phase"] != "prepared":
        raise RuntimeError("Migration is not prepared")
    preflight()
    if str(Path("/run/current-system").resolve()) != state["previous"]:
        raise RuntimeError("Active generation changed since preparation")
    with ExitStack() as stack:
        for filename in ["/run/lock/nixos-flake-update.lock", "/run/lock/host-deployment.lock"]:
            lock = stack.enter_context(open(filename, "a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        project_root = OLD_HOME / ".local/state/dev/projects"
        for project in sorted(project_root.iterdir()):
            for filename in [project / "startup.lock", project / "home/.agent-session.lock"]:
                if not filename.parent.exists():
                    continue
                lock = stack.enter_context(open(filename, "r+"))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A maintenance-lock wait may outlast the initial process inventory.
        preflight()
        save(state, "quiescing")
        try:
            run(["systemd-run", "--unit=account-rename-rollback", "--on-active=30min",
                 "--timer-property=AccuracySec=1s", "--property=Type=oneshot",
                 "--property=UMask=0077", "--property=TimeoutStartSec=20min",
                 "--property=Environment=PATH=/run/current-system/sw/bin:/run/wrappers/bin",
                 PYTHON, SCRIPT, "rollback"])
            pause(state)
            for path in ACCOUNT_FILES:
                state["account_files"][path] = backup(path, PRIVATE / "account" / path.lstrip("/"))
            save(state)
            run(["systemctl", "stop", "samba-smbd.service", "samba-nmbd.service", "samba-wsdd.service"])
            state["samba_backup"] = backup("/var/lib/samba/private/passdb.tdb", PRIVATE / "passdb.tdb")
            if not state["samba_backup"]:
                raise RuntimeError("Samba credential database backup missing")
            run(["systemctl", "stop", "sshd.service"])
            stop_user_runtime()
            # Take rollback copies only after their writers have exited.
            for relative in runtime_paths(OLD_HOME):
                saved = PRIVATE / "runtime" / relative
                if present(saved):
                    saved.unlink()
                state["runtime_files"][relative] = backup(OLD_HOME / relative, saved)
            for path in ACCOUNT_FILES:
                if "/linger/" not in path:
                    saved = PRIVATE / "account" / path.lstrip("/")
                    if present(saved):
                        saved.unlink()
                    state["account_files"][path] = backup(path, saved)
            state["groups"] = sorted(os.getgrouplist(OLD, 100))
            state["shadow_digest"] = hashlib.sha256(shadow_tail(OLD).encode()).hexdigest()
            state["credentials"] = {relative: digest(OLD_HOME / relative)
                                    for relative in state["credentials"]}
            state["samba"] = samba.snapshot()
            state["development"] = development.capture(OLD_HOME, PRIVATE / "development-cutover")
            save(state)
            for project in state["development"]["projects"]:
                if project["running"]:
                    container = project["container"]
                    run(["docker", "exec", container, "bash", "-ec",
                         "if [ -x /usr/local/bin/dev-ui ]; then dev-ui _checkpoint; fi"], timeout=180)
                    run(["docker", "stop", "--time", "30", container], timeout=90)
            save(state, "quiesced")
            samba.rename(rename_script=str(STATE / "unix-rename"), expected=state["samba"])
            state = load()
            save(state, "samba-renamed")
            state["runtime_migration_started"] = True
            save(state)
            migrate_runtime(state)
            state["development_migration_started"] = True
            save(state)
            development.migrate(state["development"], NEW_HOME)
            for relative, expected in state["credentials"].items():
                if digest(NEW_HOME / relative) != expected:
                    raise RuntimeError("Credential file changed during the home move")
            state["credentials_preserved"] = True
            save(state, "files-migrated")
            # Nix activates user units and needs the new user's runtime/bus ready.
            start_user_runtime(NEW)
            activate(state["target"], state["failed_units"])
            save(state, "activated")
            run(["loginctl", "enable-linger", NEW])
            run(["systemctl", "start", "user@1000.service", "sshd.service", "samba-smbd.service",
                 "samba-nmbd.service", "samba-wsdd.service"])
            development.start(state["development"], NEW_HOME)
            user_command(NEW, ["env", "XDG_RUNTIME_DIR=/run/user/1000",
                               "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                               "systemctl", "--user", "daemon-reload"])
            user_command(NEW, ["env", "XDG_RUNTIME_DIR=/run/user/1000",
                               "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                               "systemctl", "--user", "restart", "token-eye-headroom.service"])
            user_command(NEW, ["env", "XDG_RUNTIME_DIR=/run/user/1000",
                               "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                               "systemctl", "--user", "start",
                               "token-eye-dogfood.timer", "token-eye-update.timer"])
            for _ in range(60):
                try:
                    for port in (8787, 8788):
                        with socket.create_connection(("172.17.0.1", port), timeout=1):
                            pass
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("Migrated TokenEye gateways did not become reachable")
            # These services have explicit restart preservation during normal updates.
            run(["systemctl", "start", "codex-remote-control.service"], timeout=180)
            verify(state)
            resume_timers(state)
            save(state, "awaiting-confirmation")
        except BaseException as error:
            state = load()
            state["error"] = type(error).__name__ + ": " + str(error)
            save(state, "rolling-back")
            rollback(state)
            raise


def ssh_verify(home):
    key = PRIVATE / "verification-key"
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key])
    host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
    known = PRIVATE / "known_hosts"
    atomic(known, "127.0.0.1 " + " ".join(host_key[:2]) + "\n")
    authorized = home / ".ssh/authorized_keys"
    original = authorized.read_bytes()
    try:
        atomic(authorized, original.rstrip(b"\n") + b"\n" + key.with_suffix(".pub").read_bytes())
        result = run(["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                      "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                      "-o", f"UserKnownHostsFile={known}", "-o", "ConnectTimeout=10",
                      f"{NEW}@127.0.0.1", "id -u; printf '%s\\n' \"$HOME\"; sudo -n id -u"])
        if result.stdout.splitlines() != ["1000", str(NEW_HOME), "0"]:
            raise RuntimeError("Fresh SSH login identity/home/sudo check failed")
    finally:
        atomic(authorized, original)


def verify(state=None):
    state = state or load()
    account = pwd.getpwnam(NEW)
    if (account.pw_uid, account.pw_gid, account.pw_dir) != (1000, 100, str(NEW_HOME)):
        raise RuntimeError("New account identity mismatch")
    try:
        pwd.getpwnam(OLD)
    except KeyError:
        pass
    else:
        raise RuntimeError("Old UNIX login still exists")
    if sorted(os.getgrouplist(NEW, 100)) != state["groups"]:
        raise RuntimeError("Supplementary groups changed")
    if hashlib.sha256(shadow_tail(NEW).encode()).hexdigest() != state["shadow_digest"]:
        raise RuntimeError("UNIX password or aging state changed")
    if not OLD_HOME.is_symlink() or OLD_HOME.resolve() != NEW_HOME:
        raise RuntimeError("Home compatibility link missing")
    if not state.get("credentials_preserved"):
        raise RuntimeError("Credential preservation was not verified before clients resumed")
    for relative, expected in state["credentials"].items():
        if relative == ".ssh/authorized_keys" and digest(NEW_HOME / relative) != expected:
            raise RuntimeError("Credential file changed: " + relative)
        if not (NEW_HOME / relative).is_file():
            raise RuntimeError("Credential file missing: " + relative)
    for filename in ["/etc/subuid", "/etc/subgid"]:
        if f"{NEW}:100000:65536" not in Path(filename).read_text().splitlines():
            raise RuntimeError("Subordinate ID range changed")
    if str(Path("/run/current-system").resolve()) != state["target"]:
        raise RuntimeError("Wrong active NixOS generation")
    for unit in CRITICAL:
        run(["systemctl", "is-active", "--quiet", unit + ".service"])
    if run(["loginctl", "show-user", NEW, "--property=Linger", "--value"]).stdout.strip() != "yes":
        raise RuntimeError("User lingering is disabled")
    user_command(NEW, ["env", "XDG_RUNTIME_DIR=/run/user/1000",
                       "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                       "systemctl", "--user", "daemon-reload"])
    user_command(NEW, ["env", "XDG_RUNTIME_DIR=/run/user/1000",
                       "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                       "systemctl", "--user", "is-active", "--quiet", "token-eye-headroom.service"])
    samba.verify("/etc/samba/smb.conf", state["samba"], username=NEW)
    samba.verify_logins("/etc/samba/smb.conf", state["samba"], private_directory=PRIVATE)
    development.verify(state["development"], NEW_HOME)
    application_health(state)
    current_history = history_state(NEW_HOME)
    if current_history["count"] < state["history"]["count"]:
        raise RuntimeError("Existing Codex history entries disappeared")
    if state["history"]["sample"]:
        cwd, rollout = state["history"]["sample"]
        if not Path(cwd).is_dir() or not Path(rollout).is_file():
            raise RuntimeError("Legacy chat paths no longer open")
        with open(rollout, "rb") as stream:
            stream.read(1)
    user_command(NEW, ["codex", "login", "status"])
    claude = json.loads(user_command(NEW, ["claude", "auth", "status", "--json"]).stdout)
    if claude.get("loggedIn") is not True:
        raise RuntimeError("Existing Claude authentication is unavailable")
    ssh_verify(NEW_HOME)
    for relative, expected in state["credentials"].items():
        if relative == ".ssh/authorized_keys" and digest(NEW_HOME / relative) != expected:
            raise RuntimeError("Credential verification did not restore original file")
    state["verified_at"] = time.time()
    save(state)


def rollback(state=None):
    state = state or load()
    if state["phase"] in ("confirmed", "rolled-back"):
        return
    if state["phase"] in ("prepared", "preparing"):
        save(state, "rolled-back")
        return
    save(state, "rolling-back")
    # Prevent writers while restoring the original generation and metadata.
    for timer in state.get("timers", []):
        run(["systemctl", "stop", timer], check=False)
    for unit in ["codex-remote-control.service", "sshd.service", "user@1000.service",
                 "samba-smbd.service", "samba-nmbd.service", "samba-wsdd.service"]:
        run(["systemctl", "stop", unit], check=False)
    stop_user_runtime()
    for project in state["development"]["projects"]:
        new_project = development.compose_identity(project["name"], NEW_HOME / "projects" / project["name"])
        if new_project:
            ids = run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={new_project}"], check=False).stdout.split()
            if ids:
                run(["docker", "stop", "--time", "30", *ids], check=False, timeout=120)
    if OLD_HOME.is_symlink():
        if OLD_HOME.resolve() != NEW_HOME:
            raise RuntimeError("Refusing to replace an unexpected old-home link")
        OLD_HOME.unlink()
    if NEW_HOME.exists():
        if OLD_HOME.exists():
            raise RuntimeError("Both home directories exist; manual recovery required")
        NEW_HOME.rename(OLD_HOME)
    if state.get("runtime_migration_started"):
        for relative, existed in state["runtime_files"].items():
            restore(OLD_HOME / relative, PRIVATE / "runtime" / relative, existed)
    if state.get("development_migration_started"):
        development.rollback_files(state["development"], OLD_HOME)
    for path, existed in state.get("account_files", {}).items():
        restore(path, PRIVATE / "account" / path.lstrip("/"), existed)
    if state.get("samba_backup"):
        restore("/var/lib/samba/private/passdb.tdb", PRIVATE / "passdb.tdb", True)
    run(["nscd", "--invalidate", "passwd"], check=False)
    run(["nscd", "--invalidate", "group"], check=False)
    start_user_runtime(OLD)
    activate(state["previous"], state.get("failed_units", []))
    run(["loginctl", "enable-linger", OLD])
    run(["systemctl", "start", "sshd.service", "user@1000.service", "samba-smbd.service",
         "samba-nmbd.service", "samba-wsdd.service"])
    for project in state["development"]["projects"]:
        if project["running"]:
            run(["docker", "start", project["container"]], timeout=120)
    user_command(OLD, ["env", "XDG_RUNTIME_DIR=/run/user/1000",
                       "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                       "systemctl", "--user", "daemon-reload"])
    user_command(OLD, ["env", "XDG_RUNTIME_DIR=/run/user/1000",
                       "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                       "systemctl", "--user", "restart", "token-eye-headroom.service"])
    run(["systemctl", "start", "codex-remote-control.service"], timeout=180)
    resume_timers(state)
    if hashlib.sha256(shadow_tail(OLD).encode()).hexdigest() != state["shadow_digest"]:
        raise RuntimeError("Rollback password verification failed")
    save(state, "rolled-back")


def finalize_project_source(state):
    item = next(project for project in state["development"]["projects"] if project["name"] == "token-eye")
    source = NEW_HOME / "projects/token-eye"
    if not state.get("token_eye_commit"):
        files = [".dev/compose.yaml", "scripts/token-eye-dogfood-server",
                 "scripts/token-eye-dogfood-server.test.py"]
        user_command(NEW, ["git", "-C", source, "add", "--", *files])
        user_command(NEW, ["git", "-C", source, "commit", "-m", "fix: preserve runtime after account rename",
                           "-m", "Keep the existing proxy image and ports while moving the host account and home. Update the recovery wrapper to the migrated Compose identity."])
        state["token_eye_commit"] = user_command(NEW, ["git", "-C", source, "rev-parse", "HEAD"]).stdout.strip()
        item["head"] = state["token_eye_commit"]
        item["source_committed"] = True
        save(state)
    user_command(NEW, ["git", "-C", source, "push", "origin",
                       f"{state['token_eye_commit']}:refs/heads/{item['branch']}"], timeout=180)
    state["token_eye_pushed"] = True
    save(state)


def confirm():
    state = load()
    if state["phase"] == "confirmed":
        if not state.get("token_eye_pushed"):
            finalize_project_source(state)
        return
    if state["phase"] != "awaiting-confirmation":
        raise RuntimeError("Migration is not awaiting confirmation")
    if os.environ.get("SUDO_USER") != NEW:
        raise RuntimeError("Confirm from a fresh amahi login using sudo")
    verify(state)
    save(state, "confirmed")
    run(["systemctl", "stop", "account-rename-rollback.timer"], check=False)
    # A source-push failure must never roll back a checked and confirmed account.
    finalize_project_source(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply", "unix-rename", "verify", "confirm", "rollback"])
    parser.add_argument("--target", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run with sudo")
    for directory, forbidden in [(STATE, 0o022), (PRIVATE, 0o077)]:
        info = directory.stat()
        if directory.is_symlink() or info.st_uid != 0 or info.st_mode & forbidden:
            raise RuntimeError("Migration state directory permissions are unsafe")
    # The Samba callback runs beneath apply's lock; every other action serializes.
    with ExitStack() as stack:
        if args.action != "unix-rename":
            lock = stack.enter_context(open(PRIVATE / "controller.lock", "a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "prepare":
            if not args.target:
                parser.error("prepare requires --target")
            prepare(args.target)
        else:
            {"apply": apply, "unix-rename": unix_rename, "verify": verify,
             "confirm": confirm, "rollback": rollback}[args.action]()


if __name__ == "__main__":
    main()
