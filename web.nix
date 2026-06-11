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
    };
  };
}
