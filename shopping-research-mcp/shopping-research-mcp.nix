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
  environment.systemPackages = [ backup ];
  systemd.tmpfiles.rules = [
    "d ${state} 0750 root users -"
    "d ${state}/data 0750 amahi users -"
    "d ${state}/backups 0750 root users -"
    "d ${state}/releases 0750 root users -"
    "d /var/lib/secrets/shopping-research-mcp 0750 root users -"
  ];

  systemd.services.shopping-research-mcp = {
    description = "Authenticated shopping research MCP server";
    unitConfig.StopWhenUnneeded = true;
    environment.SHOPPING_REFRESH_SOCKET = "/tmp/shopping-refresh.sock";
    environment.SHOPPING_BACKEND_PORT = "3348";
    after = [ "docker.service" "network-online.target" ];
    requires = [ "docker.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${compose} up -d --no-build --wait --wait-timeout 90";
      ExecStop = pkgs.writeShellScript "shopping-research-mcp-stop" ''
        set -eu
        # Compose can still be waiting on its stop request after the app exits.
        if ${pkgs.coreutils}/bin/timeout -k 3s 40s ${compose} stop --timeout 30; then
          exit 0
        fi

        # Accept a lost stop acknowledgement only for a verified clean exit.
        containers=$(${pkgs.coreutils}/bin/timeout 10s ${compose} ps --all --quiet)
        mapfile -t ids <<< "$containers"
        for id in "''${ids[@]}"; do
          [[ "$id" =~ ^[a-f0-9]{12,64}$ ]] || exit 1
        done
        states=$(${pkgs.coreutils}/bin/timeout 10s ${pkgs.docker}/bin/docker inspect \
          --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' "''${ids[@]}")
        while IFS= read -r state; do
          if [[ "$state" != "exited 0 false" ]]; then
            echo "Shopping MCP stop was not confirmed: $state" >&2
            exit 1
          fi
        done <<< "$states"
        echo "Docker confirmed the app exited cleanly after the stop request."
      '';
      TimeoutStartSec = "2min";
      TimeoutStopSec = "75s";
      UMask = "0077";
    };
  };

  systemd.sockets.shopping-research-mcp-proxy = {
    wantedBy = [ "sockets.target" ];
    listenStreams = [ "127.0.0.1:3347" ];
  };
  systemd.services.shopping-research-mcp-proxy = {
    description = "Wake shopping MCP and forward queued connections";
    requires = [ "shopping-research-mcp.service" ];
    after = [ "shopping-research-mcp.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.systemd}/lib/systemd/systemd-socket-proxyd --exit-idle-time=2min 127.0.0.1:3348";
      DynamicUser = true;
      NoNewPrivileges = true;
    };
  };
  systemd.services.shopping-research-mcp-refresh = {
    description = "Record shopping catalog observations";
    requires = [ "shopping-research-mcp.service" ];
    after = [ "shopping-research-mcp.service" ];
    onFailure = [ "maintenance-alert@%n.service" ];
    serviceConfig = { Type = "oneshot"; TimeoutStartSec = "3min"; };
    script = ''
      exec ${compose} exec -T app bun -e 'const r = await fetch("http://localhost/refresh", { unix: process.env.SHOPPING_REFRESH_SOCKET, method: "POST", signal: AbortSignal.timeout(150000) }); if (r.status !== 204) process.exit(1)'
    '';
  };
  systemd.timers.shopping-research-mcp-refresh = {
    wantedBy = [ "timers.target" ];
    timerConfig = { OnCalendar = "*:0/15"; Persistent = true; AccuracySec = "10s"; };
  };

  systemd.services.shopping-research-mcp-backup = {
    description = "Online SQLite snapshots for shopping research and authentication";
    # Keep live WAL readers present for the read-only snapshot worker.
    requires = [ "shopping-research-mcp.service" ];
    after = [ "shopping-research-mcp.service" ];
    onFailure = [ "maintenance-alert@%n.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${backup}/bin/shopping-research-mcp-backup daily";
      TimeoutStartSec = "20min";
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
