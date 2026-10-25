#!/usr/bin/env python3
"""Refresh Hermes-owned MCP dependencies only while its gateway is idle."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


HERMES_HOME = Path("/var/lib/hermes/.hermes")
SERVICES = ("hermes-dashboard.service", "hermes-agent.service")
GOOGLE_ROOTS = ("google-api-python-client", "google-auth-oauthlib")
PYTHON_SMOKE = r'''
import os, re, ssl, sys
assert sys.version_info[:2] == (3, 12)
assert os.path.samefile(sys._base_executable, os.environ["UPGRADE_PYTHON"])
assert tuple(map(int, re.search(r"(\d+)\.(\d+)\.(\d+)", ssl.OPENSSL_VERSION).groups())) >= (3, 6, 5), ssl.OPENSSL_VERSION
'''
SMOKE = PYTHON_SMOKE + r'''
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import distributions
import threading
import cloakbrowser, cloakbrowsermcp, playwright
from cryptography.hazmat.backends.openssl.backend import backend
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

crypto_ssl = backend.openssl_version_text()
assert tuple(map(int, re.search(r"(\d+)\.(\d+)\.(\d+)", crypto_ssl).groups())) >= (4, 0, 3), crypto_ssl
installed = {d.metadata["Name"].lower().replace("_", "-") for d in distributions()}
if "google-api-python-client" in installed:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    api = build("gmail", "v1", credentials=Credentials(token="maintenance-smoke"),
                static_discovery=True, cache_discovery=False)
    assert callable(api.users)
    api.close()
if "google-auth-oauthlib" in installed:
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config({"installed": {
        "client_id": "maintenance-smoke", "client_secret": "maintenance-smoke",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"]}}, scopes=["openid"])
    assert flow.oauth2session.client_id == "maintenance-smoke"
class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b"<!doctype html><title>MCP maintenance</title>"
                b"<button id=\"proof\" onclick=\"this.textContent='clicked'\">ready</button>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

with ThreadingHTTPServer(("127.0.0.1", 0), PageHandler) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser = cloakbrowser.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_default_timeout(10000)
        response = page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
        assert response.status == 200
        assert page.title() == "MCP maintenance"
        assert page.evaluate("document.querySelector('#proof').textContent") == "ready"
        page.locator("#proof").click()
        assert page.evaluate("document.querySelector('#proof').textContent") == "clicked"
    finally:
        browser.close()
        server.shutdown()

async def check(command, args):
    params = StdioServerParameters(command=command, args=args, env=dict(os.environ))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            assert (await session.list_tools()).tools

async def main():
    await check(os.environ["WARDEN_COMMAND"], ["--stdio"])
    await check(str(__import__("pathlib").Path(sys.executable).parent / "cloakbrowsermcp"), ["--caps", "all"])

asyncio.run(asyncio.wait_for(main(), timeout=90))
'''


def run(*args, timeout=120):
    # Capture private manager/server output without publishing credentials.
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout).stdout.strip()


def as_owner(*args, timeout=120):
    return run("runuser", "-u", "hermes", "--", *map(str, args), timeout=timeout)


def warden_npm(*args, timeout=600):
    npm = os.environ["NPM_BIN"]
    return as_owner("env", f"PATH={Path(npm).parent}:{os.environ['PATH']}",
                    npm, "--engine-strict", *args, timeout=timeout)


def status():
    return json.loads(run("curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                          "http://127.0.0.1:9119/api/status"))


def require_idle():
    current = status()
    # Hermes' NAS lifecycle contract counts in-flight work; active_sessions is five-minute history.
    if not (current.get("gateway_running") is True
            and current.get("gateway_busy") is False
            and current.get("gateway_drainable") is True
            and current.get("active_agents") == 0):
        raise RuntimeError("Hermes is busy or its idle state is unknown; retry maintenance when idle")


def resume(services):
    if not services:
        return
    run("systemctl", "start", *reversed(services))
    for attempt in range(30):
        try:
            run("systemctl", "is-active", "--quiet", *services)
            if "hermes-agent.service" not in services or status().get("gateway_running") is True:
                return
        except (ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        if attempt < 29:
            time.sleep(2)
    raise RuntimeError("Hermes did not recover after MCP maintenance")


def refresh(state_dir=Path("/var/lib/hermes-mcp-updates")):
    services = []
    for service in SERVICES:
        state = run("systemctl", "show", "--property=ActiveState", "--value", service)
        if state == "active":
            services.append(service)
        elif state not in ("inactive", "failed"):
            raise RuntimeError("Hermes service is transitioning; retry maintenance later")
    if services:
        require_idle()
    directories = [HERMES_HOME / "warden-mcp", HERMES_HOME / ".venv"]
    backup = Path(tempfile.mkdtemp(prefix="backup-", dir=state_dir))
    snapshots = []
    stopped = False
    resume_attempted = False
    try:
        if services:
            require_idle()
            stopped = True
            run("systemctl", "stop", *services)
        for index, directory in enumerate(directories):
            destination = backup / str(index)
            run("cp", "--archive", "--reflink=auto", str(directory), str(destination))
            snapshots.append((directory, destination))
        venv = directories[1]
        # Capture optional roots before recreating this dedicated MCP environment.
        installed = {package["name"].lower().replace("_", "-") for package in json.loads(
            as_owner(venv / "bin/python", "-m", "pip", "list", "--format=json",
                     "--disable-pip-version-check"))}
        roots = ["cloakbrowsermcp", "cloakbrowser", "playwright"]
        roots.extend(root for root in GOOGLE_ROOTS if root in installed)
        # --upgrade assumes an in-place Python upgrade; Nix has distinct store paths.
        as_owner(os.environ["UPGRADE_PYTHON"], "-m", "venv", "--clear", venv)
        as_owner(venv / "bin/python", "-c", PYTHON_SMOKE)
        warden_npm("install", "--prefix", directories[0], "@icoretech/warden-mcp@latest")
        warden_npm("update", "--prefix", directories[0])
        as_owner(venv / "bin/python", "-m", "pip", "install", "--upgrade", "pip", timeout=600)
        as_owner(venv / "bin/python", "-m", "pip", "install", "--upgrade",
                 "--upgrade-strategy", "eager", *roots, timeout=600)
        as_owner(venv / "bin/python", "-m", "pip", "check")
        as_owner(venv / "bin/python", "-c", SMOKE, timeout=180)
        resume_attempted = bool(services)
        resume(services)
    except BaseException as error:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if resume_attempted:
            try:
                require_idle()
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                message = ("MCP rollback needs attention: the restarted gateway has active or unknown work. "
                           "Updated packages and services were left intact; "
                           f"backup retained at {backup}")
                print(message, file=sys.stderr, flush=True)
                raise RuntimeError(message) from error
        print(f"MCP update failed; restoring manager directories from {backup}", flush=True)
        if stopped:
            run("systemctl", "stop", *services)
        for directory, source in snapshots:
            if directory.exists():
                shutil.rmtree(directory)
            run("cp", "--archive", "--reflink=auto", str(source), str(directory))
        resume(services)
        raise
    shutil.rmtree(backup)
    print("MCP dependencies updated; browser, stdio and installed Google API checks passed", flush=True)


def interrupted(signum, frame):
    raise RuntimeError(f"Maintenance interrupted by signal {signum}")


def main():
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with Path("/run/lock/nixos-flake-update.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refresh()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"MCP maintenance failed ({type(error).__name__}); inspect the retained backup and service state.",
              file=sys.stderr)
        raise SystemExit(1) from None
