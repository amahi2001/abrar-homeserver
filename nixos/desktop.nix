# Desktop environment, GPU, and input
{ config, pkgs, lib, ... }:

{
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
  services.blueman.enable = true;

  # Display power management
  services.xserver.displayManager.sessionCommands = ''
    xset s 300 300
    xset dpms 300 600 900
  '';
}
