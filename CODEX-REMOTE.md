# Codex Remote Control

`codex-remote-control.timer` starts Codex Remote Control after boot and checks it
every 30 seconds. It restores access if the daemon exits or Remote Control is
disabled. The service runs as `buildfleet` through the login shell, using the
existing account, paired devices, and credentials in `/home/buildfleet/.codex`.

The service uses the supported `codex remote-control start --json` command from
the installed CLI. Codex retains ownership of its daemon packages and settings;
NixOS supplies boot startup and recovery. `KillMode=process` prevents systemd
from killing Codex's detached daemon when the start command finishes. Repeated
checks reuse the running daemon and preserve active work when Remote Control is
already enabled. Re-enabling a disabled daemon can restart it.

Inspect the service and timer:

```bash
systemctl status codex-remote-control.timer
journalctl -u codex-remote-control -n 20
codex app-server daemon version
```

The existing guarded updater still handles CLI upgrades. The recovery service
uses `/run/current-system/sw/bin/codex`, so it follows the deployed version.
No separate package installation or update channel is required.

To intentionally disable persistent access, remove the service and timer from
`update.nix` and rebuild NixOS before running
`codex app-server daemon disable-remote-control`. Disabling it only in Codex will
be reversed by the next timer check. Existing device pairings are preserved.

This restores the daemon and its enable setting; account sign-in, network
connectivity, and OpenAI service availability are still required.

Official command documentation:
<https://learn.chatgpt.com/docs/cli/reference#codex-remote-control>
