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

Automatic routing now covers the host Codex account and the shared account mounted
in project boxes. Ordinary new Codex sessions need no wrapper. The private compose
overlay enables the externally supervised routing daemon and automatic first setup.
The older `token-eye-codex` shortcut follows saved settings and injects no routes
or retrieval MCP. Existing sign-in, model, hooks and native approvals are retained.
Custom routes are preserved and reported; explicit native routing overrides can
take precedence over the user default.

Settings → Headroom for Codex, or `token-eye routing off --apply`, restores unchanged
owned settings and stops the proxy. That off choice survives service and host
restarts; the externally supervised daemon stays idle. Restart open Codex processes
after disabling, since they retain their initial connection. Re-enable with
`token-eye routing on --apply`.

Headroom uses stateless, lossless operation with payload logging, upload beacon
and subscription polling disabled. A locally generated private proxy header
protects `172.17.0.1:8787`, published only for host/container access. Tokens live
in private runtime/provider files and the child environment, never public Nix
source, status or previews. `token-eye-network-guard.service` runs after Docker,
before the lingering user manager, and reapplies on firewall reload. Its dedicated
DOCKER-USER chain permits Docker bridge origins and drops other forwarded origins
for that destination/port; Docker DNAT bypasses ordinary host INPUT filtering.
No LAN/Tailscale listener is added. Keep authentication enabled as a second check.

The proxy starts with the lingering user manager; it can resume only the existing
stopped project container. The timer checks 15 minutes after boot and then 48
hours after each completed run. It retains numeric counters and fixed outcomes
in the existing private dev-box state. No automatic paid test turn is added.
Intentional routing off is healthy; unexpected disconnection or route drift is
degraded. Proxy counters reset on restart and are not billed savings.

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

The initial migration did not switch the entire system generation. On 2026-10-27,
automatic routing was deployed with a full `nixos-rebuild switch` from a clean copy
of the running `/etc/nixos-deployed` source plus only `token-eye.nix` and the new
gateway guard script. The baseline toplevel matched `/run/current-system`;
closure/dry-activation review showed only the TokenEye guard/package and named
link/firewall changes. Unrelated pending `/etc/nixos` edits were excluded.
This generation persists on reboot without requiring a later rebuild. Persistent
home/account volumes retain routing ownership and the saved toggle. The private
checkout and existing box remain required. Use the host deployment lock for
subsequent activation and do not include unrelated edits accidentally.

Verify without starting a model:

```sh
loginctl show-user buildfleet -p Linger
systemctl status token-eye-network-guard.service
sudo iptables -S TOKEN-EYE-PROXY
systemctl --user status token-eye-headroom.service
systemctl --user list-timers token-eye-dogfood.timer
token-eye status
token-eye routing
```

Over SSH, if the bus is absent, set `XDG_RUNTIME_DIR=/run/user/1000` and
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus` for user-manager commands.
Do not dump private Codex config/runtime tokens. Compare numeric counters around
an ordinary Codex turn to establish traffic; a configured route alone is no proof.

### Evaluating security grafts

On Nix 2.34.9, `nix flake check --no-build` enables read-only evaluation. This
configuration's runtime security grafts use an imported derivation to inspect
references. On an uncached source tree, the check can calculate a `references.nix.drv`
path without registering it, then fail to import it as "not valid". This is not
evidence of store corruption. Materialize the derivations first, then check:

```sh
nix eval --raw --option eval-cache false \
  .#nixosConfigurations.buildfleet-server.config.system.build.toplevel.drvPath >/dev/null
nix flake check --no-build --no-write-lock-file
```

The first command may build security-graft prerequisites; it does not activate
the system. Normal `nix flake check` also permits that work and builds all checks.
Do not remove security grafts or delete caches/store data to work around this.
The previously missing derivation was generated and the same no-build check
passed on 2026-10-27. See [Nix's check mode](https://github.com/NixOS/nix/blob/2.34.9/src/nix/flake.cc#L324-L329).

For recovery, first apply `token-eye routing off --apply` and verify both accounts
are restored, then stop the proxy and timer. Restore only the saved TokenEye paths
from the archive, reload the user manager, and start the original units. Remove
the module import before a later rebuild if reverting declarative ownership;
otherwise tmpfiles intentionally restores the Nix-managed links. Existing
application data, account configuration and container volumes are preserved.
