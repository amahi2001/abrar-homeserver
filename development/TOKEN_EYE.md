# TokenEye on Buildfleet

`token-eye.nix`, imported by `configuration.nix`, declares the existing TokenEye
deployment for `buildfleet`: three commands, Headroom's user service, the two-day
health timer, its service, and user lingering. Scoped tmpfiles links keep the
existing `~/.local/bin`, `/opt/token-eye/bin/token-eye` and user-unit paths managed on subsequent NixOS rebuilds.
The existing development maintenance retry drop-in remains compatible.

The private checkout at `~/projects/token-eye`, existing project container/image,
and installed in-box CLI remain prerequisites. Nix wrappers call the tracked
private scripts at runtime; the public configuration and store do not contain
the application's source or credentials. This does not provision a fresh machine.

Automatic routing covers host and shared Claude Code and Codex accounts.
Ordinary new sessions need no route flags. The private compose overlay enables
the externally supervised routing daemon and reviewed account setup.
The older `token-eye-codex` shortcut follows saved settings and injects no routes
or retrieval MCP. Existing sign-in, model, hooks and native approvals are retained.
Custom routes are preserved and reported; explicit native routing overrides can
take precedence over the user default.

Settings → Headroom for Codex, or `token-eye routing off --apply`, restores unchanged
owned Claude/Codex settings before stopping the proxies. That off choice survives service and host
restarts; the externally supervised daemon stays idle. Restart open agent processes
after disabling, since they retain their initial connection. Re-enable with
`token-eye routing on --apply`.

Headroom uses stateless, lossless operation with payload logging, upload beacon
and subscription polling disabled. A locally generated private proxy header
protects `172.17.0.1:8787`, published only for host/container access. Tokens live
in private runtime/provider files and the child environment, never public Nix
source, status or previews. `token-eye-network-guard.service` runs after Docker,
before the lingering user manager, and reapplies on firewall reload. Its dedicated
DOCKER-USER chain permits Docker bridge origins and drops other forwarded origins
for ports 8787/8788; Docker DNAT bypasses ordinary host INPUT filtering. Four
narrow INPUT rules permit only Docker-bridge ingress to those ports on
172.17.0.1, allowing the serving box's own bridge to use Docker's userland proxy.
Claude's forward proxy on 8788 requires proxy authentication and uses a private
CA trusted only through the configured Claude environment. Only model messages
cross Headroom; control traffic remains direct. Private keys remain in app state,
and public CA bundles are copied into the relevant Claude account directories.
No LAN/Tailscale listener is added. Keep authentication enabled as a second check.

The proxy starts with the lingering user manager; it can resume only the existing
stopped project container. The timer checks 15 minutes after boot and then 48
hours after each completed run. It retains numeric counters and fixed outcomes
in the existing private dev-box state. No automatic paid test turn is added.
Intentional routing off is healthy; unexpected disconnection or route drift is
degraded. Proxy counters reset on restart and are not billed savings.

## Shared runtime and collectors

The tested headless CLI is staged privately at
`~/.local/state/token-eye-nixos/runtime/bin/token-eye`, alongside the owned
`deployment.json` marker `{"schema":1,"kind":"token-eye-server-runtime"}`.
The launcher validates ownership, permissions and canonical paths before adding
read-only `/opt/token-eye` and writable `/agent/token-eye/data` mounts to every
devbox. The data source remains the original TokenEye project-home app directory;
no existing state is moved. Native remote PATH prefixes the CLI while preserving
the image's tools. Missing deployment markers leave generic boxes unchanged;
conflicts preserve the existing box and require review.

Client boxes use read-only routing status mode for their own mounted account and
authenticated statistics from the bridge. Host accounts remain mounted only in
the serving box, where the daemon validates the full scope. Future boxes inherit
the deployment automatically. Stopped boxes retain their recorded state; refresh
running boxes through the native startup/session locks.

`token-eye claude-routing on` previews the two Claude proxy/CA changes. Apply is
explicit; custom routes and bypasses are preserved for review. Claude-only off
restores its owned settings. Master routing off restores Claude before stopping
the service. Persistent CA identity survives restarts; updates reuse the existing
image-owned Headroom and mitmdump installations.

Shared observation and Claude quota setup use the common app-data root. Host
collectors have separate receipts under `host-collectors`, and their callbacks
use the same permanent `/opt/token-eye/bin/token-eye` host launcher. Existing RTK
hooks remain. Codex's native `/hooks` review is required for new collectors;
configuration and a direct callback fixture do not establish trusted delivery.
The scheduled job requires both Headroom and Claude forwarding readiness.

On 2026-10-31 the same CLI, both configured routes and authenticated forwarding
were verified in all five boxes. Quiet synthetic callbacks reached the shared
private data directory without retaining prompt/tool fields. Original Git
branches and stopped/running state were restored. Credential and Codex settings
fingerprints remained unchanged. Application checks passed 65 frontend and 214
Rust tests, strict Clippy and isolated CLI/transport fixtures; canonical Nix
checks passed 52 development and 169 maintenance tests.

The final scoped generation is `q74f75gh37vpsadxcyahk47vn1777v0v`. Activation
reached it but returned status 4 after retrying the existing Nextcloud
maintenance-mode setup failure, also logged hours before this deployment.
Nextcloud configuration and state were left unchanged. Private deployment,
rollback and verification receipts live under
`~/.local/state/token-eye-nixos/deploy-20261031/`.

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

### Physical restart verification

Buildfleet was physically restarted on 2026-10-27. The boot ID changed and the
booted system matched the expected deployed generation. With no manual Headroom
startup or routing changes, the proxy, network guard and two-day timer became
active, and both account routes stayed configured. The same no-build flake check
passed after boot; unauthenticated gateway stats returned 401.

A private one-time verifier ran ordinary ephemeral Codex turns on the host and in
another existing project box. Each added two proxy requests and returned the
expected marker, without routing arguments or wrappers. TokenEye recorded four
requests, zero failures and 460 estimated tokens saved in the new proxy lifetime.
The automatic boot health check subsequently succeeded. Project containers use
no Docker restart policy: the TokenEye service resumed its existing box, and the
verifier resumed only the previously running test box under its native startup
lock. No box was recreated. The temporary verifier startup entry was removed
after verification; its private numeric report remains in
`~/.local/state/token-eye-restart-verification/report.json`.

For recovery, first apply `token-eye routing off --apply` and verify both accounts
are restored, then stop the proxy and timer. Restore only the saved TokenEye paths
from the archive, reload the user manager, and start the original units. Remove
the module import before a later rebuild if reverting declarative ownership;
otherwise tmpfiles intentionally restores the Nix-managed links. Existing
application data, account configuration and container volumes are preserved.
