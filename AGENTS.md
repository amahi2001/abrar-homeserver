# NixOS Configuration Guide

This directory contains the declarative NixOS configuration for buildfleet-server. All system configuration changes should be made here and applied via `nixos-rebuild`.

## File Structure

- `configuration.nix` - Host, users, packages, SSH, and base firewall
- `containers.nix` - Docker, AdGuard Home, and guarded image updates
- `desktop.nix` - Headless default, XFCE recovery UI, NVIDIA, and Bluetooth
- `files.nix` - Nextcloud, Samba, Gluetun, and Transmission
- `servarr.nix` - Prowlarr, FlareSolverr, and on-demand Sonarr/Radarr
- `hermes.nix` - Hermes Agent + Dashboard service configuration (imported by configuration.nix)
- `tailscale.nix` - Tailnet and exit-node configuration
- `update.nix` - Guarded flake updates, system upgrades, and garbage collection
- `web.nix` - DuckDNS, ACME, Nginx, and Vaultwarden
- `hardware-configuration.nix` - Hardware-specific settings (auto-generated, don't edit)
- `flake.nix` - Flake inputs: nixpkgs, hermes-agent, cloakbrowser
- `flake.lock` - Pinned flake input versions
- `.devin/` - Devin coding agent config + skill symlinks
- `.agents/skills/` - Repository-scoped Codex skill symlinks (see Agent Integrations)
- `.gitignore` - Standard Node.gitignore

## Hermes Agent Architecture

Two **separate** systemd services:

| Service | Purpose | Port |
|---------|---------|------|
| `hermes-agent.service` | Gateway for Telegram/CLI conversations | n/a (outbound) |
| `hermes-dashboard.service` | Web UI for Hermes Desktop app | 9119 (Tailscale only) |

### Critical: Desktop App ≠ api_server

The **Hermes Desktop app** connects to `hermes dashboard` (web UI with TUI chat), NOT the `api_server` platform adapter. The `api_server` is for OpenAI-compatible API clients (Open WebUI, LobeChat). The dashboard is a separate systemd service defined in `hermes.nix`.

The dashboard requires:
- `hermes-agent#full` package variant (includes TUI/PTY dependencies)
- Session token in `/var/lib/hermes/env` as `HERMES_DASHBOARD_SESSION_TOKEN`
- Tailscale for transport security (no HTTPS needed within the mesh)

### hermes.nix highlights

- `extraDependencyGroups` supplies provider and messaging integrations
- Warden MCP is declared with credentials loaded from `/var/lib/hermes/env`
- Agent tools come from system packages and declarative wrapper commands
- No `api_server` config block — dashboard is a separate service
- Dashboard runs: `hermes dashboard --host 0.0.0.0 --port 9119 --insecure --tui --no-open --skip-build`

## Sudo Access Configuration

The `hermes` user needs passwordless sudo for AI-assisted system administration. This requires **all four** settings:

```nix
# 1. hermes user gets nixconfig group (wheel comes via SupplementaryGroups)
users.users.hermes.extraGroups = [ "nixconfig" ];

# 2. NOPASSWD via extraRules (NOT wheelNeedsPassword — that's broken on NixOS)
security.sudo.enable = true;
security.sudo.extraRules = [
  {
    groups = [ "wheel" ];
    commands = [
      { command = "ALL"; options = [ "NOPASSWD" "SETENV" ]; }
    ];
  }
];

# 3. Disable NoNewPrivileges on BOTH services
systemd.services.hermes-agent.serviceConfig.NoNewPrivileges = lib.mkForce false;
systemd.services.hermes-dashboard.serviceConfig.NoNewPrivileges = lib.mkForce false;

# 4. SupplementaryGroups on BOTH services (gives processes wheel at runtime)
systemd.services.hermes-agent.serviceConfig.SupplementaryGroups = [ "wheel" ];
systemd.services.hermes-dashboard.serviceConfig.SupplementaryGroups = [ "wheel" ];
```

**WARNING:** `security.sudo.wheelNeedsPassword = false` does NOT generate NOPASSWD in current NixOS versions. Always use `extraRules`.

**Why SETENV:** The `SETENV` option allows sudo to preserve environment variables (needed by Nix tools that set `PATH`, `NIX_PATH`, etc.).

### Applying group/sudo changes

```bash
sudo nixos-rebuild switch
sudo systemctl daemon-reload
sudo systemctl stop hermes-agent hermes-dashboard
sudo systemctl start hermes-agent hermes-dashboard
```

**Important:** `systemctl restart` does NOT pick up new group memberships. Must do full stop + start.

**Agent Safety Tip (Prevent Self-Termination):** If an agent needs to execute this stop/start cycle from within a session (without self-terminating), they should use `systemd-run` to run the commands in a detached transient unit. This prevents the process from being killed when the parent service stops:
```bash
sudo systemd-run --unit=hermes-restarter --description="Transient Hermes Restarter" bash -c "systemctl stop hermes-agent hermes-dashboard && systemctl start hermes-agent hermes-dashboard"
```

## Self-Interruption Prevention

```nix
systemd.services.hermes-agent.restartIfChanged = false;
systemd.services.hermes-agent.stopIfChanged = false;
```

Without these, `nixos-rebuild switch` stops hermes-agent, killing any running AI session.

## Filesystem Notes

### Host and service mount namespaces

The host uses a writable ext4 `/` and writable tmpfs `/run`. Hermes gateway
commands run in a separate systemd mount namespace with `ProtectSystem=strict`.
A read-only path there is a service sandbox restriction, not evidence that the
host root was configured read-only. `/nix/store` is intentionally read-only.

Never remount `/` or `/run` for media files, Git edits, or system activation.
These rules supersede historical remount examples in the NixOS skills and their
references. Do not try to make the host root read-only: services, logs, state,
and media share that filesystem, so doing so would interrupt normal operation.
Do not stop processes to force a remount.

The gateway's `ReadWritePaths` in `hermes.nix` permits subtitle sidecars under
`/srv/data/media/library/movies` and `/srv/data/media/library/tv`. If an authorized
write still fails with `EROFS`, inspect the destination's `VFS-OPTIONS` and the
service allowlist; correct that narrow allowlist instead of weakening the root
sandbox. Use `sudo` for Unix ownership/permission requirements only.

### Applying Changes
```bash
sudo nixos-rebuild switch --flake /etc/nixos#buildfleet-server
```

Run activation from the host namespace. From a sandboxed gateway session, use
the host system manager instead:

```bash
sudo systemd-run --wait --pipe --collect \
  /run/current-system/sw/bin/nixos-rebuild switch --flake /etc/nixos#buildfleet-server
```

After changing the gateway allowlist, restart it from the host only once active
conversations are idle; a new namespace restores `ProtectSystem=strict`. Check
inside that namespace that `/` has `ro` in `VFS-OPTIONS` and the two organized
media paths have `rw`. The host `/` should remain `rw`.

## Firewall

Port 9119 is **NOT** in `allowedTCPPorts` (no LAN access). It's only open on Tailscale:

```nix
networking.firewall.allowedTCPPorts = [ 22 80 443 53 8080 ];  # no 9119
networking.firewall.interfaces.tailscale0 = {
  allowedTCPPorts = [ 9119 9090 7878 8989 9696 ];  # Tailscale only
};
```

## Remote Access (Tailscale)

- MagicDNS: `buildfleet-server.tailcb7cdb.ts.net`
- Tailscale IP: `100.91.234.17`
- Dashboard URL: `http://buildfleet-server.tailcb7cdb.ts.net:9119`

## Agent Integrations

The `.devin/` directory contains config for the Devin coding agent:

- `.devin/config.local.json` — Permissions (Exec for ls, sudo, bun, bunx)
- `.devin/skills/` — Symlinks to Hermes skills (set up via tmpfiles.rules in hermes.nix)

The tmpfiles rules create symlinks:
```
/etc/nixos/.devin/skills/nixos-environment → /var/lib/hermes/.hermes/skills/devops/nixos-environment
/etc/nixos/.devin/skills/nixos-hermes-integration → /var/lib/hermes/.hermes/skills/devops/nixos-hermes-integration
/etc/nixos/.devin/skills/nixos-hermes-setup → /var/lib/hermes/.hermes/skills/devops/nixos-hermes-setup
/etc/nixos/.devin/skills/cloakbrowser-nixos-setup → /var/lib/hermes/.hermes/skills/devops/cloakbrowser-nixos-setup
```

The same four links are declared under `.agents/skills/`, the repository-scoped
skill location used by Codex. Codex also reads this `AGENTS.md` whenever it works
inside `/etc/nixos`, so the operational context and the linked skills travel
together. Both sets of links are managed by `tmpfiles.rules` in `hermes.nix`.

## Current Configuration

### Models (in hermes.nix)
- Main: `gpt-5.6-luna` via `openai-codex` provider
- Compression: `deepseek-v4-pro` via `opencode-go` provider
- Main provider base URL: `https://chatgpt.com/backend-api/codex`

### Packages (in configuration.nix)
- `bun` 1.4.3 — pinned upstream stable runtime for scripts and MCP servers;
  includes `bunx` (`bunx --bun <package>` forces the Bun runtime)
- Deno and the `dx` wrapper have been removed. The vault sync uses Bun and
  standard Node APIs; its fake-CLI regression tests run in `nix flake check`.
- `cloakbrowser` — stealth Chromium from CloakHQ flake
- Docker + docker-compose, AdGuard Home, nginx, certbot, etc.

### Other Services
- **nginx** (port 443) — TLS termination for `cloud.amahi.dev`, `vault.amahi.dev`, and `jellyfin.amahi.dev`
- **AdGuard Home** (Docker, ports 53/8080) — DNS ad blocking + admin UI
- **Nextcloud** — file storage and synchronization at `https://cloud.amahi.dev`
- **Vaultwarden** — password manager at `https://vault.amahi.dev`
- **Jellyfin** — media playback at `https://jellyfin.amahi.dev`
- **Gluetun + Transmission** — VPN-isolated download service
- **Prowlarr + FlareSolverr** — media search/indexer services
- **Sonarr + Radarr** — on-demand library organization
- **Samba + WSD** — authenticated media/inbox shares on LAN and Tailscale
- **Docker** — Container runtime
- **Tailscale** — VPN mesh
- **DuckDNS** — Dynamic DNS
- **XFCE** — Desktop environment

## Common Pitfalls

| Issue | Cause | Solution |
|-------|-------|----------|
| `sudo: password required` | Using `wheelNeedsPassword = false` | Use `extraRules` with NOPASSWD |
| `sudo: "no new privileges"` | NoNewPrivileges not disabled | Add `NoNewPrivileges = lib.mkForce false` to both services |
| `id` shows no wheel group | Missing SupplementaryGroups | Add `SupplementaryGroups = [ "wheel" ]` to service |
| Desktop app can't connect | Using api_server instead of dashboard | Desktop connects to `hermes dashboard`, not api_server |
| `ModuleNotFoundError: dashboard_auth` | Using default hermes-agent package | Install `hermes-agent#full` variant |
| Rebuild kills agent session | Missing restartIfChanged | Add `restartIfChanged = false; stopIfChanged = false` |
| `/run` read-only during rebuild | Activation attempted inside a service sandbox | Run activation through the host system manager; never remount |
| Group changes not applied | Used restart instead of stop+start | Full stop + start cycle required |
| Dev stop+start hangs | Dependencies on agent service | Dashboard `after = [ "hermes-agent.service" ]` |

## Verification Commands

```bash
# Check services
systemctl status hermes-agent hermes-dashboard tailscaled

# Check dashboard is listening
ss -tlnp | grep 9119

# Check sudo works (from hermes user)
sudo whoami  # should print "root"

# Check group membership
id  # should show groups: hermes, nixconfig, wheel

# Check Tailscale
tailscale status

# Check hermes config
hermes mcp list
hermes tools list --summary
```

## Related Files

- `/var/lib/hermes/.hermes/config.yaml` — Hermes runtime config (auto-generated from hermes.nix)
- `/var/lib/hermes/env` — Environment variables (API keys, tokens)
- `/var/lib/hermes/.hermes/logs/gateway.log` — Gateway logs
- `/var/lib/hermes/.nix-profile/bin/hermes` — Full variant binary
