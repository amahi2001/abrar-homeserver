# Private file services: Nextcloud, VPN-isolated Transmission, and LAN/Tailscale SMB shares.
{
  config,
  pkgs,
  lib,
  ...
}:

let
  mediaQueue = pkgs.writeShellApplication {
    name = "media-queue";
    runtimeInputs = [
      pkgs.docker
      pkgs.python3
    ];
    text = ''
      exec ${pkgs.python3}/bin/python ${./skills/media-queue/scripts/media_queue.py} "$@"
    '';
  };

  mediaLibrary = pkgs.writeShellApplication {
    name = "media-library";
    runtimeInputs = [
      pkgs.systemd
      pkgs.python3
      pkgs.curl
    ];
    text = ''
      set -eu
      usage() {
        echo "Usage: media-library <status|start|stop> <sonarr|radarr> | organize-<tv|movie> …" >&2
        exit 2
      }
      [ "$#" -ge 1 ] || usage
      case "$1" in
        organize-tv|organize-movie)
          exec ${pkgs.python3}/bin/python ${./skills/media-queue/scripts/media_library.py} "$@"
          ;;
      esac
      [ "$#" -eq 2 ] || usage
      case "$2" in
        sonarr)
          service="sonarr.service"
          api="http://127.0.0.1:8989/ping"
          ;;
        radarr)
          service="radarr.service"
          api="http://127.0.0.1:7878/ping"
          ;;
        *) usage ;;
      esac
      case "$1" in
        status)
          systemctl show "$service" -p ActiveState -p MemoryCurrent --no-pager
          ;;
        start)
          systemctl start "$service"
          systemctl is-active --quiet "$service"
          attempt=0
          while [ "$attempt" -lt 30 ]; do
            attempt=$((attempt + 1))
            if curl --silent --fail --max-time 1 "$api" >/dev/null; then
              echo "$2 is ready for this library-management request."
              exit 0
            fi
            sleep 1
          done
          echo "$2 started but its API did not become ready in time." >&2
          exit 1
          ;;
        stop)
          systemctl stop "$service"
          echo "$2 stopped; it will not consume idle memory."
          ;;
        *) usage ;;
      esac
    '';
  };

  mediaOrganizer = pkgs.writeShellApplication {
    name = "media-organizer";
    runtimeInputs = [
      pkgs.docker
      pkgs.python3
      pkgs.systemd
      mediaLibrary
    ];
    text = ''
      exec ${pkgs.python3}/bin/python ${./skills/media-queue/scripts/media_organizer.py} "$@"
    '';
  };

  mediaCleanup = pkgs.writeShellApplication {
    name = "media-cleanup";
    runtimeInputs = [
      pkgs.docker
      pkgs.python3
      pkgs.systemd
      mediaLibrary
    ];
    text = ''
      exec ${pkgs.python3}/bin/python ${./skills/media-cleanup/scripts/media_cleanup.py} "$@"
    '';
  };

  transmissionVpnSettings = pkgs.writeText "transmission-vpn-settings.json" (
    builtins.toJSON {
      # Keep manual/Hermes downloads out of the media-library root. Servarr
      # routes its own releases to the category-specific subdirectories below.
      "download-dir" = "/downloads/library/manual";
      "incomplete-dir" = "/downloads/incomplete";
      "incomplete-dir-enabled" = true;
      "peer-port" = 51413;
      "peer-port-random-on-start" = false;
      "port-forwarding-enabled" = false;
      "rpc-bind-address" = "0.0.0.0";
      "rpc-port" = 9091;
      # Tailscale Serve is the only host-level entry point, but require a
      # separate RPC login as defense in depth for other tailnet members.
      "rpc-authentication-required" = true;
      "rpc-username" = "buildfleet";
      "rpc-whitelist-enabled" = false;
      "rpc-host-whitelist-enabled" = false;
      "rename-partial-files" = true;
      "start-added-torrents" = true;
      "trash-original-torrent-files" = true;
      "umask" = 2;
      "lpd-enabled" = false;
    }
  );

  transmissionVpnSettingsSetup = pkgs.writeShellScript "transmission-vpn-settings-setup" ''
    set -euo pipefail
    umask 077

    secret=/var/lib/secrets/transmission-rpc-password
    environment=/var/lib/secrets/transmission-rpc.env
    settings=/var/lib/transmission-vpn/settings.json

    if [ ! -s "$secret" ]; then
      ${pkgs.openssl}/bin/openssl rand -base64 32 > "$secret"
    fi

    ${pkgs.coreutils}/bin/install -D -m 0600 -o root -g root /dev/null "$environment"
    {
      echo "USER=buildfleet"
      echo "PASS=$(<"$secret")"
    } > "$environment"

    ${pkgs.coreutils}/bin/install -D -m 0600 -o transmission -g transmission ${transmissionVpnSettings} "$settings"
  '';
