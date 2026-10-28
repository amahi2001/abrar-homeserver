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
    description = "Renew nginx certificate for the existing Tailscale hostname";
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
    script = ''
      ${config.services.tailscale.package}/bin/tailscale cert --min-validity=168h \
        --cert-file /var/lib/tailscale-nginx-cert/cert.pem \
        --key-file /var/lib/tailscale-nginx-cert/key.pem \
        buildfleet-server.tailcb7cdb.ts.net
      ${pkgs.coreutils}/bin/chmod 0640 /var/lib/tailscale-nginx-cert/{cert,key}.pem
      if ${pkgs.systemd}/bin/systemctl is-active --quiet nginx.service; then
        ${pkgs.systemd}/bin/systemctl reload --no-block nginx.service
      fi
    '';
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
