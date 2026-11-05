{ pkgs, lib, ... }:
let
  home = "/home/amahi";
  # Runtime strings keep the private application and credentials out of this flake.
  checkout = "${home}/projects/token-eye";
  runtime = "${home}/.local/state/token-eye-nixos/runtime";
  updater = pkgs.writeShellApplication {
    name = "token-eye-update-server";
    runtimeInputs = [
      pkgs.python3
      pkgs.git
      pkgs.docker
      pkgs.util-linux
      pkgs.coreutils
    ];
    text = ''
      exec python3 ${./scripts/token_eye_update.py} "$@"
    '';
  };
  networkGuard = pkgs.writeShellApplication {
    name = "token-eye-network-guard";
    runtimeInputs = [
      pkgs.iptables
      pkgs.util-linux
    ];
    text = builtins.readFile ./scripts/token-eye-network-guard.sh;
  };
  commands = pkgs.symlinkJoin {
    name = "token-eye-commands";
    paths =
      map
        (
          command:
          pkgs.writeShellScriptBin command.name ''
            export PATH=${
              lib.makeBinPath [
                pkgs.docker
                pkgs.util-linux
                pkgs.coreutils
              ]
            }:$PATH
            exec ${pkgs.bash}/bin/bash ${checkout}/scripts/${command.script} "$@"
          ''
        )
        [
          {
            name = "token-eye";
            script = "token-eye-server";
          }
          {
            name = "token-eye-codex";
            script = "token-eye-codex-server";
          }
          {
            name = "token-eye-dogfood-server";
            script = "token-eye-dogfood-server";
          }
        ];
  };
  units = pkgs.symlinkJoin {
    name = "token-eye-user-units";
    paths = [
      (pkgs.writeTextDir "lib/systemd/user/token-eye-headroom.service" ''
        [Unit]
        Description=TokenEye-owned Claude and Codex Headroom proxies on Buildfleet
        ConditionUser=amahi
        StartLimitIntervalSec=0

        [Service]
        Type=simple
        WorkingDirectory=%h/projects/token-eye
        Environment="PATH=%h/.local/bin:/run/current-system/sw/bin:/usr/bin:/bin"
        ExecStart=${commands}/bin/token-eye-dogfood-server --headroom-proxy
        ExecStop=${commands}/bin/token-eye proxy-stop
        UMask=0077
        Restart=on-failure
        RestartSec=15
        TimeoutStopSec=20

        [Install]
        WantedBy=default.target
      '')
      (pkgs.writeTextDir "lib/systemd/user/token-eye-dogfood.service" ''
        [Unit]
        Description=Check TokenEye on Buildfleet and repair failures
        ConditionUser=amahi

        [Service]
        Type=oneshot
        WorkingDirectory=%h/projects/token-eye
        Environment="PATH=%h/.local/bin:/run/current-system/sw/bin:/usr/bin:/bin"
        ExecStart=${commands}/bin/token-eye-dogfood-server --require-headroom --require-claude-routing
        UMask=0077
        TimeoutStartSec=30min
      '')
      (pkgs.writeTextDir "lib/systemd/user/token-eye-dogfood.timer" ''
        [Unit]
        Description=Check TokenEye every other day
        ConditionUser=amahi

        [Timer]
        OnBootSec=15min
        OnUnitInactiveSec=48h
        AccuracySec=1min

        [Install]
        WantedBy=timers.target
      '')
      (pkgs.writeTextDir "lib/systemd/user/token-eye-update.service" ''
        [Unit]
        Description=Build and deploy TokenEye main when compatible and healthy
        ConditionUser=buildfleet

        [Service]
        Type=oneshot
        WorkingDirectory=%h/projects/token-eye
        Environment="PATH=%h/.local/bin:/run/current-system/sw/bin:/usr/bin:/bin"
        ExecStart=${updater}/bin/token-eye-update-server
        UMask=0077
        TimeoutStartSec=45min
        TimeoutStopSec=3min
      '')
      (pkgs.writeTextDir "lib/systemd/user/token-eye-update.timer" ''
        [Unit]
        Description=Keep the shared TokenEye CLI current with main
        ConditionUser=buildfleet

        [Timer]
        OnCalendar=hourly
        RandomizedDelaySec=5min
        Persistent=true

        [Install]
        WantedBy=timers.target
      '')
    ];
  };
  deployment = pkgs.symlinkJoin {
    name = "token-eye-deployment";
    paths = [
      commands
      units
      networkGuard
      updater
    ];
  };
  links = [
    {
      target = ".local/bin/token-eye";
      source = "bin/token-eye";
    }
    {
      target = ".local/bin/token-eye-codex";
      source = "bin/token-eye-codex";
    }
    {
      target = ".local/bin/token-eye-dogfood-server";
      source = "bin/token-eye-dogfood-server";
    }
    {
      target = ".local/bin/token-eye-update-server";
      source = "bin/token-eye-update-server";
    }
    {
      target = ".config/systemd/user/token-eye-headroom.service";
      source = "lib/systemd/user/token-eye-headroom.service";
    }
    {
      target = ".config/systemd/user/token-eye-dogfood.service";
      source = "lib/systemd/user/token-eye-dogfood.service";
    }
    {
      target = ".config/systemd/user/token-eye-dogfood.timer";
      source = "lib/systemd/user/token-eye-dogfood.timer";
    }
    {
      target = ".config/systemd/user/token-eye-update.service";
      source = "lib/systemd/user/token-eye-update.service";
    }
    {
      target = ".config/systemd/user/token-eye-update.timer";
      source = "lib/systemd/user/token-eye-update.timer";
    }
    {
      target = ".config/systemd/user/default.target.wants/token-eye-headroom.service";
      source = "lib/systemd/user/token-eye-headroom.service";
    }
    {
      target = ".config/systemd/user/timers.target.wants/token-eye-dogfood.timer";
      source = "lib/systemd/user/token-eye-dogfood.timer";
    }
    {
      target = ".config/systemd/user/timers.target.wants/token-eye-update.timer";
      source = "lib/systemd/user/token-eye-update.timer";
    }
  ];
