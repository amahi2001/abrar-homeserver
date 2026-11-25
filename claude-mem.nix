{ config, lib, pkgs, ... }:
let
  home = config.users.users.amahi.home;
  tool = "${pkgs.python3}/bin/python3 -I ${./scripts/claude-mem-host.py}";
  chromaUvx = pkgs.writeShellScriptBin "uvx" ''
    # Chroma sanitizes the child environment. Set the wheel dependencies here.
    export LD_LIBRARY_PATH=${lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ]}
    exec ${pkgs.uv}/bin/uvx "$@"
  '';
  common = {
    path = with pkgs; [ bash bun coreutils curl git gnumake gcc nodejs_24 python3 uv ];
    environment = {
      HOME = home;
      CI = "1";
      CLAUDE_MEM_ONLINE_OPTIN = "false";
      CLAUDE_MEM_TELEMETRY = "0";
      SSL_CERT_FILE = "/etc/ssl/certs/ca-bundle.crt";
      CLAUDE_MEM_CHROMA_UVX_PATH = "${chromaUvx}/bin";
    };
    serviceConfig = {
      User = "amahi";
      Group = "users";
      WorkingDirectory = home;
      UMask = "0077";
    };
  };
in
{
  environment.systemPackages = [ pkgs.uv ];
  environment.etc."claude-code/managed-settings.d/claude-mem.json".text = builtins.toJSON {
    extraKnownMarketplaces.thedotmack = {
      source = { source = "github"; repo = "thedotmack/claude-mem"; };
      autoUpdate = false;
    };
  };
  systemd.services.claude-mem = lib.recursiveUpdate common {
    description = "Claude Mem worker for Claude Code and Codex";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" "docker-9router.service" "docker-9router-luna.service" ];
    after = [ "network-online.target" "docker-9router.service" "docker-9router-luna.service" ];
    serviceConfig = {
      RuntimeDirectory = "claude-mem";
      LoadCredential = "gateway-key:/var/lib/secrets/9router-claude-mem.key";
      ExecStartPre = "${tool} prepare";
      ExecStart = "${tool} run";
      ExecStartPost = "${tool} ready";
      Restart = "always";
      RestartSec = "10s";
      TimeoutStartSec = "20min";
    };
  };
  systemd.services.claude-mem-update = lib.recursiveUpdate common {
    description = "Install the promoted Claude Mem release for both host clients";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    onFailure = [ "maintenance-alert@%n.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecCondition = "${tool} stage";
      ExecStart = "+${pkgs.systemd}/bin/systemctl restart claude-mem.service";
      TimeoutStartSec = "20min";
    };
  };
  systemd.timers.claude-mem-update = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 03:45:00 America/New_York";
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };
}
