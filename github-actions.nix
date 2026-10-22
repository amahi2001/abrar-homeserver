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
    in
    {
      virtualisation.oci-containers.containers.${runner} = {
        inherit image;
        serviceName = runner;
        pull = "never";
        environment = {
          RUNNER_URL = url;
          RUNNER_NAME = "buildfleet-server-${repository}";
          RUNNER_LABELS = "buildfleet-server,ubuntu-24.04";
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
        description = "GitHub Actions runner for ${repository}";
        after = [ "githubactions.slice" ];
        requires = [ "githubactions.slice" ];
        # Avoid stopping a running CI job during unrelated system rebuilds.
        restartIfChanged = false;
        stopIfChanged = false;
        preStart = lib.mkAfter ''
          set -euo pipefail
          if ! docker image inspect ${lib.escapeShellArg image} >/dev/null 2>&1; then
            docker build --tag ${lib.escapeShellArg image} ${./github-actions}
          fi
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
