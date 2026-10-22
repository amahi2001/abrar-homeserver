# Ubuntu jobs stay inside a container; registration credentials stay on the host.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  runner = "github-actions-token-eye";
  repository = "amahi2001/token-eye";
  image = "buildfleet-github-runner:${
    builtins.substring 0 16 (
      builtins.hashString "sha256" (builtins.readFile ./github-actions/Dockerfile)
    )
  }";
  unit = "${config.systemd.units."${runner}.service".unit}/${runner}.service";
in
{
  virtualisation.oci-containers.containers.${runner} = {
    inherit image;
    serviceName = runner;
    pull = "never";
    environment = {
      RUNNER_URL = "https://github.com/${repository}";
      RUNNER_NAME = "buildfleet-server-token-eye";
      RUNNER_LABELS = "buildfleet-server,ubuntu-24.04";
    };
    environmentFiles = [ "/run/${runner}/runner.env" ];
    volumes = [
      "${./github-actions/run.sh}:/opt/github-actions/run.sh:ro"
      "/var/lib/github-actions/token-eye/work:/home/runner/_work"
    ];
    cmd = [
      "bash"
      "/opt/github-actions/run.sh"
    ];
    extraOptions = [
      "--init"
      "--cpus=4"
      "--memory=8g"
      "--memory-swap=10g"
      "--pids-limit=2048"
      "--stop-signal=SIGINT"
    ];
  };

  systemd.services.${runner} = {
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
      token="$(${pkgs.gh}/bin/gh api --method POST repos/${repository}/actions/runners/registration-token --jq .token)"
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
    "d /var/lib/github-actions 0700 root root -"
    "d /var/lib/github-actions/token-eye 0700 root root -"
    "d /var/lib/github-actions/token-eye/work 0750 1001 123 -"
  ];

  # Match development.nix: permit targeted activation while other host edits wait.
  environment.etc."systemd/system.control/${runner}.service".source = unit;
  environment.etc."systemd/system.control/multi-user.target.wants/${runner}.service".source = unit;
}
