"""Launch Warden using the Codex-specific Vaultwarden account."""

import json
import os
from pathlib import Path
import stat
import sys


def main():
    credentials, bw, node, npx = sys.argv[1:]
    try:
        with os.fdopen(os.open(credentials, os.O_RDONLY | os.O_NOFOLLOW)) as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError
            secrets = json.load(file)
        keys = {"BW_CLIENTID", "BW_CLIENTSECRET", "BW_PASSWORD"}
        if not isinstance(secrets, dict) or secrets.keys() != keys:
            raise ValueError
        if any(not isinstance(value, str) or not value or "\0" in value for value in secrets.values()):
            raise ValueError
    except (OSError, ValueError):
        raise SystemExit("Warden requires a private Codex credentials.json for a dedicated account.") from None

    os.umask(0o077)
    profiles = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "codex-warden"
    profiles.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("BW_", "KEYCHAIN_", "WARDEN_", "NODE_OPTIONS"))
    }
    env.update(secrets)
    env.update(
        BW_HOST="https://vault.amahi.dev",
        BW_BIN=bw,
        KEYCHAIN_BW_HOME_ROOT=str(profiles),
        KEYCHAIN_READONLY="true",
        KEYCHAIN_NOREVEAL="true",
        PATH=f"{Path(node).parent}{os.pathsep}{os.defpath}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    os.execve(node, [node, npx, "--yes", "@icoretech/warden-mcp@0.2.78", "--stdio"], env)


if __name__ == "__main__":
    main()
