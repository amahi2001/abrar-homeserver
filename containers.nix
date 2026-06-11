# Docker and OCI containers
{ config, pkgs, ... }:

{
  # Docker
  virtualisation.docker = {
    enable = true;
    enableOnBoot = true;
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
  };

  # AdGuard Home data directories
  systemd.tmpfiles.rules = [
    "d /var/lib/adguardhome      0750 buildfleet docker -"
    "d /var/lib/adguardhome/conf 0750 buildfleet docker -"
    "d /var/lib/adguardhome/work 0750 buildfleet docker -"
  ];

  # Disable systemd-resolved stub listener so AdGuard can bind to :53
  services.resolved = {
    enable = true;
    settings.Resolve.DNSStubListener = "no";
  };
}
