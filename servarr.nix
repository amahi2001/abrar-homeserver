# Native Servarr media automation services.
# Web ports are intentionally not opened on the normal firewall; access is
# limited to tailscale0 in configuration.nix.
{
  config,
  lib,
  pkgs,
  ...
}:
{
  # FlareSolverr is an unauthenticated local service. Keep it on loopback so
  # only the local Prowlarr process can reach it; never expose port 8191 to
  # Tailscale, LAN, or the public internet.
  services.flaresolverr = {
    enable = true;
    openFirewall = false;
    port = 8191;
  };
  systemd.services.flaresolverr.environment = {
    HOST = "127.0.0.1";
    LOG_LEVEL = "info";
    LOG_HTML = "false";
    TZ = "America/New_York";
  };

  services.prowlarr = {
    enable = true;
    openFirewall = false;
    settings = {
      update.mechanism = "external";
      log.analyticsEnabled = false;
      server = {
        bindaddress = "0.0.0.0";
        port = 9696;
      };
    };
  };

  services.sonarr = {
    enable = true;
    openFirewall = false;
    user = "sonarr";
    group = "sonarr";
    settings = {
      update.mechanism = "external";
      log.analyticsEnabled = false;
      server = {
        bindaddress = "0.0.0.0";
        port = 8989;
      };
    };
  };

  services.radarr = {
    enable = true;
    openFirewall = false;
    user = "radarr";
    group = "radarr";
    settings = {
      update.mechanism = "external";
      log.analyticsEnabled = false;
      server = {
        bindaddress = "0.0.0.0";
        port = 7878;
      };
    };
  };

  # Media files are owned by transmission:media. Servarr needs library access
  # for import/rename; incomplete remains private to Transmission.
  users.users.sonarr.extraGroups = [ "media" ];
  users.users.radarr.extraGroups = [ "media" ];

  # The upstream modules enable PrivateUsers, which prevents these services
  # from using the host media group. Keep the other hardening and only disable
  # this one namespace feature needed for the bind-mounted media tree.
  systemd.services.sonarr.serviceConfig = {
    PrivateUsers = lib.mkForce false;
    ReadWritePaths = [ "/srv/data/media/library" ];
    # Library roots are setgid media directories. Give that group access to
    # newly imported files while keeping them unavailable to other local users.
    UMask = lib.mkForce "0007";
  };
  systemd.services.radarr.serviceConfig = {
    PrivateUsers = lib.mkForce false;
    ReadWritePaths = [ "/srv/data/media/library" ];
    UMask = lib.mkForce "0007";
  };

  # Repair existing Arr-created paths and enforce the same private, shared
  # media mode on every rebuild. This is limited to the SMB Media tree; Arr
  # config files remain owner-only (0600) in their service state directories.
  system.activationScripts.mediaLibraryPermissions = lib.stringAfter [ "users" ] ''
    if [ -d /srv/data/media/library ]; then
      ${pkgs.findutils}/bin/find /srv/data/media/library -exec ${pkgs.coreutils}/bin/chgrp media {} +
      ${pkgs.findutils}/bin/find /srv/data/media/library -type d -exec ${pkgs.coreutils}/bin/chmod 2770 {} +
      ${pkgs.findutils}/bin/find /srv/data/media/library -type f -exec ${pkgs.coreutils}/bin/chmod 0660 {} +
    fi
  '';

  # Hermes starts these only for an explicit library-management request. They
  # retain their configuration but do not consume memory or poll indexers when
  # the server is otherwise idle.
  systemd.services.sonarr.wantedBy = lib.mkForce [ ];
  systemd.services.radarr.wantedBy = lib.mkForce [ ];
  systemd.services.sonarr.environment.DOTNET_GCConserveMemory = "5";
  systemd.services.radarr.environment.DOTNET_GCConserveMemory = "5";
  # Prowlarr stays online for Hermes search; tune only its managed heap.
  systemd.services.prowlarr.environment.DOTNET_GCConserveMemory = "5";

  # Transmission creates one completed-download directory per Servarr
  # category. They must be writable by transmission and readable/writable by
  # the media group so Sonarr and Radarr can import and rename releases.
  # Sonarr and Radarr run with static service users, so their local API-key
  # configuration files can be owner-only. Prowlarr uses a DynamicUser: its
  # state directory is id-mapped, so host-side `nobody:nogroup` maps to the
  # runtime Prowlarr user inside the service. This preserves owner-only access
  # without preventing the service from reading or rewriting config.xml.
  systemd.tmpfiles.rules = [
    "d /srv/data/media/library/tv-sonarr       2775 transmission media -"
    "d /srv/data/media/library/radarr          2775 transmission media -"
    "z /var/lib/prowlarr/config.xml            0600 nobody       nogroup -"
    "z /var/lib/sonarr/.config/NzbDrone/config.xml 0600 sonarr    sonarr -"
    "z /var/lib/radarr/.config/Radarr/config.xml   0600 radarr    radarr -"
  ];

  # Ensure service startup waits for the network stack, while remaining
  # independent of the VPN-isolated Transmission container.
  systemd.services.prowlarr.after = [ "network-online.target" ];
  systemd.services.prowlarr.wants = [ "network-online.target" ];
  systemd.services.sonarr.after = [ "network-online.target" ];
  systemd.services.sonarr.wants = [ "network-online.target" ];
  systemd.services.radarr.after = [ "network-online.target" ];
  systemd.services.radarr.wants = [ "network-online.target" ];

  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [
    7878
    8989
    9696
  ];
}
