{ pkgs, ... }:
let
  state = "/var/lib/shopping-research-mcp";
  compose = "${pkgs.docker}/bin/docker compose --project-directory ${state}/current -f ${state}/current/compose.yaml";
  backup = pkgs.writeShellScriptBin "shopping-research-mcp-backup" ''
    export PATH=${pkgs.lib.makeBinPath [ pkgs.docker pkgs.coreutils ]}:$PATH
    exec ${pkgs.python3}/bin/python -I -B ${./backup.py} "$@"
  '';
in
{
  systemd.tmpfiles.rules = [
    "d ${state} 0750 root users -"
    "d ${state}/data 0750 buildfleet users -"
    "d ${state}/backups 0750 buildfleet users -"
    "d ${state}/releases 0750 root users -"
    "d /var/lib/secrets/shopping-research-mcp 0750 root users -"
  ];

  systemd.services.shopping-research-mcp = {
    description = "Authenticated shopping research MCP server";
    wantedBy = [ "multi-user.target" ];
    after = [ "docker.service" "network-online.target" ];
    requires = [ "docker.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${compose} up -d --no-build --wait --wait-timeout 90";
      ExecStop = "${compose} stop --timeout 30";
      TimeoutStartSec = "2min";
      TimeoutStopSec = "45s";
      UMask = "0077";
    };
  };

  systemd.services.shopping-research-mcp-backup = {
    description = "Online SQLite snapshots for shopping research and authentication";
    after = [ "shopping-research-mcp.service" ];
    onFailure = [ "maintenance-alert@%n.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${backup}/bin/shopping-research-mcp-backup daily";
      TimeoutStartSec = "10min";
      UMask = "0077";
    };
  };
  systemd.timers.shopping-research-mcp-backup = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 02:20:00 America/New_York";
      Persistent = true;
      RandomizedDelaySec = "10min";
    };
  };

  services.nginx.virtualHosts."shopping.amahi.dev" = {
    enableACME = true;
    forceSSL = true;
    extraConfig = ''
      client_max_body_size 1m;
      access_log off;
    '';
    locations."= /healthz".return = "404";
    locations."/" = {
      proxyPass = "http://127.0.0.1:3347";
      extraConfig = ''
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
      '';
    };
  };
}
