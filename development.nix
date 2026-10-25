{
  pkgs,
  ...
}:
let
  dev = pkgs.callPackage ./development/package.nix { };
in
{
  environment.systemPackages = [ dev ];
  # The unprivileged dev updater shares the host deployment mutex with root's
  # system/container updaters. Docker group members already administer containers.
  systemd.tmpfiles.rules = [ "f /run/lock/host-deployment.lock 0660 root docker -" ];
  # This project-owned proxy uses dev shell and retries while its box is being
  # rebuilt. Maintenance must not exhaust its retry budget before the box returns.
  systemd.user.units."token-eye-headroom.service" = {
    overrideStrategy = "asDropin";
    text = ''
      [Unit]
      StartLimitIntervalSec=0
    '';
  };
  # Personal host skills/config are synced from the private agent-config repo.
  # Keep this module independent of private GitHub credentials.

  systemd.services.dev-runtime-maintenance = {
    description = "Refresh development runtimes and idle running boxes";
    after = [
      "network-online.target"
      "docker.service"
    ];
    wants = [ "network-online.target" ];
    requires = [ "docker.service" ];
    restartIfChanged = false;
    stopIfChanged = false;
    path = [
      pkgs.docker
      pkgs.util-linux
    ];
    environment.HOME = "/home/buildfleet";
    serviceConfig = {
      Type = "oneshot";
      User = "buildfleet";
      Group = "users";
      ExecStart = "${pkgs.python3}/bin/python -P ${./scripts/dev_runtime_maintenance.py} --dev ${dev}/bin/dev";
      StateDirectory = "dev-runtime-maintenance";
      StateDirectoryMode = "0700";
      TimeoutStartSec = "2h";
      KillSignal = "SIGINT";
      UMask = "0077";
    };
  };
  systemd.timers.dev-runtime-maintenance = {
    description = "Weekly development package refresh and idle retry";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun *-*-* 07:00:00 America/New_York";
      OnBootSec = "15min";
      OnUnitInactiveSec = "1h";
      Persistent = true;
      RandomizedDelaySec = "5min";
    };
  };

  # Keep stopped projects, named volumes, tagged images, and BuildKit caches warm.
  # system.control permits targeted activation without replacing NixOS's read-only
  # /etc/systemd/system tree or switching unrelated pending host configuration.
  environment.etc."systemd/system.control/docker-prune.service.d/development.conf".text = ''
    [Service]
    ExecStart=
    ExecStart=${pkgs.docker}/bin/docker image prune -f
  '';
}
