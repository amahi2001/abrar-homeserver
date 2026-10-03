# Public-facing web stack: ACME, Nginx, DuckDNS
{ config, pkgs, lib, ... }:

{
  # DuckDNS dynamic DNS
  services.duckdns = {
    enable = true;
    domains = [ "buildfleet" ];
    tokenFile = "/var/lib/secrets/duckdns-token.txt";
  };

  # Retry transient provider failures within the five-minute update interval.
  # Keep the token in the credential file and curl's stdin, never its argv.
  systemd.services.duckdns = {
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = lib.mkForce "oneshot";
      TimeoutStartSec = "180s";
    };
    script = lib.mkForce ''
      set -euo pipefail
      token="$(${pkgs.systemd}/bin/systemd-creds cat DUCKDNS_TOKEN_FILE)"
      domains=${lib.escapeShellArg (lib.concatStringsSep "," config.services.duckdns.domains)}

      if ! response="$(${pkgs.curl}/bin/curl --fail --silent --show-error \
        --connect-timeout 10 --max-time 20 \
        --retry 4 --retry-delay 5 --retry-max-time 120 \
        --config - <<< "url = \"https://www.duckdns.org/update?verbose=true&domains=$domains&token=$token&ip=\"")"; then
        echo "DuckDNS update failed after bounded retries; the next timer run will retry."
        exit 1
      fi
      unset token

      read -r result <<< "$response"
      if [ "$result" != "OK" ]; then
        echo "DuckDNS rejected the update; check the configured domains and credential."
        exit 1
      fi
      echo "DuckDNS update succeeded."
    '';
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
    certs."jellyfin.buildfleet.duckdns.org" = {
      domain = "jellyfin.buildfleet.duckdns.org";
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

    # Keep Jellyfin at the root of its own hostname so existing LAN and
    # Tailscale clients do not need a Jellyfin Base URL change.
    virtualHosts."jellyfin.buildfleet.duckdns.org" = {
      enableACME = true;
      forceSSL = true;
      locations."/" = {
        proxyPass = "http://127.0.0.1:8096";
        proxyWebsockets = true;
        extraConfig = ''
          proxy_buffering off;
          proxy_read_timeout 3600s;
          proxy_send_timeout 3600s;
          # Jellyfin may put API keys in URLs; do not record request paths.
          access_log off;
        '';
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
