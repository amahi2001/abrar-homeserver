{
  # Preserve the existing filesystem and old installation; GRUB uses this disk.
  fileSystems."/srv/storage" = {
    device = "/dev/disk/by-uuid/008b7d21-572d-49e6-9c9c-b300dcfa76a5";
    fsType = "ext4";
    options = [
      "nofail"
      "nodev"
      "nosuid"
      "noexec"
      "x-systemd.device-timeout=10s"
      "x-systemd.mount-timeout=30s"
    ];
  };

  # Remove the persistent bootstrap unit once activation installs the new fstab.
  system.activationScripts.storage-mount-bootstrap = {
    deps = [ "etc" ];
    text = ''
      rm -f /etc/systemd/system.control/srv-storage.mount \
        /etc/systemd/system.control/local-fs.target.wants/srv-storage.mount
    '';
  };
}
