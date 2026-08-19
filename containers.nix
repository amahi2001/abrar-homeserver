# Docker and OCI containers
{ config, pkgs, ... }:

let
  adguardHomeUpdate = pkgs.writeShellScript "adguardhome-update" ''
    set -euo pipefail

    docker="${pkgs.docker}/bin/docker"
    systemctl="${pkgs.systemd}/bin/systemctl"
    curl="${pkgs.curl}/bin/curl"
    dig="${pkgs.dnsutils}/bin/dig"
    grep="${pkgs.gnugrep}/bin/grep"
    image="adguard/adguardhome:latest"
    rollback_image="adguard/adguardhome:rollback"
    service="docker-adguardhome.service"

    before="$($docker image inspect "$image" --format '{{.Id}}')"
    echo "Pulling $image (current: $before)"
    $docker pull "$image"
    after="$($docker image inspect "$image" --format '{{.Id}}')"

    if [ "$before" = "$after" ]; then
      echo "AdGuard Home image unchanged."
      exit 0
    fi

    echo "New AdGuard Home image found: $after"
    $docker image tag "$before" "$rollback_image"
    $systemctl restart "$service"

    healthy=false
    for _ in {1..30}; do
      if $systemctl is-active --quiet "$service" \
        && $curl --fail --silent --show-error --max-time 5 -o /dev/null http://127.0.0.1:8080/ \
        && $dig +time=5 +tries=1 @127.0.0.1 example.com A | $grep -q 'status: NOERROR'; then
        healthy=true
        break
      fi
      sleep 2
    done

    if [ "$healthy" != true ]; then
      echo "AdGuard Home health check failed; restoring $before."
      $docker image tag "$rollback_image" "$image"
      $systemctl restart "$service"
      $systemctl is-active --quiet "$service"
      exit 1
    fi

    $docker image rm "$rollback_image" || true
    echo "AdGuard Home update healthy: $before -> $after"
  '';
in
{
  # Docker
  virtualisation.docker = {
    enable = true;
    enableOnBoot = true;
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
  };

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
      ExecStart = adguardHomeUpdate;
      TimeoutStartSec = "10min";
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
