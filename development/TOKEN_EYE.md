# TokenEye on Buildfleet

`token-eye.nix`, imported by `configuration.nix`, declares the existing TokenEye
deployment for `buildfleet`: three commands, Headroom's user service, the two-day
health timer, its service, and user lingering. Scoped tmpfiles links keep the
existing `~/.local/bin` and user-unit paths managed on subsequent NixOS rebuilds.
The existing development maintenance retry drop-in remains compatible.

The private checkout at `~/projects/token-eye`, existing project container/image,
and installed in-box CLI remain prerequisites. Nix wrappers call the tracked
private scripts at runtime; the public configuration and store do not contain
the application's source or credentials. This does not provision a fresh machine.

Routing remains opt-in through `token-eye-codex` in the TokenEye box. Persisting
the deployment does not route ordinary Codex sessions or other project boxes.
The proxy starts with the lingering user manager; it can resume only the existing
stopped project container. The timer checks 15 minutes after boot and then 48
hours after each completed run. It retains numeric counters and fixed outcomes
in the existing private dev-box state. No automatic paid test turn is added.

## Scoped activation

The initial migration builds only the TokenEye artifacts, retaining them with a
persistent GC root:

```sh
nix build /etc/nixos#nixosConfigurations.buildfleet-server.config.system.build.tokenEyeDeployment \
  --out-link ~/.local/state/token-eye-nixos/current --no-write-lock-file
```

Only the eight TokenEye command/unit/enablement paths listed in `token-eye.nix`
are linked to that artifact. Original files were compared with the private
checkout before replacement, then backed up at
`~/.local/state/token-eye-nixos/pre-nix-20261025.tar.gz` with private permissions.
The redundant local `headroom.conf` dogfood drop-in was removed after comparison;
the declared service already requires proxy connectivity.

This migration does not switch the entire system generation: unrelated pending
host changes must be reviewed separately. The symlinks, retained Nix artifacts,
existing persistent home/container state and lingering account survive reboot
now. The next normal NixOS rebuild reproduces and updates the same named paths
from the module. Keep the GC root while this scoped deployment is in use.

Verify without starting a model:

```sh
loginctl show-user buildfleet -p Linger
systemctl --user status token-eye-headroom.service
systemctl --user list-timers token-eye-dogfood.timer
token-eye status
```

For recovery, stop the proxy and timer, restore only the saved TokenEye paths
from the archive, reload the user manager, and start the original units. Remove
the module import before a later rebuild if reverting declarative ownership;
otherwise tmpfiles intentionally restores the Nix-managed links. Existing
application data, account configuration and container volumes are preserved.
