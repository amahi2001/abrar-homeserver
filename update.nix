# Guarded NixOS update policy.
#
# Daily: update only the independently pinned Codex CLI input.
# Sunday: update all flake inputs and dry-build the resulting system.
# Monday: deploy the already-validated lock file with system.autoUpgrade.
#
# Update jobs intentionally run only from a clean main checkout. This prevents
# timers from committing into an agent/review branch or mixing lock updates
# with an administrator's in-progress configuration work.
{ pkgs, ... }:

let
  gitPreflight = ''
    if ! "$GIT" -C "$FLAKE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[$LOG_TAG] /etc/nixos is not a Git worktree; skipping automatic update."
      exit 0
    fi

    branch="$("$GIT" -C "$FLAKE_DIR" symbolic-ref --quiet --short HEAD || true)"
    if [ "$branch" != "main" ]; then
      echo "[$LOG_TAG] Checkout is on '$branch', not main; skipping automatic update."
      exit 0
    fi

    if [ -n "$("$GIT" -C "$FLAKE_DIR" status --porcelain --untracked-files=normal)" ]; then
      echo "[$LOG_TAG] Worktree has uncommitted changes; skipping automatic update."
      exit 0
    fi
  '';

  codexCliUpdate = pkgs.writeShellScript "codex-cli-update" ''
    set -euo pipefail

    FLAKE_DIR="/etc/nixos"
    GIT="${pkgs.git}/bin/git"
    LOG_TAG="codex-cli-update"
    ${gitPreflight}

    lock_backup="$(${pkgs.coreutils}/bin/mktemp)"
    cleanup() {
      ${pkgs.coreutils}/bin/rm -f "$lock_backup"
    }
    trap cleanup EXIT

    ${pkgs.coreutils}/bin/cp "$FLAKE_DIR/flake.lock" "$lock_backup"
    ${pkgs.nix}/bin/nix flake update codex-cli --flake "$FLAKE_DIR"

    if ${pkgs.diffutils}/bin/cmp -s "$lock_backup" "$FLAKE_DIR/flake.lock"; then
      echo "[$LOG_TAG] Codex CLI input is already current."
      exit 0
    fi

    if ! ${pkgs.nixos-rebuild}/bin/nixos-rebuild dry-build \
      --flake "$FLAKE_DIR#buildfleet-server"; then
      echo "[$LOG_TAG] Validation failed; restoring the previous lock file."
      ${pkgs.coreutils}/bin/cp "$lock_backup" "$FLAKE_DIR/flake.lock"
      exit 1
    fi

    "$GIT" -C "$FLAKE_DIR" \
      -c user.name="NixOS Codex Updater" \
      -c user.email="codex-updater@localhost" \
      add flake.lock
    if ! "$GIT" -C "$FLAKE_DIR" \
      -c user.name="NixOS Codex Updater" \
      -c user.email="codex-updater@localhost" \
      commit --only flake.lock -m "chore: update Codex CLI"; then
      echo "[$LOG_TAG] WARNING: validated lock file was not committed."
    fi
  '';

  nixosFlakeUpdate = pkgs.writeShellScript "nixos-flake-update" ''
    set -euo pipefail

    FLAKE_DIR="/etc/nixos"
    GIT="${pkgs.git}/bin/git"
    LOG_TAG="nixos-flake-update"
    ${gitPreflight}

    lock_backup="$(${pkgs.coreutils}/bin/mktemp)"
    cleanup() {
      ${pkgs.coreutils}/bin/rm -f "$lock_backup"
    }
    trap cleanup EXIT

    ${pkgs.coreutils}/bin/cp "$FLAKE_DIR/flake.lock" "$lock_backup"
    ${pkgs.nix}/bin/nix flake update --flake "$FLAKE_DIR"

    if ${pkgs.diffutils}/bin/cmp -s "$lock_backup" "$FLAKE_DIR/flake.lock"; then
      echo "[$LOG_TAG] Flake inputs are already current."
      exit 0
    fi

    if ! ${pkgs.nixos-rebuild}/bin/nixos-rebuild dry-build \
      --flake "$FLAKE_DIR#buildfleet-server"; then
      echo "[$LOG_TAG] Validation failed; restoring the previous lock file."
      ${pkgs.coreutils}/bin/cp "$lock_backup" "$FLAKE_DIR/flake.lock"
      exit 1
    fi

    "$GIT" -C "$FLAKE_DIR" \
      -c user.name="NixOS Auto Updater" \
      -c user.email="nixos-updater@localhost" \
      add flake.lock
    if ! "$GIT" -C "$FLAKE_DIR" \
      -c user.name="NixOS Auto Updater" \
      -c user.email="nixos-updater@localhost" \
      commit --only flake.lock -m "chore: update NixOS flake inputs"; then
      echo "[$LOG_TAG] WARNING: validated lock file was not committed."
    fi
  '';
in
{
  systemd.services.codex-cli-update = {
    description = "Update and validate the OpenAI Codex CLI flake input";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = codexCliUpdate;
      TimeoutStartSec = "30min";
    };
  };

  systemd.timers.codex-cli-update = {
    description = "Daily guarded Codex CLI update";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 04:30:00 America/New_York";
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };

  systemd.services.nixos-flake-update = {
    description = "Update and validate all NixOS flake inputs";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = nixosFlakeUpdate;
      TimeoutStartSec = "60min";
    };
  };

  systemd.timers.nixos-flake-update = {
    description = "Weekly guarded NixOS flake update";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun *-*-* 03:00:00 America/New_York";
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };

  system.autoUpgrade = {
    enable = true;
    flake = "/etc/nixos#buildfleet-server";
    dates = "Mon *-*-* 04:00:00 America/New_York";
    allowReboot = true;
    persistent = true;
  };

  nix.gc = {
    automatic = true;
    dates = "Sat *-*-* 02:00:00 America/New_York";
    options = "--delete-older-than 7d";
  };

  nix.optimise.automatic = true;
}
