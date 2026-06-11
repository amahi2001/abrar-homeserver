# buildfleet-server 🖥️

Declarative NixOS home server configuration. Fully reproducible — one `nixos-rebuild switch` away from a complete setup.

## What's Running

| Service | Purpose | Port |
|---------|---------|------|
| **Hermes Agent** | AI assistant (Telegram bot + dashboard) | — (outbound) |
| **Hermes Dashboard** | Web/TUI chat interface for Desktop app | 9119 (Tailscale) |
| **AdGuard Home** | Network-wide DNS ad blocking | 53, 8080 |
| **Nginx** | TLS reverse proxy | 443 |
| **Tailscale** | Mesh VPN + exit node | — |
| **DuckDNS** | Dynamic DNS for `buildfleet.duckdns.org` | — |
| **XFCE Desktop** | Lightweight GUI for local access | — |

## Architecture

```
Internet
  │
  ├── buildfleet.duckdns.org ──► Nginx (TLS/ACME)
  │                                │
  │                                └── (future: reverse proxy targets)
  │
  ├── DNS queries ───────────────► AdGuard Home (:53, :8080)
  │
  └── Tailscale mesh ───────────► Dashboard (:9119, Tailscale only)
                                   Exit node for roaming devices
```

## Repo Structure

```
├── nixos/                        # NixOS configuration modules
│   ├── flake.nix                 # Flake inputs (nixpkgs, hermes-agent, cloakbrowser)
│   ├── flake.lock                # Pinned input versions
│   ├── configuration.nix         # Main config (packages, users, firewall, nix-ld)
│   ├── hardware-configuration.nix# Auto-generated hardware config
│   ├── hermes.nix                # Hermes Agent + Dashboard service config
│   ├── containers.nix           # Docker + AdGuard Home container
│   ├── desktop.nix               # XFCE desktop + NVIDIA GPU + Bluetooth
│   ├── tailscale.nix             # Tailscale VPN exit node
│   ├── update.nix                # Auto-update: flake update + auto-upgrade + GC
│   └── web.nix                   # DuckDNS + ACME + Nginx
├── adguard/
│   ├── AdGuardHome.yaml.example  # AdGuard config template (passwords redacted)
│   └── README.md                 # AdGuard setup notes
├── hermes/
│   └── config.yaml.example       # Hermes Agent runtime config template
├── scripts/
│   ├── gh-env.sh                 # Export GITHUB_TOKEN for shell sessions
│   └── gh-token.sh               # Extract GitHub token from git-credentials
├── secrets/example/              # Example env files (copy & fill in real values)
│   ├── hermes.env.example        # All secret env vars for Hermes Agent
│   ├── hermes-dashboard.env.example # Dashboard env vars
│   ├── duckdns-token.txt.example # DuckDNS token
│   ├── duckdns-token-value.example  # DuckDNS token (ACME challenge)
│   ├── duckdns.secret.example    # DuckDNS token (NixOS module)
│   └── google_client_secret.json.example # Google OAuth credentials
└── .gitignore
```

## Quick Start (New Machine)

### 1. Install NixOS

Follow the [NixOS manual](https://nixos.org/manual/nixos/stable/#sec-installation) to get a base system running, then:

```bash
# Clone this repo
sudo git clone https://github.com/amahi2001/buildfleet-server.git /etc/nixos
cd /etc/nixos

# Review and edit hardware-configuration.nix for your hardware
# (NixOS generates this on install — you may want to keep your own)
```

### 2. Set Up Secrets

```bash
# Create secrets directory
sudo mkdir -p /var/lib/secrets

# Copy and fill in each example file
for f in secrets/example/*.example; do
  realname=$(basename "$f" .example)
  sudo cp "$f" "/var/lib/secrets/$realname"
  sudo chmod 600 "/var/lib/secrets/$realname"
done
# Edit each file with your real values
sudo vim /var/lib/secrets/*

# Hermes env files
sudo cp secrets/example/hermes.env.example /var/lib/hermes/env
sudo cp secrets/example/hermes-dashboard.env.example /var/lib/hermes/.hermes/.env
sudo chmod 600 /var/lib/hermes/env /var/lib/hermes/.hermes/.env
# Edit with your real API keys and tokens
```

### 3. Build & Switch

```bash
# First build (downloads all dependencies — takes a while)
sudo nixos-rebuild switch --flake /etc/nixos#buildfleet-server

# Subsequent updates
sudo mount -o remount,rw /
sudo mount -o remount,rw /run  # if needed
sudo nixos-rebuild switch --flake /etc/nixos#buildfleet-server
```

### 4. Post-Install

- **AdGuard Home**: Visit `http://<server-ip>:3000` on first run to set up admin password, then configure DNS filtering lists
- **Tailscale**: Run `sudo tailscale up --advertise-exit-node` to join your tailnet and enable exit node
- **Hermes Agent**: Starts automatically. Configure Telegram bot via `@BotFather` and set `TELEGRAM_BOT_TOKEN` in `/var/lib/hermes/env`
- **Hermes Dashboard**: Access at `http://<tailscale-ip>:9119` from your tailnet

## Auto-Updates

The `update.nix` module sets up three layers:

1. **Sun 03:00 ET** — `nix flake update` bumps all flake.lock inputs (with git checkpoint)
2. **Mon 04:00 ET** — `system.autoUpgrade` rebuilds from updated flake (with auto-reboot)
3. **Sat 02:00 ET** — `nix.gc` collects garbage older than 7 days

Rollback: `sudo nix-env --rollback -p /nix/var/nix/profiles/system` or select a previous generation from GRUB.

## Key Design Decisions

- **Declarative everything** — AdGuard in Docker (via `containers.nix`), not bare-metal, for clean updates
- **Tailscale-only dashboard** — Port 9119 not exposed to LAN; only reachable inside the mesh
- **Self-interruption prevention** — `restartIfChanged = false` on hermes-agent so rebuilds don't kill active sessions
- **Passwordless sudo** — `NOPASSWD + SETENV` for wheel group, with `NoNewPrivileges = false` on Hermes services
- **Nix store paths for cloakbrowser** — Binary path updates after each rebuild (tracked in hermes config)

## Hardware

- **CPU**: AMD (KVM-AMD virtualization enabled)
- **GPU**: NVIDIA RTX 3060 (proprietary driver, modesetting enabled)
- **Network**: Ethernet + WiFi (NetworkManager)
- **Storage**: ext4 root, swap partition

## Firewall

| Port | Protocol | Purpose | Scope |
|------|----------|---------|-------|
| 22 | TCP | SSH | LAN + WAN |
| 53 | TCP/UDP | AdGuard DNS | LAN + WAN |
| 80 | TCP | ACME HTTP challenge | WAN |
| 443 | TCP | Nginx (HTTPS) | WAN |
| 3000 | TCP | AdGuard initial setup | LAN |
| 8080 | TCP | AdGuard admin UI | LAN |
| 9119 | TCP | Hermes Dashboard | Tailscale only |

## License

This configuration is provided as-is for reference. Feel free to fork and adapt for your own home server setup.