# Tailscale VPN configuration
# Exit node + mesh VPN for remote access
{ config, pkgs, ... }:

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
    path = [ config.services.tailscale.package pkgs.openssl pkgs.coreutils pkgs.diffutils pkgs.systemd ];
    script = builtins.readFile ./scripts/renew-tailnet-cert.sh;
  };
  # Pin the resolved pair for each Cockpit start. A generation rollback
  # removes this drop-in and restores Cockpit's original certificate choice.
  systemd.services.cockpit = {
    overrideStrategy = "asDropin";
    serviceConfig = {
      BindReadOnlyPaths = [ "-/var/lib/tailscale-nginx-cert/cockpit-current:/etc/cockpit/ws-certs.d" ];
      ReadWritePaths = [ "-/etc/cockpit/ws-certs.d" ];
      # The packaged '+' hook skips mount isolation. '!' retains it while
      # allowing the helper to copy the key into Cockpit's private runtime dir.
      ExecStartPre = [ "" "!${pkgs.cockpit}/libexec/cockpit-certificate-ensure --for-cockpit-tls" ];
    };
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
