# Guarded NixOS update policy.
#
# Daily: update only the independently pinned Codex CLI input.
# Sunday: update all flake inputs and dry-build the resulting system.
# Sunday later: update to the latest stable Hermes release, deploy, and verify
# it independently, even when unrelated work keeps the general updater guarded.
# Monday: deploy the already-validated lock file with system.autoUpgrade.
#
# The general and Codex update jobs require a clean main checkout. The Hermes
# job updates only its input and preserves any pre-existing lock-file edits.
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

  hermesFlakeUpdate = pkgs.writeShellScript "hermes-flake-update" ''
    set -euo pipefail
    export PATH="${pkgs.git}/bin:$PATH"

    FLAKE_DIR="/etc/nixos"
    GIT="${pkgs.git}/bin/git"
    LOG_TAG="hermes-flake-update"

    exec 9>/run/lock/hermes-flake-update.lock
    ${pkgs.util-linux}/bin/flock -n 9 || {
      echo "[$LOG_TAG] Another Hermes update is running."
      exit 1
    }

    branch="$("$GIT" -C "$FLAKE_DIR" symbolic-ref --quiet --short HEAD || true)"
    if [ "$branch" != "main" ]; then
      echo "[$LOG_TAG] Checkout is on '$branch', not main; refusing to update."
      exit 1
    fi
    if [ -n "$("$GIT" -C "$FLAKE_DIR" status --porcelain -- flake.nix)" ]; then
      # A previous run may have advanced only the Hermes URL while preserving
      # an administrator's dirty lock file. Accept exactly that one-line edit.
      if ! ${pkgs.diffutils}/bin/cmp -s \
        <("$GIT" -C "$FLAKE_DIR" show HEAD:flake.nix | ${pkgs.gnused}/bin/sed -E 's|^    hermes-agent\.url = ".*";$|    hermes-agent.url = "<hermes>";|') \
        <(${pkgs.gnused}/bin/sed -E 's|^    hermes-agent\.url = ".*";$|    hermes-agent.url = "<hermes>";|' "$FLAKE_DIR/flake.nix"); then
        echo "[$LOG_TAG] flake.nix has non-Hermes edits; refusing to update it."
        exit 1
      fi
    fi

    release_tag="$(${pkgs.curl}/bin/curl --fail --silent --show-error \
      --retry 3 --max-time 30 \
      -H 'Accept: application/vnd.github+json' \
      https://api.github.com/repos/NousResearch/hermes-agent/releases/latest \
      | ${pkgs.jq}/bin/jq -er '.tag_name')"
    if ! [[ "$release_tag" =~ ^v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}$ ]]; then
      echo "[$LOG_TAG] Unexpected release tag: $release_tag"
      exit 1
    fi
    release_url="tarball+https://codeload.github.com/NousResearch/hermes-agent/tar.gz/refs/tags/$release_tag"
    desired_line="    hermes-agent.url = \"$release_url\";"
    current_line="$(${pkgs.gnugrep}/bin/grep -E '^    hermes-agent\.url = "' "$FLAKE_DIR/flake.nix")"
    if [ -z "$current_line" ] || [ "$(echo "$current_line" | ${pkgs.coreutils}/bin/wc -l)" -ne 1 ]; then
      echo "[$LOG_TAG] Cannot identify a unique Hermes flake input; refusing to edit."
      exit 1
    fi

    backup_dir="$(${pkgs.coreutils}/bin/mktemp -d)"
    trap '${pkgs.coreutils}/bin/rm -r "$backup_dir"' EXIT
    ${pkgs.coreutils}/bin/cp "$FLAKE_DIR/flake.nix" "$backup_dir/flake.nix"
    ${pkgs.coreutils}/bin/cp "$FLAKE_DIR/flake.lock" "$backup_dir/flake.lock"

    lock_was_clean=1
    if [ -n "$("$GIT" -C "$FLAKE_DIR" status --porcelain -- flake.lock)" ]; then
      lock_was_clean=0
    fi

    if [ "$current_line" != "$desired_line" ]; then
      ${pkgs.gnused}/bin/sed -i \
        's|^    hermes-agent\.url = ".*";$|    hermes-agent.url = "'"$release_url"'";|' \
        "$FLAKE_DIR/flake.nix"
      echo "[$LOG_TAG] Selected stable Hermes release $release_tag."
    fi

    if ! ${pkgs.nix}/bin/nix flake update hermes-agent --flake "$FLAKE_DIR"; then
      echo "[$LOG_TAG] Lock update failed; restoring both flake files."
      ${pkgs.coreutils}/bin/cp "$backup_dir/flake.nix" "$FLAKE_DIR/flake.nix"
      ${pkgs.coreutils}/bin/cp "$backup_dir/flake.lock" "$FLAKE_DIR/flake.lock"
      exit 1
    fi

    if ! ${pkgs.nixos-rebuild}/bin/nixos-rebuild dry-build \
      --flake "$FLAKE_DIR#buildfleet-server"; then
      ${pkgs.coreutils}/bin/cp "$backup_dir/flake.nix" "$FLAKE_DIR/flake.nix"
      ${pkgs.coreutils}/bin/cp "$backup_dir/flake.lock" "$FLAKE_DIR/flake.lock"
      echo "[$LOG_TAG] Validation failed; restored both flake files."
      exit 1
    fi

    expected="$(${pkgs.nix}/bin/nix eval --raw \
      "$FLAKE_DIR#nixosConfigurations.buildfleet-server.config.services.hermes-agent.package.version")"
    if ${pkgs.diffutils}/bin/cmp -s "$backup_dir/flake.nix" "$FLAKE_DIR/flake.nix" \
      && ${pkgs.diffutils}/bin/cmp -s "$backup_dir/flake.lock" "$FLAKE_DIR/flake.lock" \
      && ${pkgs.systemd}/bin/systemctl is-active --quiet hermes-agent.service hermes-dashboard.service \
      && [ "$(${pkgs.curl}/bin/curl --fail --silent --max-time 5 http://127.0.0.1:9119/api/status | ${pkgs.jq}/bin/jq -r .version)" = "$expected" ]; then
      echo "[$LOG_TAG] Hermes is already current at $expected."
      exit 0
    fi

    if ! ${pkgs.nixos-rebuild}/bin/nixos-rebuild switch \
      --flake "$FLAKE_DIR#buildfleet-server"; then
      echo "[$LOG_TAG] NixOS switch reported an error; checking Hermes health before deciding the result."
    fi
    ${pkgs.systemd}/bin/systemctl restart hermes-dashboard.service hermes-agent.service

    actual="$(${pkgs.curl}/bin/curl --fail --silent --show-error \
      --retry 15 --retry-connrefused --retry-delay 2 --max-time 5 \
      http://127.0.0.1:9119/api/status | ${pkgs.jq}/bin/jq -r .version)"
    if [ "$actual" != "$expected" ]; then
      echo "[$LOG_TAG] Backend reports $actual; expected $expected."
      exit 1
    fi
    ${pkgs.systemd}/bin/systemctl is-active --quiet hermes-agent.service hermes-dashboard.service
    echo "[$LOG_TAG] Hermes gateway and Desktop backend are running $actual."

    if ! ${pkgs.diffutils}/bin/cmp -s "$backup_dir/flake.nix" "$FLAKE_DIR/flake.nix" \
      || ! ${pkgs.diffutils}/bin/cmp -s "$backup_dir/flake.lock" "$FLAKE_DIR/flake.lock"; then
      if [ "$lock_was_clean" -eq 1 ]; then
        "$GIT" -C "$FLAKE_DIR" \
          -c user.name="NixOS Hermes Updater" \
          -c user.email="hermes-updater@localhost" \
          add flake.nix flake.lock
        "$GIT" -C "$FLAKE_DIR" \
          -c user.name="NixOS Hermes Updater" \
          -c user.email="hermes-updater@localhost" \
          commit --only flake.nix flake.lock -m "chore: update Hermes Agent to $release_tag"
      else
        echo "[$LOG_TAG] Preserved pre-existing uncommitted flake.lock changes; no automatic commit."
      fi
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

  systemd.services.hermes-flake-update = {
    description = "Update, deploy, and verify the Nix-managed Hermes backend";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = hermesFlakeUpdate;
      TimeoutStartSec = "4h";
    };
  };

  systemd.timers.hermes-flake-update = {
    description = "Weekly Hermes update and deployment";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun *-*-* 05:00:00 America/New_York";
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
