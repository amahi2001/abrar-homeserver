# buildfleet-server

Declarative NixOS configuration for the BuildFleet home server. The repository
root is the live flake: `/etc/nixos#buildfleet-server`.

## Services

| Service | Purpose | Exposure |
| --- | --- | --- |
| Hermes Agent | Telegram/CLI automation and media workflow | Outbound only |
| Hermes Dashboard | Hermes Desktop web/TUI endpoint | Tailscale `:9119` |
| Cockpit | Server administration | Tailscale `:9090` |
| Nextcloud | File storage, sync, sharing, and WebDAV | Public HTTPS `cloud.amahi.dev` |
| Vaultwarden | Password manager | Public HTTPS `vault.amahi.dev` |
| Jellyfin | Movies and TV playback | Public HTTPS `jellyfin.amahi.dev`; LAN and Tailscale `:8096` |
| AdGuard Home | Network DNS filtering and admin UI | DNS `:53`, LAN UI `:8080` |
| Gluetun + Transmission | VPN-isolated downloads with a fail-closed network namespace | RPC on server loopback `:9091` |
| Prowlarr | Search/indexer aggregation for Hermes | Tailscale `:9696` |
| Sonarr / Radarr | On-demand TV/movie organization | Tailscale `:8989` / `:7878` while running |
| Samba + WSD | Read-only media and writable inbox shares | Home Wi-Fi and Tailscale `:445` |
| Tailscale | Remote administration and exit-node routing | Tailnet |
| XFCE + Bluetooth | Local recovery console | Starts only when requested |

Cloudflare DNS-only CNAMEs for `vault.amahi.dev`, `cloud.amahi.dev`, and
`jellyfin.amahi.dev` point to `buildfleet.duckdns.org`. DuckDNS continues updating
the home IP. Old Nextcloud and Vaultwarden browser links redirect to their new
domains. Update Nextcloud and password-manager clients to use the new HTTPS
URLs directly. The old Jellyfin hostname remains a compatibility alias; new
clients should use `https://jellyfin.amahi.dev`.

Jellyfin's runtime network settings in `/var/lib/jellyfin/config/network.xml`
trust `127.0.0.1` as the Nginx proxy and publish the server URL from each
request. This lets remote clients receive the public HTTPS URL while LAN and
Tailscale clients continue using their existing addresses. Port 8096 remains
restricted to the home Wi-Fi and Tailscale interfaces.

Transmission shares Gluetun's network namespace and cannot reach the network
without Gluetun's VPN firewall. Its UI is intentionally loopback-only; access
it remotely with an SSH tunnel. Completed manual downloads land under
`/srv/data/media/library/manual` and are exposed by the authenticated
`Media` Samba share.

Sonarr and Radarr are stopped while idle. Hermes starts them only for explicit
library-management work, imports with verified hard links so Transmission can
continue seeding, and stops them afterward.

## Agent development environments

Use `dev start <project>` to launch Codex inside a persistent Linux container,
or add `--agent claude`. New unassigned projects receive private GitHub repos;
agents work and push on task branches. See [development/README.md](development/README.md)
for login, previews, customization, and verification. Personal skills, hooks, and
portable Codex/Claude settings are synced from the separate private
`amahi2001/agent-config` repository; its local snapshot is applied on container startup.

## Repository layout

```text
.
├── flake.nix / flake.lock       # Inputs, formatter, and system build check
├── configuration.nix            # Host, users, packages, SSH, firewall
├── hardware-configuration.nix   # Machine hardware and filesystems
├── desktop.nix                  # Headless default, XFCE, NVIDIA, Bluetooth
├── containers.nix               # Docker and guarded AdGuard updates
├── files.nix                    # Nextcloud, Samba, Gluetun, Transmission
├── servarr.nix                  # Prowlarr, FlareSolverr, Sonarr, Radarr
├── hermes.nix                   # Hermes agent, dashboard, tools, and skills
├── tailscale.nix                # Tailnet and exit-node configuration
├── update.nix                   # Guarded flake updates, upgrade, GC
├── web.nix                      # DuckDNS, ACME, Nginx, Vaultwarden
├── skills/media-queue/          # Hermes media search/queue/library workflow
├── scripts/                     # Operational helpers
└── secrets/example/             # Safe templates only
```

Runtime state and credentials do not belong in Git. They live under
`/var/lib`, principally:

- `/var/lib/secrets/` for VPN, Nextcloud, DuckDNS, and sync credentials
- `/var/lib/hermes/env` for Hermes provider and messaging credentials
- `/var/lib/gluetun/` and `/var/lib/transmission-vpn/` for download services
- `/srv/data/` for files and media

## JavaScript runtime

