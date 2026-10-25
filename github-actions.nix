# Ubuntu jobs stay inside a container; registration credentials stay on the host.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  repositories = import ./github-actions/repositories.nix;
  image = "buildfleet-github-runner:${
    builtins.substring 0 16 (
      builtins.hashString "sha256" (builtins.readFile ./github-actions/Dockerfile)
    )
  }";
in
{
  # Personal GitHub accounts require one registration per repository.
  imports = map (
    repository:
    let
      runner = "github-actions-${repository}";
      url = "https://github.com/amahi2001/${repository}";
      unit = "${config.systemd.units."${runner}.service".unit}/${runner}.service";
      runnerImage = "${image}-${repository}";
    in
    {
      virtualisation.oci-containers.containers.${runner} = {
        image = runnerImage;
        serviceName = runner;
        pull = "never";
        environment = {
          RUNNER_URL = url;
          RUNNER_NAME = "buildfleet-server-${repository}";
          RUNNER_LABELS = "buildfleet-server,ubuntu-24.04";
          FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 = "true";
          ACTIONS_RUNNER_FORCED_INTERNAL_NODE_VERSION = "node24";
          ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION = "false";
        };
        environmentFiles = [ "/run/${runner}/runner.env" ];
        volumes = [
          "${./github-actions/run.sh}:/opt/github-actions/run.sh:ro"
          "/var/lib/github-actions/${repository}/work:/home/runner/_work"
        ];
        cmd = [
          "bash"
          "/opt/github-actions/run.sh"
        ];
        extraOptions = [
          "--init"
          "--cgroup-parent=githubactions.slice"
          "--cpus=4"
          "--memory=8g"
          "--memory-swap=10g"
          "--pids-limit=2048"
          "--stop-signal=SIGINT"
        ];
      };

      systemd.services.${runner} = {
        # Activation can otherwise start newly declared runners before the
        # Docker transaction is ready to accept jobs again.
        unitConfig.ConditionPathExists = "!/run/nixos-fleet-maintenance/paused";
        description = "GitHub Actions runner for ${repository}";
        after = [ "githubactions.slice" ];
        requires = [ "githubactions.slice" ];
        # Avoid stopping a running CI job during unrelated system rebuilds.
        restartIfChanged = false;
        stopIfChanged = false;
        preStart = lib.mkAfter ''
          set -euo pipefail
          # Serialize first builds across repository services. Maintenance tags
          # each repository independently, preserving its known-good image.
          (
            ${pkgs.util-linux}/bin/flock 9
            if ! docker image inspect ${lib.escapeShellArg runnerImage} >/dev/null 2>&1; then
              if ! docker image inspect ${lib.escapeShellArg image} >/dev/null 2>&1; then
                docker build --tag ${lib.escapeShellArg image} ${./github-actions}
              fi
              docker image tag ${lib.escapeShellArg image} ${lib.escapeShellArg runnerImage}
            fi
          ) 9>/run/lock/github-actions-image.lock
          GH_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/github-token")"
          export GH_TOKEN
          ${pkgs.gh}/bin/gh api repos/amahi2001/${repository} \
            --jq '(.private == true) and (.owner.login == "amahi2001")' \
            | ${pkgs.gnugrep}/bin/grep -qx true
          token="$(${pkgs.gh}/bin/gh api --method POST repos/amahi2001/${repository}/actions/runners/registration-token --jq .token)"
          umask 077
          printf 'RUNNER_TOKEN=%s\n' "$token" > /run/${runner}/runner.env
        '';
        serviceConfig = {
          LoadCredential = "github-token:/var/lib/secrets/github-actions-token";
          RuntimeDirectory = runner;
          RuntimeDirectoryMode = "0700";
          RestartSec = "30s";
        };
      };

      systemd.tmpfiles.rules = [
        "d /var/lib/github-actions/${repository} 0700 root root -"
        "d /var/lib/github-actions/${repository}/work 0750 1001 123 -"
      ];

      # Match development.nix: activate only these units while other edits wait.
      environment.etc."systemd/system.control/${runner}.service".source = unit;
      environment.etc."systemd/system.control/multi-user.target.wants/${runner}.service".source = unit;
    }
  ) repositories;

  systemd.tmpfiles.rules = [
    "d /var/lib/github-actions 0700 root root -"
  ];

  systemd.services.github-actions-maintenance = {
    description = "Refresh GitHub runner images and replace idle listeners";
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
      pkgs.gh
      pkgs.systemd
    ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.python3}/bin/python -P ${./github-actions/maintain.py} --context ${./github-actions} --image ${image} ${lib.escapeShellArgs repositories}";
      LoadCredential = "github-token:/var/lib/secrets/github-actions-token";
      StateDirectory = "github-actions/maintenance";
      StateDirectoryMode = "0700";
      TimeoutStartSec = "2h";
      # Python unwinds the dispatch-freeze guard when maintenance is stopped.
      KillSignal = "SIGINT";
      UMask = "0077";
    };
  };
  systemd.timers.github-actions-maintenance = {
    description = "Retry deferred runner refreshes and rebuild packages weekly";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "15min";
      OnUnitInactiveSec = "1h";
      RandomizedDelaySec = "5min";
    };
  };
  environment.etc."systemd/system.control/github-actions-maintenance.service".source = "${
    config.systemd.units."github-actions-maintenance.service".unit
  }/github-actions-maintenance.service";
  environment.etc."systemd/system.control/github-actions-maintenance.timer".source = "${
    config.systemd.units."github-actions-maintenance.timer".unit
  }/github-actions-maintenance.timer";
  environment.etc."systemd/system.control/timers.target.wants/github-actions-maintenance.timer".source =
    "${config.systemd.units."github-actions-maintenance.timer".unit}/github-actions-maintenance.timer";

  # Bound the whole CI fleet, including containers started by the Docker daemon.
  systemd.slices.githubactions = {
    description = "Shared resource budget for personal GitHub Actions jobs";
    sliceConfig = {
      CPUQuota = "400%";
      MemoryMax = "8G";
      MemorySwapMax = "2G";
      TasksMax = 4096;
    };
  };
  environment.etc."systemd/system.control/githubactions.slice".source = "${
    config.systemd.units."githubactions.slice".unit
  }/githubactions.slice";
}
