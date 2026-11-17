# Tailscale VPN configuration
# Exit node + mesh VPN for remote access
{ config, pkgs, ... }:
let
  cockpitEnsure = "${pkgs.cockpit}/libexec/cockpit-certificate-ensure";
  cockpitPrepare = pkgs.writeShellScript "cockpit-prepare-tailnet" ''
    set -eu
    certs=/var/lib/tailscale-nginx-cert/cockpit-current
    if ! test -d "$certs"; then
      exec ${cockpitEnsure} --for-cockpit-tls
    fi
    pair=$(${pkgs.coreutils}/bin/readlink -e "$certs")
    exec ${pkgs.util-linux}/bin/unshare --mount --propagation private ${pkgs.runtimeShell} -eu -c '
      ${pkgs.util-linux}/bin/mount --bind "$1" /etc/cockpit/ws-certs.d
      exec ${cockpitEnsure} --for-cockpit-tls
    ' _ "$pair"
  '';
in
{
  services.tailscale = {
    enable = true;
    useRoutingFeatures = "server"; # enables IP forwarding + exit node support
    openFirewall = true;
    extraSetFlags = [
      "--advertise-exit-node" # allow this server to be used as an exit node
      "--accept-routes" # accept subnet routes from other tailnet devices
    ];
  };

  # Allow Tailscale traffic to reach Hermes dashboard and Cockpit
  networking.firewall.interfaces.tailscale0 = {
    allowedTCPPorts = [
      9119
      9090
    ];
  };

  # Required for exit node traffic to route correctly
  # Without this, packets from exit node clients get dropped by reverse path filtering
  # Note: NixOS module only sets this automatically for "client" / "both", not "server"
  networking.firewall.checkReversePath = "loose";

  # Nginx owns tailnet HTTPS; rebuilds must not modify existing Serve routes.
  systemd.services.tailscale-nginx-cert = {
    description = "Renew nginx certificate for the canonical Tailscale hostname";
    after = [ "tailscaled.service" ];
    requires = [ "tailscaled.service" ];
    before = [ "nginx.service" ];
    serviceConfig = {
      Type = "oneshot";
      StateDirectory = "tailscale-nginx-cert";
      StateDirectoryMode = "0750";
      Group = "nginx";
      UMask = "0027";
      TimeoutStartSec = "120s";
      ExecStartPre = "${config.services.tailscale.package}/bin/tailscale wait --timeout=60s";
    };
    path = [ config.services.tailscale.package pkgs.openssl pkgs.jq pkgs.coreutils pkgs.diffutils pkgs.systemd ];
    script = builtins.readFile ./scripts/renew-tailnet-cert.sh;
  };
  # Pin the resolved pair for each Cockpit start. A generation rollback
  # removes this drop-in and restores Cockpit's original certificate choice.
  systemd.services.cockpit = {
    overrideStrategy = "asDropin";
    # The stock pre-start is privileged so it can bootstrap a self-signed
    # certificate. Only the published-pair path uses a private mount.
    serviceConfig.ExecStartPre = [ "" "+${cockpitPrepare}" ];
  };
  systemd.timers.tailscale-nginx-cert = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "daily";
      Persistent = true;
    };
  };
  systemd.services.nginx = {
    wants = [ "tailscale-nginx-cert.service" ];
    after = [ "tailscale-nginx-cert.service" ];
  };
}
