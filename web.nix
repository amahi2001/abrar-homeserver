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
      extraDomainNames = [ ];
      dnsProvider = "duckdns";
      credentialFiles = {
        "DUCKDNS_TOKEN_FILE" = "/var/lib/secrets/duckdns-token-value";
      };
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
      locations."= /vault" = {
        extraConfig = "return 301 https://buildfleet.duckdns.org/vault/;";
      };
      locations."/vault/" = {
        proxyPass = "http://127.0.0.1:8222";
        proxyWebsockets = true;
      };
    };
  };

  # Vaultwarden password manager
  services.vaultwarden = {
    enable = true;
    config = {
      DOMAIN = "https://buildfleet.duckdns.org/vault";
      # The administrative account already exists; keep public registration
      # closed on this Internet-facing endpoint.
      SIGNUPS_ALLOWED = false;

      ROCKET_ADDRESS = "127.0.0.1";
      ROCKET_PORT = 8222;
      ROCKET_LOG = "critical";
    };
  };

  # Bitwarden to Vaultwarden synchronization service and timer
  systemd.services.bitwarden-sync = {
    description = "Bitwarden to Vaultwarden synchronization service";
    path = with pkgs; [
      bitwarden-cli
      deno
    ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.deno}/bin/deno run --allow-run --allow-read --allow-write --allow-env --allow-net /etc/nixos/scripts/bitwarden-sync.ts";
      EnvironmentFile = "/var/lib/secrets/bitwarden-sync.env";
      User = "root";
    };
  };

  systemd.timers.bitwarden-sync = {
    description = "Daily timer for Bitwarden to Vaultwarden synchronization";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 01:00:00 America/New_York";
      Persistent = true;
    };
  };
}
