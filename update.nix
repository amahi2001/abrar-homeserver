# Build maintenance candidates from the last deployed flake, never an editor's
# working tree. All deployments run tests, back up service state, and check health.
{
  pkgs,
  lib,
  deployedFlake,
  ...
}:
let
  stateHook = pkgs.writeShellScript "service-state" ''
    exec ${pkgs.python3}/bin/python -P ${./scripts/service-state.py} "$@"
  '';
  updater = pkgs.writeShellApplication {
    name = "system-update";
    runtimeInputs = with pkgs; [
      nix
      nixos-rebuild
      git
      gh
      systemd
      curl
      dnsutils
      docker
      util-linux
      coreutils
      postgresql
      rsync
    ];
    text = ''
      exec ${pkgs.python3}/bin/python -P ${./scripts/system-update.py} "$@" --backup-hook ${stateHook}
    '';
  };
  updateJob = task: {
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    onFailure = [ "maintenance-alert@%n.service" ];
    restartIfChanged = false;
    stopIfChanged = false;
    environment = {
      NIX_CONFIG = "max-jobs = 2\ncores = 2";
      FLEET_MAINTENANCE_MODULE = "${./scripts/fleet-state.py}";
      RUNNER_MAINTENANCE_MODULE = "${./github-actions/maintain.py}";
      RUNNER_REPOSITORIES = builtins.toJSON (import ./github-actions/repositories.nix);
    };
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${updater}/bin/system-update ${task}";
      LoadCredential = "github-token:/var/lib/secrets/github-actions-token";
      TimeoutStartSec = "8h";
      TimeoutStopSec = "30min";
      UMask = "0077";
      Nice = 10;
      # Root can use Nix's local store instead of the daemon, so bound this
      # transaction as well as nix-daemon's builders.
      IOWeight = 10;
      IOSchedulingClass = "idle";
      IOReadIOPSMax = "/nix/store 500";
      IOWriteIOPSMax = "/nix/store 500";
    };
  };
  calendar = value: {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = value;
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };
  codexRefreshPython = pkgs.python3.withPackages (ps: [ ps.websocket-client ]);
in
{
  environment.etc."nixos-deployed".source = deployedFlake;
  environment.systemPackages = [ updater ];
  systemd.tmpfiles.rules = [ "d /var/lib/nixos-updates 0700 root root -" ];

  # Cold builds previously starved live services on this host's shared SSD.
  nix.settings.max-substitution-jobs = 4;
  systemd.services.nix-daemon.serviceConfig = {
    IOWeight = 10;
    IOSchedulingClass = lib.mkForce "idle";
    IOReadIOPSMax = "/nix/store 500";
    IOWriteIOPSMax = "/nix/store 500";
  };

  systemd.services.nixos-flake-update = (updateJob "all") // {
    description = "Build, test, deploy, and verify the latest stable system inputs";
  };
  systemd.timers.nixos-flake-update = calendar "Sun *-*-* 03:00:00 America/New_York";
  systemd.services.codex-cli-update = (updateJob "codex") // {
    description = "Build, test, deploy, and verify the latest Codex CLI";
  };
  systemd.timers.codex-cli-update = calendar "*-*-* 04:30:00 America/New_York";
  systemd.services.hermes-flake-update = (updateJob "hermes") // {
    description = "Build, test, deploy, and verify the latest stable Hermes release";
  };
  systemd.timers.hermes-flake-update = calendar "Sun *-*-* 05:00:00 America/New_York";
  systemd.services.adguardhome-update.onFailure = [ "maintenance-alert@%n.service" ];
  systemd.services.vpn-container-update.onFailure = [ "maintenance-alert@%n.service" ];
  systemd.services.github-actions-maintenance.onFailure = [ "maintenance-alert@%n.service" ];
  systemd.services.dev-runtime-maintenance.onFailure = [ "maintenance-alert@%n.service" ];
  systemd.services.adguardhome-update.serviceConfig.TimeoutStopSec = lib.mkForce "20min";
  systemd.services.vpn-container-update.serviceConfig.TimeoutStopSec = lib.mkForce "20min";

  # A separate autoUpgrade switch could otherwise deploy unvalidated dirty work.
  system.autoUpgrade.enable = lib.mkForce false;

  systemd.services."maintenance-alert@" = {
    description = "Record a failed maintenance job";
    serviceConfig = {
      Type = "oneshot";
      UMask = "0077";
    };
    script = ''
      mkdir -p /var/lib/nixos-updates/failures
      date --iso-8601=seconds > /var/lib/nixos-updates/failures/"%i"
      echo "Maintenance job %i failed. Inspect: journalctl -u %i" >&2
    '';
  };

  systemd.services.critical-services-recovery = {
    description = "Recover stopped critical services and check application health";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    onFailure = [ "maintenance-alert@%n.service" ];
    restartIfChanged = false;
    stopIfChanged = false;
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${updater}/bin/system-update --recover";
      TimeoutStartSec = "5min";
      UMask = "0077";
      Nice = 10;
    };
  };
  systemd.timers.critical-services-recovery = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitInactiveSec = "1min";
      AccuracySec = "1s";
    };
  };

  systemd.services.codex-app-server-refresh = {
    description = "Refresh outdated remote Codex app servers when idle";
    serviceConfig = {
      Type = "oneshot";
      User = "buildfleet";
      Group = "users";
      ExecStart = "${codexRefreshPython}/bin/python -P ${./scripts/codex-app-server-refresh.py} --socket /home/buildfleet/.codex/app-server-control/app-server-control.sock";
      TimeoutStartSec = "2min";
      UMask = "0077";
    };
  };

  systemd.services.codex-remote-control = {
    description = "Keep Codex Remote Control enabled and running";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    restartIfChanged = false;
    stopIfChanged = false;
    environment.HOME = "/home/buildfleet";
    serviceConfig = {
      Type = "oneshot";
      User = "buildfleet";
      Group = "users";
      WorkingDirectory = "/home/buildfleet";
      ExecStart = "/run/current-system/sw/bin/bash -lc 'exec /run/current-system/sw/bin/codex remote-control start --json'";
      # Codex detaches its daemon; keep it alive after this command exits.
      KillMode = "process";
      TimeoutStartSec = "2min";
      UMask = "0077";
    };
  };
  systemd.timers.codex-remote-control = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "30s";
      OnUnitInactiveSec = "30s";
      AccuracySec = "1s";
    };
  };

  systemd.timers.codex-app-server-refresh = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "5min";
    };
  };

  nix.gc = {
    automatic = true;
    dates = "Sat *-*-* 02:00:00 America/New_York";
    options = "--delete-older-than 7d";
  };
  nix.optimise.automatic = true;
}
