# Public-facing web stack: ACME, Nginx, DuckDNS
{ config, pkgs, ... }:

{
  # DuckDNS dynamic DNS
  services.duckdns = {
    enable = true;
    domains = [ "buildfleet" ];
    tokenFile = "/var/lib/secrets/duckdns-token.txt";
  };

  # ACME / Let's Encrypt (DuckDNS DNS challenge)
  security.acme = {
    acceptTerms = true;
    defaults = {
      email = "buildfleet@proton.me";
      group = "nginx";
      dnsProvider = "duckdns";
      dnsPropagationCheck = true;
    };
    certs."buildfleet.duckdns.org" = {
      domain = "buildfleet.duckdns.org";
      extraDomainNames = [];
      dnsProvider = "duckdns";
      credentialFiles = { "DUCKDNS_TOKEN_FILE" = "/var/lib/secrets/duckdns-token-value"; };
      dnsPropagationCheck = true;
      reloadServices = [ "nginx" ];
      webroot = null;
    };
  };

  # Nginx reverse proxy
  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    recommendedTlsSettings = true;
    recommendedOptimisation = true;
    recommendedGzipSettings = true;

    virtualHosts."buildfleet.duckdns.org" = {
      enableACME = true;
      forceSSL = true;
      locations."/vault" = {
        proxyPass = "http://127.0.0.1:8222";
        proxyWebsockets = true;
      };
      # Support Vaultwarden's web vault which requests assets from the root path
      locations."/app" = {
        proxyPass = "http://127.0.0.1:8222";
      };
      locations."/css" = {
        proxyPass = "http://127.0.0.1:8222";
      };
      locations."/fonts" = {
        proxyPass = "http://127.0.0.1:8222";
      };
      locations."/images" = {
        proxyPass = "http://127.0.0.1:8222";
      };
      locations."~* \\.(css|js|json|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$" = {
        proxyPass = "http://127.0.0.1:8222";
      };
    };
  };

  # Vaultwarden password manager
  services.vaultwarden = {
    enable = true;
    config = {
      DOMAIN = "https://buildfleet.duckdns.org/vault";
      SIGNUPS_ALLOWED = true;

      ROCKET_ADDRESS = "127.0.0.1";
      ROCKET_PORT = 8222;
      ROCKET_LOG = "critical";
    };
  };

  # Bitwarden to Vaultwarden synchronization service and timer
  systemd.services.bitwarden-sync = {
    description = "Bitwarden to Vaultwarden synchronization service";
    path = with pkgs; [ bitwarden-cli deno ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.deno}/bin/deno run --allow-run --allow-read --allow-write --allow-env --allow-net /etc/nixos/scripts/bitwarden-sync.ts";
      EnvironmentFile = "/var/lib/secrets/bitwarden-sync.env";
      User = "root";
    };
  };

  systemd.timers.bitwarden-sync = {
    description = "Timer for Bitwarden to Vaultwarden synchronization";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "hourly";
      Persistent = true;
    };
  };
}
