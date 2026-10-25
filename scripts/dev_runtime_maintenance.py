"""Build current dev assets weekly, then refresh only idle running projects."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time


def log(message):
    print(f"[dev-runtime-maintenance] {message}", flush=True)


def run(*args, env=None, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} {args[1]} failed ({result.returncode})")
    return result.stdout.strip()


def atomic(path, data):
    temp = path.with_suffix(".maintenance.tmp")
    temp.write_bytes(data)
    temp.chmod(0o600)
    temp.replace(path)


def inspect(container):
    return json.loads(run("docker", "inspect", container))[0]


@contextmanager
def rollback_dispatch(project_dir, snapshots, previous, current):
    home = project_dir / "home"
    if home.is_symlink() or not home.is_dir():
        raise RuntimeError("Rollback needs attention: cannot verify the shared project home")
    mounts = json.loads(snapshots["compose.json"])["services"]["dev"].get("volumes", [])
    planned = [mount for mount in mounts if mount.get("target") == "/home/node"]
    if (len(planned) != 1 or planned[0].get("type") != "bind"
            or planned[0].get("source") != str(home)):
        raise RuntimeError("Rollback needs attention: unknown saved project home mount")
    for runtime in (previous, current):
        if runtime is None:
            continue
        mounted = [mount for mount in runtime.get("Mounts", []) if mount.get("Destination") == "/home/node"]
        if (len(mounted) != 1 or mounted[0].get("Type") != "bind"
                or mounted[0].get("Source") != str(home)):
            raise RuntimeError("Rollback needs attention: unknown running project home mount")
    path = home / ".agent-session.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Rollback needs attention: active agent work was left intact") from None
        held, linked = os.fstat(lock.fileno()), path.lstat()
        if (not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (linked.st_dev, linked.st_ino)):
            raise RuntimeError("Rollback needs attention: project agent lock changed")
        yield


def running_projects():
    rows = run("docker", "ps", "--filter", "label=dev.managed=true", "--format", '{{.ID}}\t{{.Label "dev.project"}}')
    projects = {}
    for row in rows.splitlines():
        container, project = row.split("\t")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", project) or project in projects:
            raise RuntimeError("Cannot identify one running container per managed project")
        projects[project] = container
    return projects


def idle(container):
    result = subprocess.run(["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}", container,
                             "flock", "-n", "/home/node/.agent-session.lock", "true"],
                            capture_output=True, timeout=30)
    return result.returncode == 0


def smoke(image):
    check = (
        'const a=require("node:assert/strict"),z=require("node:zlib");'
        'const v=process.versions.node.split(".").map(Number);'
        'a(v[0]===24&&(v[1]>22||(v[1]===22&&v[2]>=1)));'
        'a.equal(z.brotliDecompressSync(z.brotliCompressSync(Buffer.from("dev"))).toString(),"dev")'
    )
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "node", image, "-e", check)
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "bun", image, "-e",
        'const z=require("node:zlib");if(z.brotliDecompressSync(z.brotliCompressSync(Buffer.from("dev"))).toString()!=="dev")throw Error("Brotli round trip failed")')
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "bash", image,
        "-e", "-o", "pipefail", "-c", "codex --version; claude --version; uv --version")


def candidate(dev, state):
    prior = state.get("candidate", {})
    if prior.get("dev") == dev and 0 <= time.time() - prior.get("built_at", 0) < 7 * 86400:
        try:
            if run("docker", "image", "inspect", "--format", "{{.Id}}", prior["image"]) == prior["id"]:
                return prior
        except (RuntimeError, KeyError):
            pass
    run(dev, "setup", "--refresh-runtime", timeout=600)
    image = run(dev, "build", timeout=3600).splitlines()[-1]
    if not re.fullmatch(r"buildfleet-dev:[0-9a-f]{16}", image):
        raise RuntimeError("Dev build did not return its managed image tag")
    smoke(image)
    return {"dev": dev, "image": image,
            "id": run("docker", "image", "inspect", "--format", "{{.Id}}", image),
            "built_at": time.time()}


def refresh(dev, project, container, project_dir, desired):
    lock = (project_dir / "startup.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"{project}: session/preparation active; retrying next hour")
            return None
        # Re-read after taking the same lock as dev start/rebuild. Stopped
        # projects stay stopped, including one stopped during this invocation.
        if running_projects().get(project) != container or not idle(container):
            log(f"{project}: stopped, busy or uncertain; retrying next hour")
            return None
        metadata = json.loads((project_dir / "project.json").read_text())
        previous = inspect(container)
        if previous["Config"]["Labels"].get("com.docker.compose.project") != metadata["composeProject"]:
            raise RuntimeError(f"{project}: container does not match its saved project")
        snapshots = {name: (project_dir / name).read_bytes() if (project_dir / name).exists() else None
                     for name in ["project.json", "compose.json", "devcontainer.json"]}
        run("docker", "image", "tag", previous["Image"], f"{metadata['image']}-previous")
        try:
            run(dev, "rebuild", project, env={**os.environ, "DEV_LOCK_HELD": project,
                                            "DEV_HOST_LOCK_HELD": project}, timeout=1200)
            current = running_projects()[project]
            health = inspect(current)
            if health["State"].get("Health", {}).get("Status") != "healthy":
                raise RuntimeError(f"{project}: rebuilt container is not healthy")
            return {"candidate": desired["id"], "image": health["Image"]}
        except BaseException as error:
            current = running_projects().get(project)
            with rollback_dispatch(project_dir, snapshots, previous, inspect(current) if current else None):
                run("docker", "image", "tag", previous["Image"], metadata["image"])
                for name, data in snapshots.items():
                    path = project_dir / name
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, data)
                if current != container:
                    run("docker", "compose", "--project-directory", metadata["source"], "-p", metadata["composeProject"],
                        "-f", str(project_dir / "compose.json"), "up", "-d", "--no-build", "--no-deps", "--wait", "dev")
            if not isinstance(error, Exception):
                raise
            raise RuntimeError(f"{project}: update failed; previous image and project configuration restored")
    finally:
        lock.close()


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Dev maintenance interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", required=True)
    args = parser.parse_args()
    state_dir = Path(os.environ["STATE_DIRECTORY"])
    state_path = state_dir / "state.json"
    projects_dir = Path(os.environ.get("DEV_STATE_ROOT", str(Path.home() / ".local/state/dev"))) / "projects"
    with (state_dir / "maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            state = json.loads(state_path.read_text())
        except FileNotFoundError:
            state = {}
        desired = candidate(args.dev, state)
        state["candidate"] = desired
        state.setdefault("projects", {})
        atomic(state_path, (json.dumps(state) + "\n").encode())
        failed = False
        for project, container in running_projects().items():
            try:
                recorded = state["projects"].get(project, {})
                if recorded.get("candidate") == desired["id"] and recorded.get("image") == inspect(container)["Image"]:
                    continue
                with Path("/run/lock/host-deployment.lock").open("a") as host_lock:
                    fcntl.flock(host_lock, fcntl.LOCK_EX)
                    updated = refresh(args.dev, project, container, projects_dir / project, desired)
                if updated:
                    state["projects"][project] = updated
                    atomic(state_path, (json.dumps(state) + "\n").encode())
                    log(f"{project}: updated; source, home and volumes retained")
            except Exception as error:
                log(str(error) if isinstance(error, RuntimeError) else type(error).__name__)
                failed = True
        return int(failed)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log(str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        raise SystemExit(1)
