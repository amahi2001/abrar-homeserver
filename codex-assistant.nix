{
  pkgs,
  lib,
  config,
  ...
}:
let
  home = config.users.users.amahi.home;
  hermesSkills = "/var/lib/hermes/.hermes/skills";
  skills = {
    email-inbox-triage = "${hermesSkills}/email/email-inbox-triage";
    meeting-action-items = "${hermesSkills}/productivity/meeting-action-items";
    document-to-action-items = "${hermesSkills}/productivity/document-to-action-items";
    github-issues = "${hermesSkills}/github/github-issues";
    browser-administration = ./skills/browser-administration;
    github-pr-workflow = ./skills/github-pr-workflow;
    scheduled-reminders = ./skills/scheduled-reminders;
    server-health = ./skills/server-health;
    vaultwarden = ./skills/vaultwarden;
  };
  cloakDeps = pkgs.python312Packages;
  cloakbrowser = cloakDeps.buildPythonPackage {
    pname = "cloakbrowser";
    version = "0.7.3";
    format = "wheel";
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/bc/0e/1ef19fde8d2c74535c61b59710699cdbc973fa9e4b85f1233185dd2ce7e5/cloakbrowser-0.7.3-py3-none-any.whl";
      hash = "sha256-o/1fYjiC+qhOEYsjocD5iJECqN6PJnb3D/oZzRF+wjs=";
    };
    dependencies = with cloakDeps; [ cryptography httpx playwright ];
  };
  cloakMcp = cloakDeps.buildPythonApplication {
    pname = "cloakbrowsermcp";
    version = "2.0.4";
    pyproject = true;
    build-system = [ cloakDeps.hatchling ];
    # The published wheel still passes the backend argument removed by CloakBrowser.
    src = pkgs.fetchFromGitHub {
      owner = "overtimepog";
      repo = "CloakMCP";
      rev = "1e7122d9543e444216a42131b6b95dee736448f2";
      hash = "sha256-Soc0c9SHRcWGzRFdXna2Ag8VBnepTcDiUBheHONbnsg=";
    };
    dependencies = [ cloakbrowser cloakDeps.mcp cloakDeps.playwright ];
  };
  cloakLauncher = pkgs.writeShellApplication {
    name = "codex-cloakbrowser";
    runtimeEnv = config.services.hermes-agent.mcpServers.cloakbrowser.env;
    text = ''
      exec ${lib.getExe' cloakMcp "cloakbrowsermcp"} --caps ""
    '';
  };
  pinchtabVersion = "0.15.2";
  pinchtabBinary = pkgs.fetchurl {
    url = "https://github.com/pinchtab/pinchtab/releases/download/v${pinchtabVersion}/pinchtab-linux-amd64";
    hash = "sha256-C7T5ehyS+UvNut/8OdxphaR1fIineHa7y0IUIZHoIC0=";
  };
  pinchtab = pkgs.runCommand "pinchtab-${pinchtabVersion}" { } ''
    install -Dm755 ${pinchtabBinary} "$out/bin/pinchtab"
  '';
  pinchSettings = pkgs.writeText "pinchtab-settings.json" (builtins.toJSON {
    server = {
      bind = "127.0.0.1";
      port = "9867";
    };
    browser.binary = lib.getExe pkgs.chromium;
    instanceDefaults.headless = true;
    autoSolver.enabled = false;
    security = {
      allowCookies = false;
      allowEvaluate = false;
      allowDownload = false;
      allowUpload = false;
      attach.enabled = false;
      idpi = {
        enabled = true;
        scanContent = true;
        strictMode = true;
        wrapContent = true;
      };
    };
  });
  pinchService = pkgs.writeShellApplication {
    name = "codex-pinchtab-service";
    runtimeInputs = with pkgs; [ jq openssl ];
    text = ''
      umask 077
      if [[ ! -s "$STATE_DIRECTORY/token" ]]; then
        openssl rand -hex 32 > "$STATE_DIRECTORY/token"
      fi
      export PINCHTAB_CONFIG="$STATE_DIRECTORY/config.json"
      temporary="$(mktemp "$STATE_DIRECTORY/config.XXXXXX")"
      trap 'rm -f "$temporary"' EXIT
      jq --arg state "$STATE_DIRECTORY" --rawfile token "$STATE_DIRECTORY/token" '
        .server.stateDir = $state
        | .profiles.baseDir = ($state + "/profiles")
        | .server.token = ($token | rtrimstr("\n"))
      ' ${pinchSettings} > "$temporary"
      mv "$temporary" "$PINCHTAB_CONFIG"
      exec ${pinchtab}/bin/pinchtab server
    '';
  };
  pinchLauncher = pkgs.writeShellApplication {
    name = "codex-pinchtab";
    text = ''
      state="''${XDG_STATE_HOME:-$HOME/.local/state}/codex-pinchtab"
      export PINCHTAB_CONFIG="$state/config.json"
      unset PINCHTAB_SESSION
      PINCHTAB_TOKEN="$(< "$state/token")"
      export PINCHTAB_TOKEN
      exec ${pinchtab}/bin/pinchtab --server http://127.0.0.1:9867 --agent-id codex mcp
    '';
  };
  # Use the same supported Node/npm pair as Hermes's Warden updater.
  wardenNpmRoot = dirOf (dirOf config.systemd.services.hermes-mcp-update.environment.NPM_BIN);
  wardenLauncher = pkgs.writeShellApplication {
    name = "codex-warden";
    text = ''
      credentials="''${XDG_CONFIG_HOME:-$HOME/.config}/codex-warden/credentials.json"
      exec ${pkgs.python3}/bin/python -P ${./skills/vaultwarden/launch.py} "$credentials" \
        ${lib.getExe pkgs.bitwarden-cli} ${wardenNpmRoot}/bin/node \
        ${wardenNpmRoot}/lib/npm/bin/npx-cli.js
    '';
  };
  health = pkgs.writeShellApplication {
    name = "codex-server-health";
    runtimeInputs = with pkgs; [ coreutils systemd ];
    text = ''
      printf 'Failed systemd units\n'
      systemctl --failed --no-pager --plain
      printf '\nDisk usage\n'
      df -hP / /srv/data
      printf '\nShopping research backup\n'
      systemctl show shopping-research-mcp-backup.service \
        -p Result -p ExecMainStatus -p ExecMainExitTimestamp
      systemctl list-timers --no-pager shopping-research-mcp-backup.timer
      printf '\nGitHub runner services\n'
      systemctl list-units --all --no-pager 'github-actions-*.service'
    '';
  };
in
{
  environment.systemPackages = [ health pinchtab ];
  environment.etc."codex/config.toml".source = (pkgs.formats.toml { }).generate "codex-system.toml" {
    mcp_servers = {
      cloakbrowser = {
        command = lib.getExe cloakLauncher;
        enabled_tools = [
          "cloak_launch" "cloak_close" "cloak_navigate" "cloak_snapshot" "cloak_read_page"
          "cloak_click" "cloak_type" "cloak_press_key" "cloak_select" "cloak_scroll"
          "cloak_wait" "cloak_screenshot" "cloak_list_pages" "cloak_new_page" "cloak_close_page"
        ];
      };
      pinchtab = {
        command = lib.getExe pinchLauncher;
        enabled_tools = [
          "pinchtab_health" "pinchtab_list_tabs" "pinchtab_navigate" "pinchtab_snapshot"
          "pinchtab_get_text" "pinchtab_click" "pinchtab_fill" "pinchtab_type" "pinchtab_select"
          "pinchtab_key" "pinchtab_scroll" "pinchtab_screenshot" "pinchtab_close_tab"
        ];
      };
      warden = {
        command = lib.getExe wardenLauncher;
        enabled = false;
        startup_timeout_sec = 60;
        enabled_tools = [ "keychain_status" "keychain_search_items" "keychain_get_item" ];
      };
    };
  };

  systemd.tmpfiles.rules = lib.mapAttrsToList (
    name: source: "L+ ${home}/.agents/skills/${name} - - - - ${source}"
  ) skills;

  systemd.user.services.codex-pinchtab = {
    description = "Local PinchTab browser for Codex";
    wantedBy = [ "default.target" ];
    unitConfig.ConditionUser = "amahi";
    serviceConfig = {
      ExecStart = lib.getExe pinchService;
      StateDirectory = "codex-pinchtab";
      StateDirectoryMode = "0700";
      UMask = "0077";
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };
  systemd.services.codex-server-health = {
    description = "Read-only Codex server health report";
    serviceConfig = {
      Type = "oneshot";
      User = "amahi";
      ExecStart = lib.getExe health;
    };
  };
  systemd.timers.codex-server-health = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Mon *-*-* 09:00:00 America/New_York";
      Persistent = true;
    };
  };
}
