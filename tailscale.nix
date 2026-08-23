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

  # Give Transmission a stable, private HTTPS endpoint without publishing its
  # RPC port.  Tailscale Serve terminates TLS on the tailnet and proxies only
  # to the Docker mapping on server loopback.
  systemd.services.tailscale-serve-transmission = {
    description = "Tailscale Serve proxy for Transmission";
    wantedBy = [ "multi-user.target" ];
    after = [
      "tailscaled.service"
      "docker-transmission-vpn.service"
    ];
    requires = [ "tailscaled.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # Tailscale may require a one-time admin approval before Serve can start.
      # Do not let that interactive approval block a full system activation.
      TimeoutStartSec = "20s";
      ExecStart = "${config.services.tailscale.package}/bin/tailscale serve --bg --https=443 http://127.0.0.1:9091";
    };
  };
}
