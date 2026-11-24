# Refresh persistent npm/venv MCP dependencies independently of the Hermes flake.
{
  config,
  pkgs,
  securityPackages,
  ...
}:
let
  browser = config.services.hermes-agent.mcpServers.cloakbrowser;
  # Warden declares patch-level engines for both Node and npm.
  wardenNpm = pkgs.runCommand "warden-npm-10.9.11" {
    src = pkgs.fetchurl {
      url = "https://registry.npmjs.org/npm/-/npm-10.9.11.tgz";
      hash = "sha512-PakPh1DVnNuaxWehZ9dDQcvBAmpJq7knHiDWKGSxT58V11cDVCZzLh2N+pjMUaT2aKbjzfpbgtc8MXMegN7IjA==";
    };
  } ''
    mkdir -p "$out/bin" "$out/lib/npm"
    tar -xzf "$src" -C "$out/lib/npm" --strip-components=1
    ln -s ${securityPackages.nodejs_22}/bin/node "$out/bin/node"
    ln -s ../lib/npm/bin/npm-cli.js "$out/bin/npm"
    sed -i "1c#!${securityPackages.nodejs_22}/bin/node" "$out/lib/npm/bin/npm-cli.js"
  '';
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
      NPM_BIN = "${wardenNpm}/bin/npm";
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
