# NixOS Configuration Guide

This directory contains the declarative NixOS configuration for buildfleet-server. All system configuration changes should be made here and applied via `nixos-rebuild`.

## File Structure

- `configuration.nix` - Main system config (hardware, packages, networking, Docker, AdGuard, etc.)
- `hermes.nix` - Hermes Agent + Dashboard service configuration (imported by configuration.nix)
- `hardware-configuration.nix` - Hardware-specific settings (auto-generated, don't edit)
- `flake.nix` - Flake inputs: nixpkgs, hermes-agent, cloakbrowser
- `flake.lock` - Pinned flake input versions
- `.devin/` - Devin coding agent config + skill symlinks (see Devin Integration)
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

- `extraPythonPackages` for platform adapters (currently: python-telegram-bot)
- No `extraPackages` — agent tools come from systemPackages
- No MCP servers declared declaratively (managed via `~/.hermes/config.yaml` or runtime)
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

### Read-Only Mounts
Both `/` and `/run` are mounted read-only by default. Before `nixos-rebuild`:

```bash
sudo mount -o remount,rw /
sudo mount -o remount,rw /run
```

### Applying Changes
```bash
sudo mount -o remount,rw /
sudo mount -o remount,rw /run
sudo nixos-rebuild switch
```

## Firewall

Port 9119 is **NOT** in `allowedTCPPorts` (no LAN access). It's only open on Tailscale:

```nix
networking.firewall.allowedTCPPorts = [ 22 80 443 53 8080 ];  # no 9119
networking.firewall.interfaces.tailscale0 = {
  allowedTCPPorts = [ 9119 ];  # Tailscale only
};
```

## Remote Access (Tailscale)

- MagicDNS: `buildfleet-server.tailcb7cdb.ts.net`
- Tailscale IP: `100.91.234.17`
- Dashboard URL: `http://buildfleet-server.tailcb7cdb.ts.net:9119`

## Devin Integration

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

This lets Devin (and other coding agents) inherit NixOS-specific knowledge from Hermes skills.

## Current Configuration

### Models (in hermes.nix)
- Main: `glm-5.1` via `opencode-go` provider
- Compression: `deepseek-v4-pro` via `opencode-go` provider
- Provider base URL: `https://opencode.ai/zen/go/v1`

### Packages (in configuration.nix)
- `deno` 2.8.2 (with `dx` wrapper) — replaces opencode binary
- `bun` — JS runtime for MCP servers
- `cloakbrowser` — stealth Chromium from CloakHQ flake
- Docker + docker-compose, AdGuard Home, nginx, certbot, etc.

### Other Services
- **nginx** (port 443) — TLS termination for `buildfleet.duckdns.org`
- **AdGuard Home** (Docker, ports 53/8080) — DNS ad blocking + admin UI
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
| `/run` read-only during rebuild | Forgot to remount | Run `mount -o remount,rw /run` first |
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
