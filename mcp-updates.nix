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
}
