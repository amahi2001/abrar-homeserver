# buildfleet-server

Declarative NixOS configuration for the BuildFleet home server. The repository
root is the live flake: `/etc/nixos#buildfleet-server`.

## Services

| Service | Purpose | Exposure |
| --- | --- | --- |
| Hermes Agent | Telegram/CLI automation and media workflow | Outbound only |
| Hermes Dashboard | Hermes Desktop web/TUI endpoint | Tailscale `:9119` |
| Cockpit | Server administration | Tailscale `:9090` |
| Nextcloud | File storage, sync, sharing, and WebDAV | Public HTTPS root |
| Vaultwarden | Password manager | Public HTTPS `/vault/` |
| AdGuard Home | Network DNS filtering and admin UI | DNS `:53`, LAN UI `:8080` |
| Gluetun + Transmission | VPN-isolated downloads with a fail-closed network namespace | RPC on server loopback `:9091` |
| Prowlarr | Search/indexer aggregation for Hermes | Tailscale `:9696` |
| Sonarr / Radarr | On-demand TV/movie organization | Tailscale `:8989` / `:7878` while running |
| Samba + WSD | Read-only media and writable inbox shares | Home Wi-Fi and Tailscale `:445` |
| Tailscale | Remote administration and exit-node routing | Tailnet |
| XFCE + Bluetooth | Local recovery console | Starts only when requested |

Transmission shares Gluetun's network namespace and cannot reach the network
without Gluetun's VPN firewall. Its UI is intentionally loopback-only; access
it remotely with an SSH tunnel. Completed manual downloads land under
`/srv/data/media/library/manual` and are exposed by the authenticated
`Media` Samba share.

Sonarr and Radarr are stopped while idle. Hermes starts them only for explicit
library-management work, imports with verified hard links so Transmission can
continue seeding, and stops them afterward.

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

## Deploy and validate

```bash
cd /etc/nixos

# The host mounts these paths read-only by default.
sudo mount -o remount,rw /
sudo mount -o remount,rw /run

# Format, evaluate/build the flake check, then deploy.
nix fmt
nix flake check
sudo nixos-rebuild switch --flake /etc/nixos#buildfleet-server
```

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

Automatic input updates run only when the live checkout is on a clean `main`
branch. Each updater validates the resulting system before creating one
lock-file-only commit. It skips review branches and dirty worktrees, preventing
timer-generated commits from mixing with manual server work.

The repository is public. Never place real credentials in examples, commits,
issues, or pull-request text. The ignore rules are a safety net, not a substitute
for the secret scan.

## Recovery

- Start the local GUI: `sudo systemctl isolate graphical.target`
- Return to headless mode: `sudo systemctl isolate multi-user.target`
- Roll back a deployment: select an older generation in GRUB or run
  `sudo nixos-rebuild switch --rollback`
- Inspect services: `systemctl status hermes-agent docker-gluetun docker-transmission-vpn prowlarr`
- Inspect timers: `systemctl list-timers codex-cli-update nixos-flake-update nixos-upgrade`

## License

This configuration is provided as-is for reference. Adapt it to your own
hardware, network, accounts, and threat model.
