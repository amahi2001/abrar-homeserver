{
  config,
  lib,
  pkgs,
  ...
}:
let
  downloader = pkgs.yt-dlp.override { jsRuntime = pkgs.nodejs; };
  cli = pkgs.writeShellApplication {
    name = "youtube-queue";
    runtimeInputs = [
      pkgs.python3
      pkgs.systemd
      pkgs.ffmpeg-headless
    ];
    text = ''
      exec ${pkgs.python3}/bin/python ${./skills/youtube-queue/scripts/youtube_queue.py} "$@"
    '';
  };
in
{
  environment.systemPackages = [ cli ];
  environment.etc."youtube-queue.json".text = builtins.toJSON {
    ytDlp = lib.getExe downloader;
    reserveBytes = 5 * 1024 * 1024 * 1024;
    jellyfinUrl = "http://127.0.0.1:8096";
    publicJellyfinUrl = "https://jellyfin.amahi.dev";
    jellyfinKeyFile = "/var/lib/secrets/youtube-jellyfin-api-key";
  };

  users.users.youtube-download = {
    isSystemUser = true;
    group = "media";
  };

  systemd.tmpfiles.rules = [
    "d /var/lib/youtube-queue                  0700 root             root  -"
    "d /srv/data/media/.youtube-incomplete     2770 youtube-download media -"
    "d /srv/data/media/library/youtube         2770 youtube-download media -"
    "L+ /var/lib/hermes/.hermes/skills/media/youtube-queue - - - - ${./skills/youtube-queue}"
    "C+ /var/lib/hermes/.hermes/scripts/youtube-queue-watch.sh 0755 hermes hermes - ${./skills/youtube-queue/scripts/youtube-queue-watch.sh}"
  ];

  systemd.services.hermes-agent.serviceConfig.ReadWritePaths = lib.mkAfter [
    "/var/lib/youtube-queue"
  ];

  systemd.services.youtube-queue-worker = {
    description = "Download queued YouTube videos directly and publish to Jellyfin";
    after = [
      "network-online.target"
      "jellyfin.service"
    ];
    wants = [ "network-online.target" ];
    path = [ cli ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${cli}/bin/youtube-queue worker";
      TimeoutStartSec = "infinity";
      TimeoutStopSec = "45s";
      UMask = "0077";
      Nice = 10;
      IOSchedulingClass = "idle";
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = true;
      MemoryMax = "2G";
      CPUQuota = "200%";
      TasksMax = 128;
      ReadWritePaths = [
        "/var/lib/youtube-queue"
        # A single bind mount keeps staging and publication on the same mount.
        # Separate writable bind mounts make directory rename fail with EXDEV.
        "/srv/data/media"
      ];
    };
  };

  systemd.timers.youtube-queue-worker = {
    description = "Resume pending YouTube downloads and verify Jellyfin indexing";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "1min";
      OnUnitInactiveSec = "1min";
      Persistent = true;
    };
  };

  # The Python suite uses fake downloader/Jellyfin boundaries and temporary media,
  # so flake validation never downloads videos or touches the running library.
  assertions = [
    {
      assertion = config.services.jellyfin.enable;
      message = "The YouTube queue requires Jellyfin.";
    }
  ];
}
