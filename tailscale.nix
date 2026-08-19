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
}
