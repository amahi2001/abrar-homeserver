"""Exercise generated service wrappers and upstream auth gates without starting services."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace


TOKEN = "hermes-startup-check-synthetic-token"
REFUSED = "public-url auth gate rejected missing provider"


def probe(role, command):
    # Imports and plugin discovery must remain offline, including subprocesses.
    def offline(event, _args):
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen"}:
            raise RuntimeError(f"Startup check attempted external work: {event}")

    sys.addaudithook(offline)
    assert command[1] == role, command
    from hermes_cli.plugins import discover_plugins

    discover_plugins()
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import InvalidCredentialsError, get_provider, list_providers

    try:
        if role == "gateway":
            from gateway.run_api import GatewayDashboardMount

            state = SimpleNamespace(bound_host="127.0.0.1", bound_port=0)
            GatewayDashboardMount(SimpleNamespace(), state)._bind_state(web_server)
        else:
            host = command[command.index("--host") + 1]
            web_server._configure_auth_gate(host, False, None, None)
    except SystemExit:
        if web_server.app.state.auth_required and not list_providers():
            print(REFUSED)
            return 2
        raise

    assert web_server.app.state.auth_required, "Public URL did not enable authentication"
    provider = get_provider("basic")
    assert provider is not None, "Bundled password provider was not registered"
    session = provider.complete_password_login(username="hermes", password=TOKEN)
    assert provider.verify_session(access_token=session.access_token) is not None
    try:
        provider.complete_password_login(username="hermes", password="wrong-password")
    except InvalidCredentialsError:
        pass
    else:
        raise AssertionError("Password provider accepted invalid credentials")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bash", required=True)
    parser.add_argument("--gateway", required=True, type=Path)
    parser.add_argument("--dashboard", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hermes-startup-check-") as directory:
        home = Path(directory)
        (home / "config.yaml").write_text("dashboard:\n  public_url: https://hermes-check.invalid\n")
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HERMES_HOME": directory,
            "HERMES_DASHBOARD_SESSION_TOKEN": TOKEN,
            "HERMES_CHECK_PYTHON": sys.executable,
            "HERMES_CHECK_SCRIPT": str(Path(__file__).resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # Run the exact wrapper. Substitute only its private env file and final
        # process launch; no service credentials are read and no listener starts.
        shell = r'''
set -euo pipefail
function . { [[ $# == 1 && "$1" == /var/lib/hermes/env ]]; }
function source { . "$@"; }
function exec {
  "$HERMES_CHECK_PYTHON" -P "$HERMES_CHECK_SCRIPT" --probe "$2" "$@"
}
builtin source "$1"
'''

        def run(wrapper):
            return subprocess.run(
                [args.bash, "--noprofile", "--norc", "-c", shell, "startup-check", str(wrapper)],
                env=environment, cwd=directory, capture_output=True, text=True, timeout=60,
            )

        for role, wrapper in (("gateway", args.gateway), ("dashboard", args.dashboard)):
            result = run(wrapper)
            assert result.returncode == 0, f"{role} startup auth failed:\n{result.stdout}{result.stderr}"
            print(f"{role}: public-URL gate and password/session verification passed")

        # Reproduce the original regression, rather than accepting a check that
        # passes only because another provider or ambient credentials masked it.
        broken = home / "gateway-without-auth"
        broken.write_text("\n".join(
            line for line in args.gateway.read_text().splitlines()
            if "export HERMES_DASHBOARD_BASIC_AUTH_" not in line
        ))
        result = run(broken)
        assert result.returncode == 2 and REFUSED in result.stdout, (
            f"Missing-provider negative control did not fail closed:\n{result.stdout}{result.stderr}"
        )
        print("gateway without auth exports: rejected by upstream public-URL gate")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--probe"]:
        sys.exit(probe(sys.argv[2], sys.argv[3:]))
    main()
