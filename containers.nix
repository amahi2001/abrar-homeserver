# Docker and OCI containers
{
  config,
  pkgs,
  lib,
  ...
}:

let
  adguardHomeDnsConfig = pkgs.writeShellScript "adguardhome-dns-config" ''
    set -euo pipefail
    config=/var/lib/adguardhome/conf/AdGuardHome.yaml
    if [ -f "$config" ]; then
      # Preserve UI settings and credentials while managing upstreams in Nix.
      ${pkgs.yq-go}/bin/yq -i '
        .dns.upstream_dns = ["https://cloudflare-dns.com/dns-query"] |
        .dns.fallback_dns = ["tls://dns10.quad9.net"]
      ' "$config"
    fi
  '';

  adguardHomeUpdate = pkgs.writeShellApplication {
    name = "adguardhome-update";
    runtimeInputs = with pkgs; [
      docker
      systemd
      curl
      dnsutils
      coreutils
      python3
    ];
    text = ''
      exec python3 -P ${./scripts/container-update.py} adguard
    '';
  };
in
{
  # Docker
  virtualisation.docker = {
    enable = true;
    enableOnBoot = true;
    liveRestore = true;
    autoPrune = {
      enable = true;
      dates = "Sun *-*-* 05:00:00 America/New_York";
      # Keep named and anonymous volumes. They contain persistent service data.
      flags = [ ];
    };
  };
  virtualisation.oci-containers.backend = "docker";

  # AdGuard Home — network-wide DNS ad blocking
  virtualisation.oci-containers.containers.adguardhome = {
    image = "adguard/adguardhome:latest";
    autoStart = true;
    ports = [
      "53:53/tcp"
      "53:53/udp"
      "3000:3000/tcp"
      "8080:80/tcp"
    ];
    volumes = [
      "/var/lib/adguardhome/conf:/opt/adguardhome/conf"
      "/var/lib/adguardhome/work:/opt/adguardhome/work"
    ];
    # The maintenance job controls pulls, so rollback tags remain authoritative.
    pull = "never";
  };

  # Run after the OCI module removes the old container, while DNS is stopped.
  systemd.services.docker-adguardhome.preStart = lib.mkAfter ''
    ${adguardHomeDnsConfig}
  '';

  # AdGuard Home data directories
  systemd.tmpfiles.rules = [
    "d /var/lib/adguardhome      0750 buildfleet docker -"
    "d /var/lib/adguardhome/conf 0750 buildfleet docker -"
    "d /var/lib/adguardhome/work 0750 buildfleet docker -"
  ];

  # Pull the mutable image weekly, restart only on a real image change, then
  # require both the HTTP UI and DNS resolver to pass before retaining it.
  systemd.services.adguardhome-update = {
    description = "Guarded AdGuard Home image update";
    after = [
      "docker.service"
      "docker-adguardhome.service"
      "network-online.target"
    ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${adguardHomeUpdate}/bin/adguardhome-update";
      StateDirectory = "container-updates";
      StateDirectoryMode = "0700";
      UMask = "0077";
      TimeoutStartSec = "20min";
      TimeoutStopSec = "5min";
    };
  };

  systemd.timers.adguardhome-update = {
    description = "Weekly guarded AdGuard Home image update";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Wed *-*-* 05:00:00 America/New_York";
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };

  # Disable systemd-resolved stub listener so AdGuard can bind to :53
  services.resolved = {
    enable = true;
    settings.Resolve.DNSStubListener = "no";
  };
}
