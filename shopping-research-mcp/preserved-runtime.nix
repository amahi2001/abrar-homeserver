{ ... }: {
  # Preserve the live fstab entry as well as the loaded bootstrap unit. Do not
  # apply the unrelated canonical storage migration or its cleanup activation.
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
  # Preserve the already active persistent bootstrap mount verbatim. The canonical storage migration is unrelated to shopping.
  systemd.units."srv-storage.mount" = {
    text = builtins.readFile ./preserved-srv-storage.mount;
    wantedBy = [ "local-fs.target" ];
  };
}
