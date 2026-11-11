# Refresh persistent npm/venv MCP dependencies independently of the Hermes flake.
{
  config,
  pkgs,
  ...
}:
let
  browser = config.services.hermes-agent.mcpServers.cloakbrowser;
  updater = pkgs.writeShellApplication {
    name = "hermes-mcp-update";
    runtimeInputs = with pkgs; [
      bash
      coreutils
      curl
      systemd
      util-linux
      nodejs
    ];
    text = ''
      exec ${pkgs.python3}/bin/python -P ${./scripts/mcp-update.py}
    '';
  };
in
{
  systemd.services.hermes-mcp-update = {
    description = "Guarded Hermes MCP dependency refresh";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    onFailure = [ "maintenance-alert@%n.service" ];
    restartIfChanged = false;
    stopIfChanged = false;
    environment = browser.env // {
      WARDEN_COMMAND = config.services.hermes-agent.mcpServers.warden-mcp.command;
      NPM_BIN = "${pkgs.nodejs_22}/bin/npm";
      UPGRADE_PYTHON = "${pkgs.python312}/bin/python3";
    };
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${updater}/bin/hermes-mcp-update";
      StateDirectory = "hermes-mcp-updates";
      StateDirectoryMode = "0700";
      UMask = "0077";
      TimeoutStartSec = "30min";
      TimeoutStopSec = "5min";
      # A busy gateway is a successful deferral, not a failed deployment.
      SuccessExitStatus = [ 75 ];
      Nice = 10;
    };
  };
  systemd.timers.hermes-mcp-update = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Tue *-*-* 05:00:00 America/New_York";
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };
  systemd.services.hermes-mcp-retry = {
    description = "Retry MCP maintenance deferred by active Hermes work";
    serviceConfig.Type = "oneshot";
    script = ''
      if [ "$(${pkgs.systemd}/bin/systemctl show --property=ActiveState --value hermes-mcp-update.service)" = inactive ] &&
         [ "$(${pkgs.systemd}/bin/systemctl show --property=ExecMainStatus --value hermes-mcp-update.service)" = 75 ]; then
        ${pkgs.systemd}/bin/systemctl start --no-block hermes-mcp-update.service
      fi
    '';
  };
  systemd.timers.hermes-mcp-retry = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "15min";
      OnUnitInactiveSec = "15min";
    };
  };
}
