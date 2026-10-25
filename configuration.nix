{
  config,
  pkgs,
  lib,
  cloakbrowser,
  codexCli,
  ...
}:

{
  imports = [
    ./hardware-configuration.nix
    ./desktop.nix
    ./web.nix
    ./containers.nix
    ./github-actions.nix
    ./development.nix
    ./token-eye.nix
    ./files.nix
    ./servarr.nix
    ./hermes.nix
    ./tailscale.nix
    ./update.nix
    ./mcp-updates.nix
    ./runtime-security.nix
  ];

  # Bootloader
  boot.loader.grub.enable = true;
  boot.loader.grub.device = "/dev/sda";
  boot.loader.grub.useOSProber = true;

  # Hostname
  networking.hostName = "buildfleet-server";

  # Networking
  networking.networkmanager.enable = true;
  # Wi-Fi is the only remote access path, so favour connection reliability over
  # the small savings from client-oriented Wi-Fi power saving.
  networking.networkmanager.wifi.powersave = false;

  # Time zone and locale
  time.timeZone = "America/New_York";
  i18n.defaultLocale = "en_US.UTF-8";
  i18n.extraLocaleSettings = {
    LC_ADDRESS = "en_US.UTF-8";
    LC_IDENTIFICATION = "en_US.UTF-8";
    LC_MEASUREMENT = "en_US.UTF-8";
    LC_MONETARY = "en_US.UTF-8";
    LC_NAME = "en_US.UTF-8";
    LC_NUMERIC = "en_US.UTF-8";
    LC_PAPER = "en_US.UTF-8";
    LC_TELEPHONE = "en_US.UTF-8";
    LC_TIME = "en_US.UTF-8";
  };

  # Users and groups
  users.users.buildfleet = {
    isNormalUser = true;
    description = "Buildfleet";
    extraGroups = [
      "networkmanager"
      "wheel"
      "docker"
      "hermes"
      "nixconfig"
      "media"
      "files"
    ];
    packages = with pkgs; [ ];
  };

  users.groups.nixconfig = {
    gid = 1001;
  };

  # Sudo
  security.sudo.enable = true;
  security.sudo.extraRules = [
    {
      groups = [ "wheel" ];
      commands = [
        {
          command = "ALL";
          options = [
            "NOPASSWD"
            "SETENV"
          ];
        }
      ];
    }
  ];

  # System packages
  nixpkgs.config.allowUnfree = true;
  # Enforce security floors until nixpkgs catches up; never downgrade a newer package.
  nixpkgs.overlays = [
    (
      _final: prev:
      let
        atLeast =
          package: version: override:
          if lib.versionOlder package.version version then package.overrideAttrs override else package;
      in
      {
        # Stable lags the CLI's fixes; prefer the newer already-locked package.
        bitwarden-cli =
          let
            candidate = codexCli.inputs.nixpkgs.legacyPackages.${prev.stdenv.hostPlatform.system}.bitwarden-cli;
          in
          if lib.versionOlder prev.bitwarden-cli.version candidate.version then
            candidate
          else
            prev.bitwarden-cli;
        bun = atLeast prev.bun "1.4.4" (_old: rec {
          version = "1.4.4";
          src = prev.fetchurl {
            url = "https://github.com/oven-sh/bun/releases/download/bun-v${version}/bun-linux-x64.zip";
            hash = "sha256-v4zL+ysLgBWeD5ptFcVTF63YB3U028YTCj7bUvzLOVw=";
          };
        });
      }
    )
  ];
  environment.systemPackages = with pkgs; [
    wget
    unzip
    vim
    pciutils
    iw
    gnumake
    git
    gh
    gcc
    bc
    linuxHeaders
    docker
    docker-compose
    ripgrep
    fd
    gzip
    certbot
    dnsutils
    btop
    lm_sensors
    bun
    # Latest release from the dedicated Codex flake, not the older stable
    # nixpkgs package. Updated by codex-cli-update.service.
    codexCli.packages.x86_64-linux.default
    rtk
    cloakbrowser.packages.x86_64-linux.default
    bitwarden-cli
  ];

  # CPU frequency scaling
  powerManagement = {
    enable = true;
    cpuFreqGovernor = lib.mkDefault "schedutil";
  };

  # nix-ld for running non-Nix binaries
  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = with pkgs; [
    stdenv.cc.cc
    openssl
    zlib
    libunwind
    libuuid
    curl
  ];

  # SSH
  services.openssh.enable = true;

  # Cockpit - Web-based server management
  services.cockpit = {
    enable = true;
    port = 9090;
    settings = {
      WebService = {
        Origins = lib.mkForce "https://buildfleet-server.tailcb7cdb.ts.net:9090 https://100.91.234.17:9090";
      };
    };
  };

  # Firewall
  networking.firewall.allowedTCPPorts = [
    22
    80
    443
    53
    8080
  ];
  networking.firewall.allowedUDPPorts = [ 53 ];
  networking.firewall.enable = true;
  networking.firewall.logRefusedConnections = true;

  # Keep the repository itself collaborative without recursively marking every
  # tracked source file executable on each rebuild.
  systemd.tmpfiles.rules = [
    "d /etc/nixos 0775 root nixconfig -"
  ];

  # Nix
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  system.stateVersion = "26.05";
}
