# АдGuard Home Docker data directories
# These are created by NixOS tmpfiles.rules in containers.nix
# /var/lib/adguardhome/conf/  — AdGuard Home configuration
# /var/lib/adguardhome/work/  — AdGuard Home working data
#
# The AdGuard Home config (AdGuardHome.yaml) lives in /var/lib/adguardhome/conf/
# On first run, AdGuard creates a default config. After setup via the web UI,
# export the config and place it here (with passwords redacted for the repo).
#
# The example config is provided as adguard/AdGuardHome.yaml.example
# Copy it to /var/lib/adguardhome/conf/AdGuardHome.yaml and set a real password.
# containers.nix manages only upstream_dns and fallback_dns before container start:
# Quad9 unfiltered DNS over TLS, with Cloudflare DNS over HTTPS as fallback.
# Other settings, filters, and credentials remain managed through the AdGuard UI.
