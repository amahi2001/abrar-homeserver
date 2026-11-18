"""Verify a stock devbox port plan and watch a reserved, running canary."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import select
import subprocess
import time


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=45, **kwargs).strip()


def check_ports(container, project, source, image):
    compose = ["docker", "compose", "--project-directory", str(source), "-f", str(Path(__file__).with_name("compose.yaml"))]
    override = source / ".dev/compose.yaml"
    if override.exists():
        services = json.loads(run("docker", "compose", "-f", str(override), "config", "--no-consistency", "--format", "json"))["services"]
        dev = services.get("dev", {})
        if (set(services) != {"dev"} or set(dev) - {"ports", "networks", "command", "entrypoint"}
                or dev.get("command") or dev.get("entrypoint") or dev.get("networks") != {"default": None}):
            raise ValueError("A custom service needs its own effective-image smoke")
        compose.extend(["-f", str(override)])
    env = {**os.environ, "DEV_PROJECT": project, "DEV_IMAGE": image, "DEV_SOURCE": str(source),
           "DEV_UID": str(os.getuid()), "DEV_GID": str(os.getgid()), "DEV_GIT_NAME": "Fixture",
           "DEV_GIT_EMAIL": "fixture@example.invalid"}
    for name in ("PROJECT_STATE", "CREDENTIALS", "ACCOUNTS", "AGENT_CONFIG"):
        env["DEV_" + name] = "/tmp/canary"
    config = json.loads(run(*compose, "config", "--format", "json", env=env))
    planned = sorted((f"{p['target']}/{p['protocol']}", p.get("host_ip"), str(p.get("published")))
                     for p in config["services"]["dev"].get("ports", []))
    published = json.loads(run("docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", container))
    actual = sorted((key, p["HostIp"], p["HostPort"]) for key, ports in published.items() for p in ports or [])
    if not actual or planned != actual or any(ip not in ("127.0.0.1", "::1") for _, ip, _ in actual):
        raise ValueError("Resolved ports differ from the running loopback endpoints")


def watch(container, image, seconds):
    since = datetime.now(timezone.utc)
    until = since + timedelta(seconds=seconds + 60)
    def healthy():
        status = run("docker", "inspect", "-f", "{{.Image}} {{.State.Health.Status}} {{.State.StartedAt}}", container).split()
        if status[:2] != [image, "healthy"]:
            raise RuntimeError("Canary image or health changed")
        return status[2]
    started = healthy()
    args = ["docker", "events", "--since", since.isoformat(), "--until", until.isoformat(),
            "--filter", "type=container", "--filter", f"container={container}", "--format", "{{.Action}}"]
    for event in ("health_status: unhealthy", "die", "stop", "restart", "pause", "oom", "destroy"):
        args.extend(["--filter", f"event={event}"])
    deadline = time.monotonic() + seconds
    with subprocess.Popen(args, stdout=subprocess.PIPE) as events:
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                if select.select([events.stdout], [], [], min(60, remaining))[0]:
                    raise RuntimeError("Canary lifecycle event or disconnected Docker event stream")
                if healthy() != started:
                    raise RuntimeError("Canary restarted during observation")
            if select.select([events.stdout], [], [], 0)[0]:
                raise RuntimeError("Canary event stream changed at the end of observation")
        finally:
            events.terminate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ports = commands.add_parser("ports")
    ports.add_argument("container")
    ports.add_argument("project")
    ports.add_argument("source", type=Path)
    ports.add_argument("image")
    soak = commands.add_parser("watch")
    soak.add_argument("container")
    soak.add_argument("image")
    soak.add_argument("seconds", type=int)
    args = parser.parse_args()
    if args.command == "ports":
        check_ports(args.container, args.project, args.source, args.image)
    elif args.seconds > 0:
        watch(args.container, args.image, args.seconds)
    else:
        parser.error("watch duration must be positive")


if __name__ == "__main__":
    main()
