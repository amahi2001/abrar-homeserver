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
      "rpc-authentication-required" = false;
      "rpc-whitelist-enabled" = false;
      "rpc-host-whitelist-enabled" = false;
      "rename-partial-files" = true;
      "start-added-torrents" = true;
      "trash-original-torrent-files" = true;
      "umask" = 2;
      "lpd-enabled" = false;
    }
  );
in
{
  environment.systemPackages = [
    mediaQueue
    mediaLibrary
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
    "d /var/lib/gluetun             0700 root         root  -"
    "d /var/lib/transmission-vpn    0750 transmission media -"
    "d /var/lib/media-queue         0700 root         root  -"
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
  # the image may add its own non-security defaults around it.
  systemd.services.docker-transmission-vpn = {
    after = [ "docker-gluetun.service" ];
    requires = [ "docker-gluetun.service" ];
    # If Gluetun stops or its network namespace disappears, stop Transmission
    # too. It cannot leak without the shared namespace, and this makes the
    # fail-closed relationship explicit at the service-manager layer.
    bindsTo = [ "docker-gluetun.service" ];
    serviceConfig.ExecStartPre = lib.mkBefore [
      "+${pkgs.coreutils}/bin/install -D -m 0640 -o transmission -g media ${transmissionVpnSettings} /var/lib/transmission-vpn/settings.json"
    ];
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
