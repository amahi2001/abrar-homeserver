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