Bun 1.4.4 and the fixed Node release lines are minimum security versions in
`configuration.nix` and `runtime-security.nix`. Newer nixpkgs packages take precedence. Development-image
Node and Bun pins advance through the guarded system updater.

Use `bun run script.ts` for local scripts and `bunx --bun <npm-package>` for
package executables. The `--bun` flag makes Node shebangs use Bun. Deno and
the `dx` wrapper are removed; Deno-specific URLs, JSR imports, and runtime
APIs must be ported before running them with Bun.

The daily vault sync runs an immutable Nix-store copy of
`scripts/bitwarden-sync.ts` using Bun. Its existing direction is
**Vaultwarden to Bitwarden**, including deletion of tracked Bitwarden items
that were removed from Vaultwarden. The timer and credentials are unchanged.
Profile state defaults to `/var/lib/bitwarden-sync`; the
`BITWARDEN_SYNC_STATE_DIR` override supports isolated testing.

`bun test scripts/bitwarden-sync.test.ts` exercises the sync with a fake `bw`
and synthetic credentials; it never connects to either vault. These tests
also run in the Nix build sandbox as part of `nix flake check`. Existing Node
dependencies such as the packaged Bitwarden CLI and Warden MCP remain on
their declared runtimes.

## Deploy and validate

```bash
cd /etc/nixos

# Format, evaluate/build the flake check, then deploy.
nix fmt
nix flake check
sudo nixos-rebuild switch --flake /etc/nixos#buildfleet-server
```

The host `/` and `/run` are writable. Hermes gateway commands see a separate
read-only systemd sandbox (`ProtectSystem=strict`), with explicit writable media
paths for subtitle sidecars. Never remount `/` or `/run` to bypass it. From a
gateway session, run activation through the host system manager:

```bash
sudo systemd-run --wait --pipe --collect \
  /run/current-system/sw/bin/nixos-rebuild switch --flake /etc/nixos#buildfleet-server
```

`hermes.nix` owns the media allowlist and exposes Jellyfin's existing `ffprobe`
on the gateway PATH. The repository owns `skills/media-subtitles`; its runtime
skill is a symlink. See `AGENTS.md` for namespace checks and restart guidance.

For a non-deploying system validation:

```bash
sudo nixos-rebuild dry-build --flake /etc/nixos#buildfleet-server
```

## Change tracking

Make administrator changes on a short-lived branch and review them in a pull
request. Before pushing:

```bash
git diff --check
nix flake check
nix run nixpkgs#gitleaks -- dir --redact --no-banner .
```

Automatic updates build and test an isolated copy of the last deployed flake.
Uncommitted administrator changes remain separate. System inputs deploy weekly;
Codex deploys daily at 04:30–04:45 Eastern. Stateful application changes receive
quiesced backups, service health checks, and recovery on failure. Docker upgrades
also protect active CI and dev work. See [automatic maintenance and recovery](UPDATES.md)
for schedules, coverage, retained state, and external update limits.

`codex-app-server-refresh` checks the remote app server every five minutes.
An outdated server is retired only after checking that its chats are idle and
no background terminals are running. The app reconnects through SSH using the
installed CLI. Busy servers stay running and are checked again on the next
timer; this also catches CLI upgrades applied manually or by the weekly job.
Failures are visible in `journalctl -u codex-cli-update -u codex-app-server-refresh`.

`codex-remote-control` checks Remote Control 30 seconds after boot and every
30 seconds afterward. It uses the existing private app-server socket, preserves
connected servers, and enables Remote Control through the native protocol when
disabled or errored. Only an absent server uses `codex remote-control start`.
This avoids the managed-daemon ownership error for desktop/SSH-owned servers
without restarting chats or creating a competing server. Successful runs verify
the native connection reaches `connected`; failures remain visible in the
service journal. Logs contain fixed outcomes, never pairing codes or account/
device identifiers. The timer and helper are declared in `update.nix`.

```bash
systemctl status codex-remote-control.service codex-remote-control.timer
journalctl -u codex-remote-control.service -n 20
```

The repository is public. Never place real credentials in examples, commits,
issues, or pull-request text. The ignore rules are a safety net, not a substitute
for the secret scan.

## Recovery

- Start the local GUI: `sudo systemctl isolate graphical.target`
- Return to headless mode: `sudo systemctl isolate multi-user.target`
- Roll back a deployment: select an older generation in GRUB or run
  `sudo nixos-rebuild switch --rollback`
- Inspect services: `systemctl status hermes-agent docker-gluetun docker-transmission-vpn prowlarr`
- Inspect timers: `systemctl list-timers --all`

## License

This configuration is provided as-is for reference. Adapt it to your own
hardware, network, accounts, and threat model.
