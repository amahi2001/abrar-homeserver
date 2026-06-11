# NixOS auto-update module (best-practice approach)
#
# Three layers:
#   1. nixos-flake-update timer: bumps all flake.lock inputs weekly (Sun 03:00)
#   2. system.autoUpgrade: rebuilds from updated flake weekly (Mon 04:00)
#   3. nix.gc + nix.optimise: garbage collection and store optimisation
#
# Why separate timers instead of one script?
#   - system.autoUpgrade is maintained by NixOS upstream, handles failures
#     gracefully (won't switch if build fails), supports allowReboot for
#     kernel updates, and auto-rolls back via GRUB generations.
#   - nix flake update must run BEFORE the rebuild but has different failure
#     modes (network issues, bad input), so it's a separate unit.
#   - Sunday flake-update → Monday rebuild gives a ~25h window to rollback
#     flake.lock if an input breaks.
#
# Rollback:
#   cd /etc/nixos && sudo git checkout -- flake.lock
#   sudo nixos-rebuild switch --flake /etc/nixos#buildfleet-server
#   Or from GRUB: select a previous generation.
#
# Check status:
#   systemctl list-timers nixos-*
#   journalctl -u nixos-flake-update
#   journalctl -u nixos-upgrade
{ config, pkgs, lib, ... }:

let
  nixosFlakeUpdate = pkgs.writeShellScript "nixos-flake-update" ''
    set -euo pipefail

    FLAKE_DIR="/etc/nixos"
    LOG_TAG="nixos-flake-update"

    echo "[$LOG_TAG] Starting flake update at $(date)"
    cd "$FLAKE_DIR"

    # Git checkpoint for easy rollback
    if git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
      ${pkgs.git}/bin/git add -A
      ${pkgs.git}/bin/git commit -m "pre-flake-update checkpoint $(date +%Y-%m-%d_%H-%M)" || true
      echo "[$LOG_TAG] Git checkpoint created"
    else
      echo "[$LOG_TAG] WARNING: /etc/nixos is not a git repo — skipping checkpoint"
    fi

    # Update all flake inputs (nixpkgs, hermes-agent, cloakbrowser, etc.)
    echo "[$LOG_TAG] Running nix flake update..."
    ${pkgs.nix}/bin/nix flake update --flake "$FLAKE_DIR"

    # Commit updated lockfile
    if git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
      ${pkgs.git}/bin/git add flake.lock
      ${pkgs.git}/bin/git commit -m "post-flake-update $(date +%Y-%m-%d_%H-%M)" || true
    fi

    echo "[$LOG_TAG] Flake update completed at $(date)"
  '';
in
{
  # --- 1. Flake input update (runs Sunday 03:00 ET) ---
  # Bumps all inputs in flake.lock so the next auto-upgrade picks up new versions.
  systemd.services.nixos-flake-update = {
    description = "Update all NixOS flake inputs";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    path = with pkgs; [ nix git coreutils ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = nixosFlakeUpdate;
      TimeoutStartSec = "15min";
    };
  };

  systemd.timers.nixos-flake-update = {
    description = "Weekly flake input update (runs before auto-upgrade)";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun *-*-* 03:00:00 America/New_York";
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };

  # --- 2. NixOS auto-upgrade (runs Monday 04:00 ET) ---
  # Uses the built-in NixOS module — handles build failures gracefully,
  # won't switch if build fails, and reboots automatically when kernel/boot changes.
  system.autoUpgrade = {
    enable = true;
    flake = "path:/etc/nixos";
    flags = [ "--flake" "/etc/nixos#buildfleet-server" ];
    dates = "Mon *-*-* 04:00:00 America/New_York";
    allowReboot = true;
    persistent = true;
  };

  # --- 3. Garbage collection ---
  # Keeps last 7 days of old generations, runs weekly (Sat 02:00).
  nix.gc = {
    automatic = true;
    dates = "Sat *-*-* 02:00:00 America/New_York";
    options = "--delete-older-than 7d";
  };

  # --- 4. Store optimisation (deduplication) ---
  nix.optimise.automatic = true;
}
