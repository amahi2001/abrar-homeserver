"""Quiesce idle CI/dev work around Docker daemon replacement."""

import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import select
import subprocess
import time

PROJECTS = Path("/home/buildfleet/.local/state/dev/projects")
SYSTEM_CONTROL = Path("/etc/systemd/system.control")
SYSTEM_UNITS = (Path("/etc/systemd/system"), Path("/run/systemd/system"))
NIX_STORE = Path("/nix/store")
PAUSE_MARKER = Path("/run/nixos-fleet-maintenance/paused")


def run(*args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Fleet maintenance: {args[0]} {args[1]} failed ({result.returncode})")
    return result.stdout.strip()


def runners():
    spec = importlib.util.spec_from_file_location("runner_maintenance", os.environ["RUNNER_MAINTENANCE_MODULE"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dev_containers():
    rows = run("docker", "ps", "--filter", "label=dev.managed=true", "--format", '{{.ID}}\t{{.Label "dev.project"}}')
    result = {}
    for row in rows.splitlines():
        container, project = row.split("\t")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", project) or project in result:
            raise RuntimeError("Cannot identify exactly one dev container per project")
        result[project] = container
    return result


def compose_sidecars(dev):
    siblings = []
    for container in dev.values():
        project = run("docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', container)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
            raise RuntimeError("Cannot identify a dev container's Compose project")
        running = run("docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}")
        siblings.extend(cid for cid in running.splitlines() if cid not in dev.values() and cid not in siblings)
    return siblings


@contextlib.contextmanager
def agent_lock(container, uid, gid):
    # Keep stdin open while holding the same lock as dev-agent. This closes
    # the race with `dev shell ... -- dev-agent` while dispatch is quiesced.
    process = subprocess.Popen([
        "docker", "exec", "-i", "--user", f"{uid}:{gid}", container,
        "flock", "-n", "/home/node/.agent-session.lock", "sh", "-c",
        "printf 'locked\\n'; read -r release || true",
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 30)
        if not ready or process.stdout.readline() != b"locked\n":
            raise RuntimeError(f"Dev container {container} has active or unknown work")
        yield
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()


def runner_unit_target(unit, value):
    if not isinstance(value, str):
        raise RuntimeError(f"Invalid saved runner unit target: {unit}")
    target = Path(value)
    if (not re.fullmatch(r"github-actions-[a-z0-9][a-z0-9-]*\.service", unit)
            or target.parent.parent != NIX_STORE or target.name != unit
            or not re.fullmatch(r"[0-9abcdfghijklmnpqrsvwxyz]{32}-unit-" + re.escape(unit), target.parent.name)
            or not target.is_file() or target.resolve() != target):
        raise RuntimeError(f"Invalid saved runner unit target: {unit}")
    return target


def restore_runner_unit(unit, value):
    if run("systemctl", "show", "--property=LoadState", "--value", unit) != "not-found":
        return
    destination = SYSTEM_CONTROL / unit
    # A newly activated canonical unit always wins over the old ad-hoc link.
    if any(path.exists() or path.is_symlink() for path in
           (destination, *(directory / unit for directory in SYSTEM_UNITS))):
        return
    target = runner_unit_target(unit, value)
    SYSTEM_CONTROL.mkdir(parents=True, exist_ok=True)
    try:
        destination.symlink_to(target)
    except FileExistsError:
        return
    run("systemctl", "daemon-reload")


def resume(manifest, runner):
    if PAUSE_MARKER.exists() or PAUSE_MARKER.is_symlink():
        raise RuntimeError("Fleet pause marker is still present; recovery needs attention")
    errors = []
    containers = [(f"sidecar {cid}", cid, False) for cid in manifest.get("sidecars", [])]
    containers.extend((project, cid, True) for project, cid in manifest["dev"].items())
    for project, container, require_health in containers:
        try:
            run("docker", "start", container)
            for _ in range(600):
                state = json.loads(run("docker", "inspect", container))[0]["State"]
                health = state.get("Health", {}).get("Status")
                if state["Running"] and (health == "healthy" or (not require_health and health is None)):
                    break
                if not state["Running"] or state.get("Health", {}).get("Status") == "unhealthy":
                    raise RuntimeError(f"Container {project} did not recover")
                time.sleep(1)
            else:
                raise RuntimeError(f"Container {project} did not become healthy")
        except Exception as error:
            errors.append(str(error) if isinstance(error, RuntimeError) else type(error).__name__)
    for repository in manifest["runners"]:
        try:
            unit = f"github-actions-{repository}.service"
            if unit in manifest.get("runner_units", {}):
                restore_runner_unit(unit, manifest["runner_units"][unit])
            run("systemctl", "start", unit)
            for attempt in range(40):
                try:
                    current = runner.container(repository)
                    break
                except (RuntimeError, subprocess.TimeoutExpired):
                    if attempt == 39:
                        raise RuntimeError(f"Runner {repository} container was not created") from None
                    time.sleep(1)
            runner.wait_online(repository, current["Image"])
        except Exception as error:
            errors.append(str(error) if isinstance(error, RuntimeError) else type(error).__name__)
    if errors:
        raise RuntimeError("Fleet recovery needs attention: " + "; ".join(errors))


@contextlib.contextmanager
def guard(directory):
    """Hold project locks, stop idle dispatch, and restore the prior running set."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest_path = directory / "fleet.json"
    repositories = json.loads(os.environ["RUNNER_REPOSITORIES"])
    if not isinstance(repositories, list) or any(not isinstance(repo, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", repo) for repo in repositories):
        raise RuntimeError("Invalid managed runner repository list")
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "github-token"
    os.environ["GH_TOKEN"] = credential.read_text().strip()
    runner = runners()
    owner = pwd.getpwnam("buildfleet")
    projects = PROJECTS
    manifest = {"dev": dev_containers(), "runners": [], "runner_units": {}, "resumed": False}
    stopped = False
    with contextlib.ExitStack() as locks:
        for project, container in manifest["dev"].items():
            state_dir = projects / project
            lock_path = state_dir / "startup.lock"
            if not lock_path.is_file() or lock_path.is_symlink() or state_dir.is_symlink():
                raise RuntimeError(f"Cannot locate the existing startup lock for {project}")
            lock = locks.enter_context(lock_path.open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"Dev project {project} has an active session or preparation")
            if dev_containers().get(project) != container:
                raise RuntimeError(f"Dev project {project} changed during idle checks")
            locks.enter_context(agent_lock(container, owner.pw_uid, owner.pw_gid))
            # Same supported checkpoint as dev stop; old images without a viewer
            # can proceed, while a failed checkpoint leaves the running box alone.
            run("docker", "exec", "--user", f"{owner.pw_uid}:{owner.pw_gid}", container,
                "bash", "-ec", "if [ -x /usr/local/bin/dev-ui ]; then dev-ui _checkpoint; fi")
        listeners = []
        for repository in repositories:
            unit = f"github-actions-{repository}.service"
            status = run("systemctl", "show", "--property=ActiveState", "--value", unit)
            if status in {"inactive", "failed"}:
                continue
            if status != "active":
                raise RuntimeError(f"Runner {repository} is changing state; retry when idle")
            control = SYSTEM_CONTROL / unit
            if control.is_symlink():
                try:
                    target = control.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise RuntimeError(f"Invalid saved runner unit target: {unit}") from None
                manifest["runner_units"][unit] = str(runner_unit_target(unit, str(target)))
            pidfd = locks.enter_context(runner.idle_listener(repository))
            if pidfd is None:
                raise RuntimeError(f"Runner {repository} has active or unknown work")
            listeners.append((repository, pidfd))
            manifest["runners"].append(repository)
        manifest["sidecars"] = compose_sidecars(manifest["dev"])
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        parent = PAUSE_MARKER.parent
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o7777 != 0o700:
            raise RuntimeError("Fleet pause directory has unexpected ownership or permissions")
        try:
            with PAUSE_MARKER.open("x") as pause:
                os.fchmod(pause.fileno(), 0o600)
                pause_identity = os.fstat(pause.fileno())
        except FileExistsError:
            raise RuntimeError("Fleet pause marker already exists; inspect it before retrying") from None
        try:
            stopped = True
            for repository, pidfd in listeners:
                runner.stop_idle(repository, pidfd)
            for container in manifest["dev"].values():
                run("docker", "stop", "--time", "30", container)
            for container in manifest["sidecars"]:
                run("docker", "stop", "--time", "30", container)
            yield
        finally:
            try:
                current = PAUSE_MARKER.lstat()
            except OSError:
                raise RuntimeError("Fleet pause marker disappeared; recovery needs attention") from None
            if (current.st_dev, current.st_ino) != (pause_identity.st_dev, pause_identity.st_ino):
                raise RuntimeError("Fleet pause marker was replaced; recovery needs attention")
            PAUSE_MARKER.unlink()
            if stopped:
                resume(manifest, runner)
                manifest["resumed"] = True
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