in
{
  environment.systemPackages = [
    mediaQueue
    mediaLibrary
    mediaOrganizer
    mediaCleanup
  ];

  users.groups = {
    # Preserve the existing IDs so the already-downloaded media remains owned
    # by the same service account after migrating off the native service.
    media = {
      gid = 987;
    };
    files = { };
    transmission = {
      gid = 70;
    };
  };

  # The service accounts may access only the directories their respective
  # services require. Nextcloud's own data is never exported through Samba.
  users.users.transmission = {
    isSystemUser = true;
    uid = 70;
    group = "transmission";
    extraGroups = [ "media" ];
  };
  users.users.nextcloud.extraGroups = [
    "files"
    "media"
  ];
  # Jellyfin reads only the completed, Servarr-organized library.  The
  # downloads/incomplete area remains unavailable to it.
  users.users.jellyfin.extraGroups = [
    "media"
    "render"
    "video"
  ];

  systemd.tmpfiles.rules = [
    "d /srv/data                    0755 root         root  -"
    "d /srv/data/inbox              0770 buildfleet   files -"
    "d /srv/data/media              0770 transmission media -"
    "d /srv/data/media/incomplete   0700 transmission media -"
    "d /srv/data/media/library      0775 transmission media -"
    "d /srv/data/media/library/manual 2775 transmission media -"
    "d /srv/data/media/library/movies 2775 transmission media -"
    "d /srv/data/media/library/tv     2775 transmission media -"
    "d /srv/data/media/.media-cleanup-trash 0700 root root -"
    "d /var/lib/gluetun             0700 root         root  -"
    "d /var/lib/secrets             0700 root         root  -"
    "d /var/lib/transmission-vpn    0750 transmission media -"
    "d /var/lib/media-queue         0700 root         root  -"
    "d /var/lib/media-queue/events  0700 root         root  -"
  ];

  # Disable the native service: unlike the container below it has the host's
  # network namespace and therefore cannot be protected by the VPN kill switch.
  services.transmission = {
    enable = false;
    openPeerPorts = false;
  };

  # Gluetun owns Transmission's entire network namespace. Its firewall blocks
  # all non-VPN egress until the OpenVPN tunnel is established and again if the
  # tunnel drops. Only the RPC UI is published, and only on host loopback.
  # The OpenVPN file is a root-only runtime secret, deliberately outside both
  # the Git tree and the Nix store.
  virtualisation.oci-containers.containers = {
    gluetun = {
      # Pinned after a verified pull: upgrades remain an explicit review step.
      image = "qmcgaw/gluetun@sha256:1a5bf4b4820a879cdf8d93d7ef0d2d963af56670c9ebff8981860b6804ebc8ab";
      autoStart = true;
      devices = [ "/dev/net/tun:/dev/net/tun" ];
      extraOptions = [ "--cap-add=NET_ADMIN" ];
      environment = {
        VPN_SERVICE_PROVIDER = "custom";
        VPN_TYPE = "openvpn";
        OPENVPN_CUSTOM_CONFIG = "/gluetun/vpn-unlimited.ovpn";
        TZ = "America/New_York";
      };
      volumes = [
        "/var/lib/gluetun:/gluetun"
        "/var/lib/secrets/vpn-unlimited-openvpn.ovpn:/gluetun/vpn-unlimited.ovpn:ro"
      ];
      ports = [ "127.0.0.1:9091:9091/tcp" ];
    };

    transmission-vpn = {
      image = "lscr.io/linuxserver/transmission@sha256:7356451f32395838628b241f33bcd7130df9302777fc23f80cbc7ca8b4997c02";
      autoStart = true;
      dependsOn = [ "gluetun" ];
      environmentFiles = [ "/var/lib/secrets/transmission-rpc.env" ];
      # Sharing Gluetun's namespace is the key guarantee: Transmission has no
      # route to the host network that could bypass the VPN firewall.
      extraOptions = [ "--network=container:gluetun" ];
      environment = {
        PUID = "70";
        PGID = "987";
        TZ = "America/New_York";
        UMASK = "002";
        PEERPORT = "51413";
        TRANSMISSION_DOWNLOAD_DIR = "/downloads/library/manual";
        TRANSMISSION_INCOMPLETE_DIR = "/downloads/incomplete";
      };
      volumes = [
        "/var/lib/transmission-vpn:/config"
        "/srv/data/media:/downloads"
      ];
    };
  };

  # Keep an immutable declarative baseline for the container on each start;
  # the image may add its own non-security defaults around it.  The RPC
  # password is generated once at activation and stays outside the Nix store.
  systemd.services.docker-transmission-vpn = {
    after = [ "docker-gluetun.service" ];
    requires = [ "docker-gluetun.service" ];
    # If Gluetun stops or its network namespace disappears, stop Transmission
    # too. It cannot leak without the shared namespace, and this makes the
    # fail-closed relationship explicit at the service-manager layer.
    bindsTo = [ "docker-gluetun.service" ];
    serviceConfig.ExecStartPre = lib.mkBefore [
      "+${transmissionVpnSettingsSetup}"
    ];
  };

  # Poll completed torrents independently of Hermes so downloads added through
  # either media-queue or Transmission's UI are organized. Sonarr/Radarr are
  # started only for an import and stopped again to preserve low idle memory.
  systemd.services.media-organizer = {
    description = "Organize completed Transmission media for Jellyfin";
    after = [ "docker-transmission-vpn.service" ];
    wants = [ "docker-transmission-vpn.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${mediaOrganizer}/bin/media-organizer run";
      UMask = "0077";
      Nice = 10;
      IOSchedulingClass = "idle";
      IOSchedulingPriority = 7;
      TimeoutStartSec = "10min";
    };
  };

  systemd.timers.media-organizer = {
    description = "Watch Transmission for completed media";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "2min";
      RandomizedDelaySec = "15s";
      Persistent = true;
    };
  };

  # Google Drive-like UI, desktop/mobile sync, sharing, and WebDAV. The
  # existing Nginx/ACME virtual host supplies HTTPS at the root URL; Vaultwarden
  # keeps its more-specific /vault/ location.
  services.nextcloud = {
    enable = true;
    package = pkgs.nextcloud33;
    hostName = "buildfleet.duckdns.org";
    https = true;
    datadir = "/srv/data/nextcloud";
    maxUploadSize = "20G";
    database.createLocally = true;
    configureRedis = true;
    config = {
      dbtype = "pgsql";
      adminuser = "buildfleet";
      adminpassFile = "/var/lib/secrets/nextcloud-admin-password";
    };
    settings = {
      overwriteprotocol = "https";
      default_phone_region = "US";
    };
  };

  # Local-first media server for the Shield and remote Tailscale devices.
  # Compatible clients Direct Play, so this service normally leaves the GPU
  # idle.  NVENC/NVDEC is available only when a client genuinely needs a
  # transcode (for example, remote bandwidth or codec compatibility).
  services.jellyfin = {
    enable = true;
    openFirewall = false;
    # Jellyfin 10.11.11 can replace an HLS remux job while another concurrent
    # segment response is still reading its output. Pin the upstream job-locking
    # fix and atomically publish completed HLS files until both are in a stable
    # release.
    package = pkgs.jellyfin.overrideAttrs (old: {
      patches = (old.patches or [ ]) ++ [
        (pkgs.fetchurl {
          url = "https://github.com/jellyfin/jellyfin/commit/e2586eed9b04d501cd5805711cb6ad5553c1816b.patch";
          hash = "sha256-Nmhwg9Hlja5mHlkRqYHACSuZJlGdlIVlC9xa+6RCN1E=";
        })
        (pkgs.writeText "jellyfin-hls-atomic-segments.patch" ''
          diff --git a/Jellyfin.Api/Controllers/DynamicHlsController.cs b/Jellyfin.Api/Controllers/DynamicHlsController.cs
          --- a/Jellyfin.Api/Controllers/DynamicHlsController.cs
          +++ b/Jellyfin.Api/Controllers/DynamicHlsController.cs
          @@ -1617,7 +1617,9 @@ public class DynamicHlsController : BaseJellyfinApiController
                   var segmentFormat = string.Empty;
                   var segmentContainer = outputExtension.TrimStart('.');
                   var inputModifier = _encodingHelper.GetInputModifier(state, _encodingOptions, segmentContainer);
          -        var hlsArguments = $"-hls_playlist_type {(isEventPlaylist ? "event" : "vod")} -hls_list_size 0";
          +        // Publish segments and playlists only after FFmpeg has finished writing them. This also
          +        // prevents replacement HLS jobs from growing a file while it is being served to a client.
          +        var hlsArguments = $"-hls_flags temp_file -hls_playlist_type {(isEventPlaylist ? "event" : "vod")} -hls_list_size 0";

                   if (string.Equals(segmentContainer, "ts", StringComparison.OrdinalIgnoreCase))
                   {
        '')
        (pkgs.writeText "jellyfin-direct-play-error-reencode.patch" ''
          diff --git a/MediaBrowser.Controller/MediaEncoding/EncodingHelper.cs b/MediaBrowser.Controller/MediaEncoding/EncodingHelper.cs
          --- a/MediaBrowser.Controller/MediaEncoding/EncodingHelper.cs
          +++ b/MediaBrowser.Controller/MediaEncoding/EncodingHelper.cs
          @@ -25,5 +25,6 @@ using MediaBrowser.Model.Dlna;
           using MediaBrowser.Model.Dto;
           using MediaBrowser.Model.Entities;
           using MediaBrowser.Model.MediaInfo;
          +using MediaBrowser.Model.Session;
           using Microsoft.Extensions.Configuration;
           using IConfigurationManager = MediaBrowser.Common.Configuration.IConfigurationManager;
          @@ -2340,6 +2341,14 @@ namespace MediaBrowser.Controller.MediaEncoding
                   {
                       var request = state.BaseRequest;
          ${" "}
          +            // A client reporting DirectPlayError has already failed to decode the original
          +            // video path. Re-encoding the video makes the HLS fallback self-contained instead
          +            // of copying the same elementary stream into a different container.
          +            if ((state.TranscodeReasons & TranscodeReason.DirectPlayError) != 0)
          +            {
          +                return false;
          +            }
          +
                       if (!request.AllowVideoStreamCopy)
                       {
                           return false;
        '')
        (pkgs.writeText "jellyfin-mulan-androidtv-transcode.patch" ''
          diff --git a/Jellyfin.Api/Helpers/MediaInfoHelper.cs b/Jellyfin.Api/Helpers/MediaInfoHelper.cs
          --- a/Jellyfin.Api/Helpers/MediaInfoHelper.cs
          +++ b/Jellyfin.Api/Helpers/MediaInfoHelper.cs
          @@ -212,6 +212,25 @@ public class MediaInfoHelper
          ${" "}
                   var user = _userManager.GetUserById(userId) ?? throw new ResourceNotFoundException();
          ${" "}
          +        // This H.264 stream deterministically crashes the Fire TV decoder at 00:52:29.
          +        // Skip the client's Direct Play attempt so playback starts on the verified NVENC path.
          +        if (string.Equals(profile.Name, "AndroidTV-Default", StringComparison.Ordinal)
          +            && string.Equals(
          +                mediaSource.Path,
          +                "/srv/data/media/library/movies/Mulan (1998)/Mulan.1998.1080p.BRrip.x264.GAZ.YIFY.mp4",
          +                StringComparison.Ordinal))
          +        {
          +            _logger.LogInformation("Forcing video transcode for known Android TV decoder-incompatible media: {Path}", mediaSource.Path);
          +            options.EnableDirectPlay = false;
          +            options.EnableDirectStream = false;
          +            options.AllowVideoStreamCopy = false;
          +            mediaSource.SupportsDirectPlay = false;
          +            mediaSource.SupportsDirectStream = false;
          +            enableDirectPlay = false;
          +            enableDirectStream = false;
          +            allowVideoStreamCopy = false;
          +        }
          +
                   if (!enableDirectPlay)
                   {
                       mediaSource.SupportsDirectPlay = false;
        '')
      ];
    });
    hardwareAcceleration = {
      enable = true;
      type = "nvenc";
      device = "/dev/dri/by-path/pci-0000:08:00.0-render";
    };
    # Keep video-encoding policy reproducible across restarts instead of
    # relying on mutable dashboard-only settings.
    forceEncodingConfig = true;
    transcoding = {
      maxConcurrentStreams = 2;
      enableHardwareEncoding = true;
      enableToneMapping = true;
      throttleTranscoding = true;
      hardwareDecodingCodecs = {
        h264 = true;
        hevc = true;
        hevc10bit = true;
        mpeg2 = true;
        vc1 = true;
        vp8 = true;
        vp9 = true;
        av1 = true;
      };
      # The RTX 3060 supports HEVC, but not AV1, hardware encoding.
      hardwareEncodingCodecs.hevc = true;
    };
  };

  # The NixOS Jellyfin module grants the DRM render node, but NVIDIA's CUDA
  # path also opens these character devices. Keep the sandbox closed to every
  # other device while permitting only the nodes required for NVDEC/NVENC.
  systemd.services.jellyfin.serviceConfig.DeviceAllow = [
    "/dev/nvidia0 rw"
    "/dev/nvidiactl rw"
    "/dev/nvidia-uvm rw"
  ];

  # A fast network-drive view for the two explicitly shared folders. SMB is
  # reachable only on the trusted home Wi-Fi and via Tailscale—never from the
  # public Internet.
  services.samba = {
    enable = true;
    # NetBIOS and WSD make the authenticated shares discoverable to local
    # clients such as Android TV/VLC and modern desktop file browsers.
    nmbd.enable = true;
    winbindd.enable = false;
    openFirewall = false;
    settings = {
      global = {
        "server min protocol" = "SMB3_00";
        "map to guest" = "Never";
        "disable netbios" = "no";
      };
      Media = {
        path = "/srv/data/media/library";
        comment = "Completed media downloads";
        browseable = "yes";
        "guest ok" = "no";
        "read only" = "yes";
        "valid users" = [ "buildfleet" ];
      };
      Inbox = {
        path = "/srv/data/inbox";
        comment = "Personal file drop";
        browseable = "yes";
        "guest ok" = "no";
        "read only" = "no";
        "valid users" = [ "buildfleet" ];
        "force user" = "buildfleet";
        "force group" = "files";
        "create mask" = "0660";
        "directory mask" = "0770";
      };
    };
  };

  # WSD discovery is restricted to the physical Wi-Fi network. Remote
  # Tailscale clients retain SMB access but do not receive LAN broadcasts.
  services.samba-wsdd = {
    enable = true;
    interface = "wlp6s0";
    openFirewall = false;
  };

  networking.firewall.interfaces = {
    # Remote access: authenticated SMB and Jellyfin over the encrypted tailnet.
    tailscale0.allowedTCPPorts = [
      445
      8096
    ];

    # Local access and discovery only on the trusted 192.168.1.0/24 Wi-Fi.
    wlp6s0 = {
      allowedTCPPorts = [
        445
        5357
        8096
      ];
      allowedUDPPorts = [
        137
        138
        3702
      ];
    };

    # Transmission reaches Prowlarr's local download handoff through Gluetun's
    # private Docker namespace. This is not reachable from LAN or the Internet.
    docker0.allowedTCPPorts = [ 9696 ];
  };
}
