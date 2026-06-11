{ config, pkgs, lib, cloakbrowser, ... }:

let
  deno-latest = pkgs.stdenv.mkDerivation rec {
    pname = "deno";
    version = "2.8.2";

    src = pkgs.fetchurl {
      url = "https://github.com/denoland/deno/releases/download/v${version}/deno-x86_64-unknown-linux-gnu.zip";
      hash = "sha256-GE2npSZ6tkm8CIIbO8POaAXY5phfuCcHy41en9ZTU2I=";
    };

    nativeBuildInputs = [ pkgs.unzip ];
    buildInputs = [ pkgs.stdenv.cc.cc.lib ];

    sourceRoot = ".";

    installPhase = ''
      mkdir -p $out/bin
      cp deno $out/bin/
      chmod +x $out/bin/deno
    '';
  };
in
{
  imports = [
    ./hardware-configuration.nix
    ./desktop.nix
    ./web.nix
    ./containers.nix
    ./hermes.nix
    ./tailscale.nix
    ./update.nix
  ];

  # Bootloader
  boot.loader.grub.enable = true;
  boot.loader.grub.device = "/dev/sda";
  boot.loader.grub.useOSProber = true;

  # Hostname
  networking.hostName = "buildfleet-server";

  # Networking
  networking.networkmanager.enable = true;

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
    extraGroups = [ "networkmanager" "wheel" "docker" "hermes" "nixconfig" ];
    packages = with pkgs; [];
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
        { command = "ALL"; options = [ "NOPASSWD" "SETENV" ]; }
      ];
    }
  ];

  # System packages
  nixpkgs.config.allowUnfree = true;
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
    deno-latest
    (pkgs.writeShellScriptBin "dx" ''
      exec ${deno-latest}/bin/deno x "$@"
    '')
    cloakbrowser.packages.x86_64-linux.default
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

  # Firewall
  networking.firewall.allowedTCPPorts = [ 22 80 443 53 8080 ];
  networking.firewall.allowedUDPPorts = [ 53 ];
  networking.firewall.enable = true;
  networking.firewall.logRefusedConnections = true;

  # Shared nixos config directory permissions
  systemd.tmpfiles.rules = [
    "Z /etc/nixos 0775 root nixconfig -"
  ];

  # Nix
  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  system.stateVersion = "26.05";
}
