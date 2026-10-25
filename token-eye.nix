{ pkgs, lib, ... }:
let
  home = "/home/buildfleet";
  # Runtime strings keep the private application and credentials out of this flake.
  checkout = "${home}/projects/token-eye";
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
        Description=TokenEye-owned Headroom proxy on Buildfleet
        ConditionUser=buildfleet
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
        ConditionUser=buildfleet

        [Service]
        Type=oneshot
        WorkingDirectory=%h/projects/token-eye
        Environment="PATH=%h/.local/bin:/run/current-system/sw/bin:/usr/bin:/bin"
        ExecStart=${commands}/bin/token-eye-dogfood-server --require-headroom
        UMask=0077
        TimeoutStartSec=30min
      '')
      (pkgs.writeTextDir "lib/systemd/user/token-eye-dogfood.timer" ''
        [Unit]
        Description=Check TokenEye every other day
        ConditionUser=buildfleet

        [Timer]
        OnBootSec=15min
        OnUnitInactiveSec=48h
        AccuracySec=1min

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
      target = ".config/systemd/user/default.target.wants/token-eye-headroom.service";
      source = "lib/systemd/user/token-eye-headroom.service";
    }
    {
      target = ".config/systemd/user/timers.target.wants/token-eye-dogfood.timer";
      source = "lib/systemd/user/token-eye-dogfood.timer";
    }
  ];
in
{
  environment.systemPackages = [ commands ];
  systemd.packages = [ units ];
  users.users.buildfleet.linger = true;
  # Package base units coexist with development.nix's maintenance retry drop-in.
  systemd.user.units."token-eye-headroom.service".wantedBy = [ "default.target" ];
  systemd.user.units."token-eye-dogfood.timer".wantedBy = [ "timers.target" ];

  system.build.tokenEyeDeployment = deployment;
  # Maintain the existing user-owned entry points without requiring Home Manager.
  systemd.tmpfiles.rules = [
    "d ${home}/.local/bin 0755 buildfleet users -"
    "d ${home}/.config/systemd/user/default.target.wants 0755 buildfleet users -"
    "d ${home}/.config/systemd/user/timers.target.wants 0755 buildfleet users -"
  ]
  ++ map (link: "L+ ${home}/${link.target} - buildfleet users - ${deployment}/${link.source}") links;
}
