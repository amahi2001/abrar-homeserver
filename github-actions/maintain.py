"""Refresh vendor runner images without interrupting running jobs."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


def log(message):
    print(f"[github-actions-maintenance] {message}", flush=True)


def run(*args, timeout=120):
    # Never include captured output or credential environment in exceptions.
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed ({result.returncode})")
    return result.stdout.strip()


def api(path):
    return json.loads(run("gh", "api", path, timeout=30))


def stable_version(release):
    tag = release.get("tag_name", "")
    if (release.get("draft") is not False or release.get("prerelease") is not False
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag)):
        raise RuntimeError("Cannot identify a stable official runner release")
    return tag[1:]


def registered(repository):
    name = f"buildfleet-server-{repository}"
    response = api(f"repos/amahi2001/{repository}/actions/runners?per_page=100")
    matches = [runner for runner in response["runners"] if runner["name"] == name]
    if response["total_count"] > 100 or len(matches) != 1:
        raise RuntimeError(f"{repository}: cannot identify exactly one registered runner")
    return matches[0]


def container(repository):
    return json.loads(run("docker", "inspect", f"github-actions-{repository}"))[0]


def processes(repository):
    rows = run("docker", "top", f"github-actions-{repository}", "-eo", "pid,comm")
    return [(int(pid), name) for pid, name in (row.split() for row in rows.splitlines()[1:])]


@contextlib.contextmanager
def idle_listener(repository, allow_offline=False):
    """Freeze only dispatch, then check again so a newly assigned job is safe."""
    runner = registered(repository)
    if runner.get("busy") is not False or (runner.get("status") != "online" and not allow_offline):
        yield None
        return
    tasks = processes(repository)
    listeners = [pid for pid, name in tasks if name == "Runner.Listener"]
    if any(name == "Runner.Worker" for _, name in tasks) or len(listeners) != 1:
        yield None
        return
    pid = listeners[0]
    pidfd = os.pidfd_open(pid)
    try:
        if ((pid, "Runner.Listener") not in processes(repository)
                or Path(os.readlink(f"/proc/{pid}/exe")).name != "Runner.Listener"):
            yield None
            return
        signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
        for _ in range(100):
            state = Path(f"/proc/{pid}/status").read_text().split("State:", 1)[1].split()[0]
            if state in {"T", "t"}:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError(f"{repository}: listener did not suspend")
        # pidfd protects PID reuse; membership protects a concurrent replacement.
        tasks = processes(repository)
        runner = registered(repository)
        if ((pid, "Runner.Listener") not in tasks
                or any(name == "Runner.Worker" for _, name in tasks)
                or runner.get("busy") is not False
                or (runner.get("status") != "online" and not allow_offline)):
            yield None
        else:
            yield pidfd
    finally:
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(pidfd, signal.SIGCONT)
        os.close(pidfd)


def stop_idle(repository, pidfd):
    unit = f"github-actions-{repository}.service"
    # Mark the unit stopping before killing the frozen, verified idle listener;
    # otherwise Restart=always could race the intended replacement.
    run("systemctl", "stop", "--no-block", unit)
    with contextlib.suppress(ProcessLookupError):
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    run("systemctl", "stop", unit)


def wait_online(repository, image_id):
    for _ in range(40):
        try:
            current = container(repository)
            connected = "Listening for Jobs" in run("docker", "logs", f"github-actions-{repository}")
            if (current["State"]["Running"] and current["Image"] == image_id
                    and connected and registered(repository).get("status") == "online"):
                return
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
    raise RuntimeError(f"{repository}: replacement did not register online")


def replace(repository, image, candidate):
    previous = container(repository)["Image"]
    if previous == candidate:
        if registered(repository).get("status") != "online":
            raise RuntimeError(f"{repository}: current image is not registered online")
        run("docker", "image", "tag", candidate, f"{image}-{repository}")
        return True
    try:
        with idle_listener(repository) as pidfd:
            if pidfd is None:
                log(f"{repository}: busy or uncertain; retrying next hour")
                return False
            run("docker", "image", "tag", previous, f"{image}-{repository}-previous")
            run("docker", "image", "tag", candidate, f"{image}-{repository}")
            stop_idle(repository, pidfd)
        run("systemctl", "start", f"github-actions-{repository}.service")
        wait_online(repository, candidate)
    except BaseException as error:
        # Restore the tag immediately. If a replacement already accepted a job,
        # leave that job running; the next safe service start uses the old image.
        run("docker", "image", "tag", previous, f"{image}-{repository}")
        try:
            current = container(repository)
        except RuntimeError:
            current = None
        if current and current["State"]["Running"] and current["Image"] == previous:
            raise RuntimeError(f"{repository}: update failed; previous image remains running")
        if current and current["State"]["Running"]:
            with idle_listener(repository, allow_offline=True) as pidfd:
                if pidfd is None:
                    raise RuntimeError(f"{repository}: rollback tag restored; active/unknown job prevents restart")
                stop_idle(repository, pidfd)
        run("systemctl", "reset-failed", f"github-actions-{repository}.service")
        run("systemctl", "start", f"github-actions-{repository}.service")
        wait_online(repository, previous)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError(f"{repository}: replacement failed; previous image restored online")
    log(f"{repository}: updated and registered online")
    return True


def candidate_image(context, image, state_path, version):
    try:
        state = json.loads(state_path.read_text())
        if (state["version"] == version and state["source"] == image
                and 0 <= time.time() - state["built_at"] < 7 * 86400):
            run("docker", "image", "inspect", state["image"])
            return state["image"]
    except (FileNotFoundError, KeyError, ValueError, RuntimeError):
        pass
    vendor = f"ghcr.io/actions/actions-runner:{version}"
    run("docker", "pull", vendor, timeout=600)
    digests = json.loads(run("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", vendor))
    base = next((digest for digest in digests if re.fullmatch(
        r"ghcr\.io/actions/actions-runner@sha256:[0-9a-f]{64}", digest)), None)
    if base is None:
        raise RuntimeError("Cannot resolve the official runner image digest")
    tag = f"{image}-candidate"
    run("docker", "build", "--pull", "--no-cache", "--build-arg", f"RUNNER_BASE={base}",
        "--tag", tag, str(context), timeout=3600)
    # Exercise the unmodified vendor runtime, Brotli, and required job tools.
    smoke = (
        './bin/Runner.Listener --version | grep -qx "' + version + '"; '
        "externals/node24/bin/node -e 'const a=require(\"node:assert/strict\"),z=require(\"node:zlib\");"
        "const v=process.versions.node.split(\".\").map(Number);"
        "a(v[0]===24&&(v[1]>22||(v[1]===22&&v[2]>=1)));"
        "a.equal(z.brotliDecompressSync(z.brotliCompressSync(Buffer.from(\"runner\"))).toString(),\"runner\")'; "
        "command -v git gh gcc make python rustup tar zstd zstdmt unzstd; "
        'test "$FORCE_JAVASCRIPT_ACTIONS_TO_NODE24" = true; '
        'test "$ACTIONS_RUNNER_FORCED_INTERNAL_NODE_VERSION" = node24'
    )
    run("docker", "run", "--rm", "--network", "none", tag, "bash", "-e", "-o", "pipefail", "-c", smoke)
    candidate = run("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    data = {"version": version, "source": image, "image": candidate, "built_at": time.time()}
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(data) + "\n")
    temp.replace(state_path)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("repositories", nargs="+")
    args = parser.parse_args()
    if not re.fullmatch(r"buildfleet-github-runner:[0-9a-f]{16}", args.image):
        parser.error("invalid local image name")
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", repo) for repo in args.repositories):
        parser.error("invalid repository name")
    state_dir = Path(os.environ["STATE_DIRECTORY"])
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "github-token"
    os.environ["GH_TOKEN"] = credential.read_text().strip()
    with (state_dir / "maintenance.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        version = stable_version(api("repos/actions/runner/releases/latest"))
        with Path("/run/lock/github-actions-image.lock").open("w") as image_lock:
            fcntl.flock(image_lock, fcntl.LOCK_EX)
            candidate = candidate_image(args.context, args.image, state_dir / "candidate.json", version)
        failed = False
        for repository in args.repositories:
            try:
                metadata = api(f"repos/amahi2001/{repository}")
                if metadata.get("private") is not True or metadata.get("owner", {}).get("login") != "amahi2001":
                    raise RuntimeError(f"{repository}: private repository ownership check failed")
                # Build outside this lock: core deployment and container
                # maintenance serialize only their host mutations.
                with Path("/run/lock/host-deployment.lock").open("w") as host_lock:
                    fcntl.flock(host_lock, fcntl.LOCK_EX)
                    replace(repository, args.image, candidate)
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
