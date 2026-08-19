# Desktop environment, GPU, and input
{
  config,
  pkgs,
  lib,
  ...
}:

{
  # Boot as a server: provide a local TTY for recovery, but do not start the
  # graphical login, XFCE, or its background processes until explicitly asked.
  # To restore the desktop locally: sudo systemctl isolate graphical.target
  systemd.defaultUnit = lib.mkForce "multi-user.target";

  # XFCE desktop
  services.xserver = {
    enable = true;
    desktopManager = {
      xterm.enable = false;
      xfce.enable = true;
    };
  };
  services.displayManager.defaultSession = "xfce";

  # Keyboard layout
  services.xserver.xkb = {
    layout = "us";
    variant = "";
  };

  # Nvidia GPU (RTX 3060)
  services.xserver.videoDrivers = [ "nvidia" ];
  hardware.nvidia = {
    modesetting.enable = true;
    open = false;
    nvidiaSettings = true;
    powerManagement = {
      enable = true;
      finegrained = false;
    };
  };

  # Bluetooth
  hardware.bluetooth.enable = true;
  hardware.bluetooth.powerOnBoot = true;
  # The BlueZ service itself is normally pulled in by graphical.target. Keep
  # it available for paired input devices when booting directly to a TTY.
  systemd.targets.bluetooth.wantedBy = [ "multi-user.target" ];
  # BlueZ keeps paired input devices working; the graphical Blueman manager is
  # unnecessary on a headless-by-default server.
  services.blueman.enable = false;

  # Display power management
  services.xserver.displayManager.sessionCommands = ''
    xset s 300 300
    xset dpms 300 600 900
  '';
}
