"""Keep Remote Control connected without replacing an existing app server."""

import argparse
import importlib.util
from pathlib import Path
import subprocess
import time


spec = importlib.util.spec_from_file_location(
    "codex_remote_control_refresh", Path(__file__).with_name("codex-app-server-refresh.py"),
)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

STATES = {"disabled", "connecting", "connected", "errored"}


def log(message):
    print(f"[codex-remote-control] {message}", flush=True)


def connection_status(response):
    if not isinstance(response, dict) or not isinstance(response.get("status"), str) or response["status"] not in STATES:
        raise RuntimeError("Unknown Remote Control state")
    return response["status"]


def ensure_remote_control(codex, socket_path, check_only=False):
    try:
        rpc = refresh.Rpc(socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
        if check_only:
            log("No app server is running.")
            return "stopped"
        try:
            subprocess.run(
                [codex, "remote-control", "start", "--json"],
                check=True, capture_output=True, timeout=45,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # An SSH client can claim the socket while native startup is running.
            pass
        try:
            rpc = refresh.Rpc(socket_path)
        except Exception:
            raise RuntimeError("Remote Control startup failed") from None
    try:
        state = connection_status(rpc.call("remoteControl/status/read", None))
        if check_only:
            log(f"Remote Control is {state}.")
            return state
        if state in {"disabled", "errored"}:
            state = connection_status(rpc.call("remoteControl/enable", {"ephemeral": False}))
        deadline = time.monotonic() + 30
        while state == "connecting":
            # Reserve the shared RPC client's ten-second timeout within this budget.
            if time.monotonic() + 11 > deadline:
                raise RuntimeError("Remote Control did not connect in time")
            time.sleep(1)
            state = connection_status(rpc.call("remoteControl/status/read", None))
        if state != "connected":
            raise RuntimeError("Remote Control is not connected")
        log("Remote Control is connected.")
        return state
    finally:
        rpc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="/run/current-system/sw/bin/codex")
    parser.add_argument(
        "--socket", type=Path,
        default=Path.home() / ".codex/app-server-control/app-server-control.sock",
    )
    parser.add_argument("--check", action="store_true", help="Inspect connection without changing it")
    args = parser.parse_args()
    try:
        state = ensure_remote_control(args.codex, args.socket, args.check)
    except Exception:
        # RPC errors and native command output may contain account/device details.
        log("Remote Control could not be verified; retrying on the next timer.")
        return 1
    return 0 if state == "connected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