in
{
  environment.systemPackages = [
    commands
    updater
  ];
  systemd.packages = [ units ];
  users.users.amahi.linger = true;
  systemd.services.token-eye-network-guard = {
    description = "Restrict authenticated Headroom gateways to local project bridges";
    wantedBy = [ "multi-user.target" ];
    after = [ "docker.service" ];
    requires = [ "docker.service" ];
    partOf = [ "docker.service" ];
    before = [ "user@1000.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${networkGuard}/bin/token-eye-network-guard";
      Restart = "on-failure";
      RestartSec = 5;
      UMask = "0077";
    };
  };
  networking.firewall.extraCommands = ''
    if ${pkgs.iptables}/bin/iptables -w -S DOCKER-USER >/dev/null 2>&1; then
      ${networkGuard}/bin/token-eye-network-guard
    fi
  '';
  # Package base units coexist with development.nix's maintenance retry drop-in.
  systemd.user.units."token-eye-headroom.service".wantedBy = [ "default.target" ];
  systemd.user.units."token-eye-dogfood.timer".wantedBy = [ "timers.target" ];
  systemd.user.units."token-eye-update.timer".wantedBy = [ "timers.target" ];

  system.build.tokenEyeDeployment = deployment;
  # Maintain the existing user-owned entry points without requiring Home Manager.
  systemd.tmpfiles.rules = [
    "d /opt/token-eye 0755 amahi users -"
    "d /opt/token-eye/bin 0755 amahi users -"
    "L /opt/token-eye/bin/token-eye - amahi users - /run/current-system/sw/bin/token-eye"
    "d ${home}/.local/state/token-eye-nixos 0700 amahi users -"
    "d ${runtime} 0700 amahi users -"
    "d ${home}/.local/bin 0755 amahi users -"
    "d ${home}/.config/systemd/user/default.target.wants 0755 amahi users -"
    "d ${home}/.config/systemd/user/timers.target.wants 0755 amahi users -"
  ]
  ++ map (link: "L+ ${home}/${link.target} - amahi users - ${deployment}/${link.source}") links;
}
