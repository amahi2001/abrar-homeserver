"""Measure the real Compose startup marker check on isolated, unlabeled containers."""

import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


def run(*argv, timeout=90, **kwargs):
    return subprocess.check_output(argv, text=True, timeout=timeout, **kwargs).strip()


def probe(path, image, project):
    # Resolve the healthcheck from the launcher assets, but copy no accounts,
    # mounts, ports, or fleet management labels into the disposable project.
    env = {**os.environ, **{f"DEV_{name}": "/tmp/canary" for name in
           ("SOURCE", "PROJECT_STATE", "ACCOUNTS", "CREDENTIALS", "AGENT_CONFIG")},
           "DEV_PROJECT": project, "DEV_IMAGE": image, "DEV_UID": "1000", "DEV_GID": "1000",
           "DEV_GIT_NAME": "Fixture", "DEV_GIT_EMAIL": "fixture@example.invalid"}
    return json.loads(run("docker", "compose", "-p", project, "-f", path, "config",
                          "--format", "json", env=env))["services"]["dev"]["healthcheck"]


def wait(container, expected, timeout):
    start = time.monotonic()
    while run("docker", "inspect", "-f", "{{.State.Health.Status}}", container) != expected:
        if time.monotonic() - start > timeout:
            raise RuntimeError(f"{container} did not become {expected}")
        time.sleep(.2)
    return time.monotonic() - start


def cpu(container):
    pid = run("docker", "inspect", "-f", "{{.State.Pid}}", container)
    relative = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1].lstrip("/")
    stats = Path("/sys/fs/cgroup", relative, "cpu.stat").read_text()
    return int(dict(line.split() for line in stats.splitlines())["usage_usec"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="already-built image tag or digest")
    parser.add_argument("--baseline", type=Path, help="deployed compose.yaml")
    args = parser.parse_args()
    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    project = "dev-probe-" + uuid.uuid4().hex[:12]
    paths = {"candidate": Path(__file__).with_name("compose.yaml")}
    if args.baseline:
        paths["baseline"] = args.baseline
    image = run("docker", "image", "inspect", "-f", "{{.Id}}", args.image)
    services = {name: dict(image=image, pull_policy="never", network_mode="none",
                           read_only=True, tmpfs=["/tmp"], init=True,
                           cap_drop=["ALL"], security_opt=["no-new-privileges"],
                           entrypoint=["sh", "-c", "touch /tmp/dev-ready; exec sleep infinity"],
                           healthcheck=probe(path, image, project)) for name, path in paths.items()}
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp, "compose.json")
        file.write_text(json.dumps({"services": services}))
        compose = ("docker", "compose", "-p", project, "-f", file)
        try:
            # A disconnected client cannot cancel an in-flight Docker create.
            # Wait for daemon acknowledgement before cleaning up this project.
            saved = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
            try:
                run(*compose, "create", timeout=None)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, saved)
            ids = {name: run(*compose, "ps", "-a", "-q", name) for name in paths}
            timing = {}
            for name, cid in ids.items():
                start = time.monotonic()
                run("docker", "start", cid)
                wait(cid, "healthy", 70)
                timing[name] = {"firstHealthySeconds": round(time.monotonic() - start, 2)}
            before = {name: (cpu(cid), time.monotonic()) for name, cid in ids.items()}
            time.sleep(60)
            for name, cid in ids.items():
                used = cpu(cid) - before[name][0]
                timing[name]["oneCoreCpuPercent"] = round(used / (time.monotonic() - before[name][1]) / 1e4, 3)
            cid = ids["candidate"]
            run("docker", "exec", cid, "rm", "/tmp/dev-ready")
            timing["candidate"]["markerLossSeconds"] = round(wait(cid, "unhealthy", 75), 2)
            run("docker", "exec", cid, "touch", "/tmp/dev-ready")
            timing["candidate"]["recoverySeconds"] = round(wait(cid, "healthy", 45), 2)
            print(json.dumps(timing, indent=2))
        finally:
            run(*compose, "down", "--timeout", "5")


if __name__ == "__main__":
    main()
