#!/usr/bin/env python3
"""Update the DNS or VPN container stack, restoring images/config on failure."""

import fcntl
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(*args, timeout=180):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout).stdout.strip()


def vpn_ready():
    if run("docker", "inspect", "--format", "{{.State.Health.Status}}", "gluetun", timeout=10) != "healthy":
        raise RuntimeError("VPN container is not healthy")
    run("docker", "exec", "gluetun", "sh", "-ceu",
        "test -d /sys/class/net/tun0 && iptables -S OUTPUT | grep -qx -- '-P OUTPUT DROP'", timeout=10)


def healthy(kind):
    if kind == "adguard":
        run("systemctl", "is-active", "--quiet", "docker-adguardhome.service", timeout=10)
        run("curl", "--fail", "--silent", "--show-error", "--max-time", "5",
            "-o", "/dev/null", "http://127.0.0.1:8080/", timeout=10)
        result = run("dig", "+time=5", "+tries=1", "@127.0.0.1", "example.com", "A", timeout=10)
        if "status: NOERROR" not in result:
            raise RuntimeError("DNS lookup failed")
        return
    vpn_ready()
    if kind == "vpn-ready":
        return
    current = run("docker", "inspect", "--format", "{{.Id}}", "gluetun", timeout=10)
    namespace = run("docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", "transmission-vpn", timeout=10)
    if not namespace.startswith("container:"):
        raise RuntimeError("Transmission has a separate network namespace")
    target = run("docker", "inspect", "--format", "{{.Id}}", namespace.removeprefix("container:"), timeout=10)
    if target != current:
        raise RuntimeError("Transmission uses a stale VPN network namespace")
    run("docker", "exec", "transmission-vpn", "sh", "-ceu",
        'export TR_AUTH="$USER:$PASS"; exec transmission-remote 127.0.0.1:9091 --authenv --session-info', timeout=10)


def wait_healthy(kind):
    for attempt in range(60):
        try:
            healthy(kind)
            return
        except (subprocess.SubprocessError, RuntimeError):
            if attempt == 59:
                raise RuntimeError(f"{kind} health check failed") from None
            time.sleep(2)


def update(kind, state_dir=Path("/var/lib/container-updates")):
    if kind == "adguard":
        containers = [("adguardhome", "adguard/adguardhome:latest")]
        services = ["docker-adguardhome.service"]
        configs = [Path("/var/lib/adguardhome/conf")]
    else:
        containers = [("gluetun", "qmcgaw/gluetun:latest"),
                      ("transmission-vpn", "lscr.io/linuxserver/transmission:latest")]
        services = ["docker-transmission-vpn.service", "docker-gluetun.service"]
        configs = [Path("/var/lib/transmission-vpn"), Path("/var/lib/gluetun")]

    # Use the running image, so a prior interrupted pull cannot hide an update.
    previous = {image: run("docker", "inspect", "--format", "{{.Image}}", name)
                for name, image in containers}
    try:
        for _, image in containers:
            run("docker", "pull", image, timeout=600)
    except BaseException:
        for image, image_id in previous.items():
            run("docker", "image", "tag", image_id, image)
        raise
    if all(run("docker", "image", "inspect", "--format", "{{.Id}}", image) == image_id
           for image, image_id in previous.items()):
        print(f"{kind}: images unchanged", flush=True)
        return

    backup = Path(tempfile.mkdtemp(prefix=f"{kind}-", dir=state_dir))
    rollback_tags = []
    stopped = False
    snapshots = []
    try:
        # Tags keep rollback images protected from the weekly Docker prune job.
        for index, image_id in enumerate(previous.values()):
            tag = f"buildfleet-container-rollback:{kind}-{index}"
            run("docker", "image", "tag", image_id, tag)
            rollback_tags.append(tag)
        stopped = True
        run("systemctl", "stop", *services)
        for index, config in enumerate(configs):
            destination = backup / str(index)
            run("cp", "--archive", "--reflink=auto", str(config), str(destination))
            snapshots.append((config, destination))
        run("systemctl", "start", *reversed(services))
        wait_healthy(kind)
    except BaseException:
        # Finish recovery even when the updater was stopped by systemd.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print(f"{kind}: update failed; restoring images and config from {backup}", flush=True)
        if stopped:
            run("systemctl", "stop", *services)
        for image, image_id in previous.items():
            run("docker", "image", "tag", image_id, image)
        for config, source in snapshots:
            if config.exists():
                shutil.rmtree(config)
            run("cp", "--archive", "--reflink=auto", str(source), str(config))
        if stopped:
            run("systemctl", "start", *reversed(services))
            wait_healthy(kind)
        # Retain the backup and rollback tags for diagnosis or manual recovery.
        raise
    shutil.rmtree(backup)
    for tag in rollback_tags:
        run("docker", "image", "rm", tag)
    print(f"{kind}: updated and healthy", flush=True)


def interrupted(signum, frame):
    raise RuntimeError(f"Interrupted by signal {signum}")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("adguard", "vpn", "vpn-ready", "transmission-ready"):
        raise SystemExit("Usage: container-update.py adguard|vpn|vpn-ready|transmission-ready")
    if sys.argv[1] in ("vpn-ready", "transmission-ready"):
        wait_healthy("vpn-ready" if sys.argv[1] == "vpn-ready" else "vpn")
        return
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    state_dir = Path("/var/lib/container-updates")
    state_dir.mkdir(mode=0o700, exist_ok=True)
    with (state_dir / f"{sys.argv[1]}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Post-start probes return above without recursively taking this lock.
        with Path("/run/lock/host-deployment.lock").open("w") as deployment_lock:
            fcntl.flock(deployment_lock, fcntl.LOCK_EX)
            update(sys.argv[1], state_dir)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        # Commands can contain authentication headers; never print their output.
        print(f"Container maintenance failed ({type(error).__name__}). See retained backup.", file=sys.stderr)
        raise SystemExit(1) from None
