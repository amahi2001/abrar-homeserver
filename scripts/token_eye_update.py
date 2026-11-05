"""Deploy tested descendants of the installed TokenEye revision from origin/main."""

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tarfile
import tempfile
import time


SERVICE = "token-eye-headroom.service"


class Deferred(Exception):
    pass


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def atomic(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(path, value):
    atomic(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def owned_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise RuntimeError("unowned_deployment_file")


def read_record(path):
    owned_file(path)
    return json.loads(path.read_text())


@contextmanager
def locked(path, shared=False):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Deferred("busy") from None
        linked, held = path.lstat(), os.fstat(lock.fileno())
        if (not stat.S_ISREG(held.st_mode)
                or (linked.st_dev, linked.st_ino) != (held.st_dev, held.st_ino)):
            raise RuntimeError("lock_changed")
        yield


def command(*args, cwd=None, timeout=120, check=True):
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    with subprocess.Popen(args, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          start_new_session=True) as process:
        try:
            output = process.communicate(timeout=timeout)[0]
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if check and process.returncode:
        raise RuntimeError("command_failed")
    return subprocess.CompletedProcess(args, process.returncode, output)


def valid_receipt(value):
    return (type(value) is dict and set(value) == {"commit", "sha256"}
            and type(value["commit"]) is str and re.fullmatch(r"[0-9a-f]{40}", value["commit"])
            and type(value["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]))


class Updater:
    def __init__(self, home=None):
        self.home = Path.home() if home is None else Path(home)
        self.checkout = self.home / "projects/token-eye"
        self.project = self.home / ".local/state/dev/projects/token-eye"
        self.runtime = self.home / ".local/state/token-eye-nixos/runtime"
        self.binary = self.runtime / "bin/token-eye"
        self.receipt = self.runtime / "installed.json"
        self.state = self.runtime / "update"
        self.pending = self.state / "pending.json"
        self.previous = self.state / "previous.bin"
        self.dev = self.home / ".local/bin/dev"
        self.phase = "admission"
        self.installed_commit = None
        self.main_commit = None
        self.failure_phase = None

    def git(self, *args, check=True):
        return command("git", *args, cwd=self.checkout, check=check)

    def in_box(self, *args, timeout=120):
        return command(str(self.dev), "shell", "token-eye", "--", *args, timeout=timeout)

    def service_active(self):
        return command("systemctl", "--user", "is-active", "--quiet", SERVICE,
                       timeout=10, check=False).returncode == 0

    def restart(self):
        command("systemctl", "--user", "restart", SERVICE, timeout=60)

    def box_ready(self):
        metadata = json.loads((self.project / "project.json").read_text())
        containers = command("docker", "ps", "--quiet", "--no-trunc", "--filter",
                             f"label=com.docker.compose.project={metadata['composeProject']}",
                             "--filter", "label=com.docker.compose.service=dev").stdout.decode().split()
        if len(containers) != 1:
            raise Deferred("box_unavailable")
        status = command("docker", "inspect", "--format", "{{.State.Health.Status}}", containers[0]).stdout.strip()
        if status != b"healthy":
            raise Deferred("box_unavailable")

    def routes(self):
        result = []
        for action in ("routing", "claude-routing"):
            value = json.loads(self.in_box("/opt/token-eye/bin/token-eye", action, timeout=10).stdout)
            fields = ("requested", "configured", "enabled", "profiles")
            if (any(type(value.get(field)) is not bool for field in fields[:-1])
                    or type(value.get("profiles")) is not int or value["profiles"] < 0
                    or (action == "claude-routing" and type(value.get("forwardReady")) is not bool)):
                raise RuntimeError("invalid_routing_status")
            result.append({key: value[key] for key in (*fields, "forwardReady") if key in value})
        return result

    @staticmethod
    def healthy(current, baseline):
        for actual, prior in zip(current, baseline, strict=True):
            if actual["requested"] != prior["requested"]:
                return False
            if not prior["requested"]:
                if actual["enabled"]:
                    return False
            elif (not actual["configured"] or not actual["enabled"]
                  or actual["profiles"] < max(1, prior["profiles"])
                  or actual.get("forwardReady", True) is not True):
                return False
        return True

    def wait_ready(self, baseline):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if self.service_active() and self.healthy(self.routes(), baseline):
                    return
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise RuntimeError("readiness_failed")

    def installed(self):
        if self.runtime.resolve() != self.runtime or self.binary.parent.resolve() != self.binary.parent:
            raise RuntimeError("unknown_runtime_path")
        marker = read_record(self.runtime / "deployment.json")
        receipt = read_record(self.receipt)
        if marker != {"schema": 1, "kind": "token-eye-server-runtime"} or not valid_receipt(receipt):
            raise RuntimeError("unknown_deployment")
        owned_file(self.binary)
        if digest(self.binary) != receipt["sha256"]:
            raise RuntimeError("installed_binary_changed")
        self.installed_commit = receipt["commit"]
        return receipt

    def replace(self, expected, data):
        owned_file(self.binary)
        if digest(self.binary) != expected:
            raise RuntimeError("installed_binary_changed")
        atomic(self.binary, data, 0o755)

    def recover(self):
        self.phase = "rollback"
        pending = read_record(self.pending)
        previous, candidate = pending["previous"], pending["candidate"]
        owned_file(self.previous)
        if (not valid_receipt(previous) or not valid_receipt(candidate)
                or self.previous.is_symlink() or digest(self.previous) != previous["sha256"]):
            raise RuntimeError("rollback_backup_changed")
        actual = digest(self.binary)
        if actual not in (previous["sha256"], candidate["sha256"]):
            raise RuntimeError("installed_binary_changed")
        receipt = read_record(self.receipt)
        if receipt not in (previous, candidate):
            raise RuntimeError("installed_receipt_changed")
        self.replace(actual, self.previous.read_bytes())
        save(self.receipt, previous)
        self.restart()
        self.wait_ready(pending["routes"])
        self.pending.unlink()
        self.installed_commit = previous["commit"]
        self.phase = "rollback_complete"
        return "rolled_back"

    def build(self, revision, source):
        archive = source.parent / "source.tar"
        # git archive reads immutable objects, leaving the checkout and its branch alone.
        self.git("archive", "--format=tar", f"--output={archive}", revision)
        with tarfile.open(archive) as stream:
            stream.extractall(source, filter="data")
        archive.unlink()
        desktop = "/workspace/token-eye/" + str(source.relative_to(self.checkout)) + "/desktop"
        target = "/workspace/token-eye/build/main-update/target"
        self.in_box("timeout", "--signal=TERM", "--kill-after=30s", "2400s", "bash", "-c", r"""
set -eu
cd "$1"
cargo build --release --locked --manifest-path src-tauri/Cargo.toml --no-default-features --bin token-eye --target-dir "$2"
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/codex" "$fixture/claude" "$fixture/data" "$fixture/xdg"
unset TOKEN_EYE_CLAUDE_ACCOUNTS TOKEN_EYE_HISTORY_ACCOUNTS
export CODEX_HOME="$fixture/codex" CLAUDE_CONFIG_DIR="$fixture/claude" XDG_DATA_HOME="$fixture/xdg"
export TOKEN_EYE_DATA_DIR="$fixture/data" TOKEN_EYE_CODEX_ROOTS="[\"$fixture/codex\"]" TOKEN_EYE_ROUTING_AUTO_BOOTSTRAP=0
cargo test --locked --manifest-path src-tauri/Cargo.toml --no-default-features --lib --target-dir "$2"
python3 tests/headless-cli.py "$2/release/token-eye"
""", "token-eye-update", desktop, target, timeout=2460)
        return self.checkout / "build/main-update/target/release/token-eye"

    def deploy(self, candidate, revision, previous, baseline):
        self.phase = "backup"
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeError("invalid_candidate")
        data = candidate.read_bytes()
        updated = {"commit": revision, "sha256": hashlib.sha256(data).hexdigest()}
        atomic(self.previous, self.binary.read_bytes(), 0o700)
        save(self.pending, {"previous": previous, "candidate": updated, "routes": baseline})
        try:
            self.phase = "replace_binary"
            self.replace(previous["sha256"], data)
            self.phase = "restart_service"
            self.restart()
            self.phase = "verify_readiness"
            self.wait_ready(baseline)
            self.phase = "record_receipt"
            save(self.receipt, updated)
            self.pending.unlink()
        except BaseException:
            self.failure_phase = self.phase
            self.recover()
            return "rolled_back"
        self.installed_commit = revision
        self.phase = "complete"
        return "updated"

    def update(self):
        if self.pending.exists():
            self.phase = "recover_pending"
            self.box_ready()
            return self.recover()
        self.phase = "verify_installed"
        previous = self.installed()
        self.phase = "fetch_main"
        origin = self.git("remote", "get-url", "origin").stdout.decode().strip()
        if origin not in ("https://github.com/amahi2001/token-eye.git", "https://github.com/amahi2001/token-eye",
                          "git@github.com:amahi2001/token-eye.git"):
            raise RuntimeError("unexpected_origin")
        self.git("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        revision = self.git("rev-parse", "refs/remotes/origin/main^{commit}").stdout.decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError("invalid_revision")
        self.main_commit = revision
        self.phase = "verify_ancestry"
        if revision == previous["commit"]:
            return "current"
        ancestor = self.git("merge-base", "--is-ancestor", previous["commit"], revision, check=False).returncode
        if ancestor == 1:
            return "waiting_for_main"
        if ancestor:
            raise RuntimeError("ancestry_check_failed")
        self.phase = "verify_box"
        self.box_ready()
        if not self.service_active():
            raise Deferred("service_inactive")
        self.phase = "verify_routes"
        baseline = self.routes()
        if not self.healthy(baseline, baseline):
            raise Deferred("routing_unhealthy")
        work = self.checkout / "build/main-update"
        work.mkdir(parents=True, exist_ok=True)
        if work.resolve() != work:
            raise RuntimeError("unexpected_build_path")
        with tempfile.TemporaryDirectory(prefix="source-", dir=work) as temporary:
            self.phase = "build_and_test"
            candidate = self.build(revision, Path(temporary))
            # Revalidate the live deployment and toggle after the potentially long build.
            self.phase = "revalidate_deployment"
            if self.installed() != previous or self.routes() != baseline or not self.service_active():
                raise Deferred("deployment_changed")
            return self.deploy(candidate, revision, previous, baseline)

    def run(self):
        if (not self.runtime.is_dir() or self.runtime.resolve() != self.runtime
                or self.runtime.stat().st_uid != os.getuid()
                or self.runtime.stat().st_mode & 0o022
                or self.state.is_symlink()
                or (self.state.exists() and (self.state.stat().st_uid != os.getuid()
                                             or self.state.stat().st_mode & 0o022))):
            raise RuntimeError("unknown_runtime_path")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.chmod(0o700)
        try:
            with ExitStack() as locks:
                for path, shared in ((self.state / ".lock", False),
                                     (Path("/run/lock/host-deployment.lock"), True),
                                     (self.project / "startup.lock", False),
                                     (self.project / "home/.agent-session.lock", False)):
                    locks.enter_context(locked(path, shared))
                outcome = self.update()
            code = int(outcome == "rolled_back")
        except Deferred as error:
            outcome, code = f"deferred_{error}", 0
        except BaseException as error:
            # Exceptions and subprocess output can contain private account data.
            outcome, code = ("rollback_needs_review" if self.pending.exists() else "failed"), 1
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        report = {"checkedAt": datetime.now(timezone.utc).isoformat(), "outcome": outcome,
                  "phase": self.phase, "installedCommit": self.installed_commit, "mainCommit": self.main_commit}
        if self.failure_phase is not None:
            report["failurePhase"] = self.failure_phase
        save(self.state / "latest.json", report)
        print(f"TokenEye main update: {outcome}", flush=True)
        return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    arguments = parser.parse_args()
    os.umask(0o077)
    updater = Updater()
    if arguments.status:
        print((updater.state / "latest.json").read_text(), end="")
        return 0
    def interrupted(signum, frame):
        raise InterruptedError("interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    return updater.run()


if __name__ == "__main__":
    raise SystemExit(main())
