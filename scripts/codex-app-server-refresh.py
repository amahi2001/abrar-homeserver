"""Retire stale SSH app servers only after checking their live work state.

The remote app reconnects through SSH and starts the newly deployed CLI. This
also runs independently of flake updates, so deferred restarts are retried.
"""

import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import time

import websocket


def log(message):
    print(f"[codex-app-server-refresh] {message}", flush=True)


def version_tuple(value):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise RuntimeError("Cannot compare an unknown Codex version; leaving server running")
    return tuple(map(int, match.groups()))


def daemon_status(codex):
    result = subprocess.run(
        [codex, "app-server", "daemon", "version"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return json.loads(result.stdout)


class Rpc:
    def __init__(self, socket_path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(str(socket_path))
            self.ws = websocket.create_connection(
                "ws://localhost/", socket=sock, timeout=10,
                origin="http://localhost",
            )
        except Exception:
            sock.close()
            raise
        self.request_id = 0
        try:
            self.call("initialize", {
                "clientInfo": {
                    "name": "codex_updater", "title": "Codex Updater", "version": "1.0",
                },
                "capabilities": {"experimentalApi": True},
            })
            self.ws.send(json.dumps({"method": "initialized", "params": {}}))
        except Exception:
            self.close()
            raise

    def close(self):
        self.ws.close()

    def call(self, method, params=None):
        self.request_id += 1
        self.ws.send(json.dumps({
            "id": self.request_id, "method": method, "params": params or {},
        }))
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out checking {method}; leaving server running")
            self.ws.settimeout(remaining)
            message = json.loads(self.ws.recv())
            if message.get("id") == self.request_id:
                if "error" in message:
                    # Avoid logging chat content or credentials from RPC error details.
                    raise RuntimeError(f"Cannot check {method}; leaving server running")
                return message["result"]


def server_idle(rpc):
    threads = rpc.call("thread/loaded/list")["data"]
    for thread_id in threads:
        thread = rpc.call("thread/read", {
            "threadId": thread_id, "includeTurns": False,
        })["thread"]
        if thread["status"]["type"] not in {"idle", "notLoaded"}:
            return False
        if thread["status"]["type"] == "idle":
            terminals = rpc.call("thread/backgroundTerminals/list", {
                "threadId": thread_id, "limit": 1,
            })
            if terminals["data"] or terminals.get("nextCursor"):
                return False
    return True


def listener_pid(socket_path):
    """Match the actual listening socket, never all processes named codex."""
    resolved = str(socket_path.resolve(strict=True))
    inodes = set()
    for row in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = row.split(maxsplit=7)
        if len(fields) == 8 and fields[7] == resolved and int(fields[3], 16) & 0x10000:
            inodes.add(f"socket:[{fields[6]}]")
    if not inodes:
        raise RuntimeError("Cannot identify the control socket listener; leaving server running")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            args = (proc / "cmdline").read_bytes().split(b"\0")
            if b"app-server" not in args or b"proxy" in args or b"daemon" in args:
                continue
            if Path(os.readlink(proc / "exe")).name != "codex":
                continue
            if any(os.readlink(fd) in inodes for fd in (proc / "fd").iterdir()):
                return int(proc.name)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    raise RuntimeError("Cannot identify this user's app server; leaving it running")


def exited(pidfd, seconds):
    poll = select.poll()
    poll.register(pidfd, select.POLLIN)
    return bool(poll.poll(int(seconds * 1000)))


def retire_server(rpc, socket_path):
    pid = listener_pid(socket_path)
    # pidfd prevents a reused PID from targeting an unrelated process.
    pidfd = os.pidfd_open(pid)
    try:
        if exited(pidfd, 0):
            return True
        if listener_pid(socket_path) != pid or not server_idle(rpc):
            return False
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        if not exited(pidfd, 5):
            # Some SSH-launched servers ignore TERM. Recheck activity before
            # retiring that exact process, and defer if the check fails.
            if not server_idle(rpc):
                return False
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            if not exited(pidfd, 5):
                raise RuntimeError("Old app server did not exit")
        return True
    finally:
        os.close(pidfd)


def refresh(codex, socket_path, check_only=False):
    status = daemon_status(codex)
    if status["status"] != "running":
        log("No app server is running; the next SSH connection will use the installed CLI.")
        return
    cli = version_tuple(status["cliVersion"])
    server = version_tuple(status["appServerVersion"])
    if server >= cli and not check_only:
        log(f"App server {status['appServerVersion']} is current; no restart needed.")
        return
    rpc = Rpc(socket_path)
    try:
        idle = server_idle(rpc)
        if check_only:
            log(f"CLI {status['cliVersion']}; server {status['appServerVersion']}; "
                f"{'idle' if idle else 'busy'}; {'refresh pending' if server < cli else 'current'}.")
            return
        if not idle:
            log("Update pending; a chat or background terminal is active. Retrying in five minutes.")
            return
        # Re-read versions to avoid stopping a replacement that already updated.
        latest = daemon_status(codex)
        if latest["status"] != "running" or version_tuple(latest["appServerVersion"]) >= cli:
            return
        pid = listener_pid(socket_path)
        managed_path = latest.get("managedCodexPath")
        managed = managed_path and Path(managed_path).exists() and (
            Path(f"/proc/{pid}/exe").resolve() == Path(managed_path).resolve()
        )
        if managed:
            if not server_idle(rpc):
                log("Update deferred; new work started. Retrying in five minutes.")
                return
            subprocess.run(
                [codex, "app-server", "daemon", "update", "--from-cli", "--yes"],
                check=True, capture_output=True, timeout=60,
            )
        elif not retire_server(rpc, socket_path):
            log("Update deferred; new work started. Retrying in five minutes.")
            return
    finally:
        rpc.close()
    # SSH clients reconnect automatically. If the app is closed, leave the
    # server stopped rather than launch it with a different SSH environment.
    for attempt in range(10):
        latest = daemon_status(codex)
        if latest["status"] == "running":
            if version_tuple(latest["appServerVersion"]) >= cli:
                log(f"Remote app server is now {latest['appServerVersion']}.")
                return
        time.sleep(1)
    if latest["status"] != "running":
        log("Old server retired; the updated CLI is ready for the next SSH connection.")
        return
    raise RuntimeError("Remote app reconnected with an outdated CLI; check its SSH launch command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="/run/current-system/sw/bin/codex")
    parser.add_argument("--socket", type=Path, default=Path.home() / ".codex/app-server-control/app-server-control.sock")
    parser.add_argument("--check", action="store_true", help="Report state without restarting anything")
    args = parser.parse_args()
    try:
        refresh(args.codex, args.socket, args.check)
    except Exception as error:
        reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        log(f"Refresh failed: {reason}. Retrying on the next timer.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
