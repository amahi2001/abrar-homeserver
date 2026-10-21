{
  pkgs,
  lib,
  ponytail,
  caveman,
  ...
}:
let
  dev = pkgs.callPackage ./development/package.nix { inherit ponytail caveman; };
  names = [
    "ponytail"
    "ponytail-review"
    "ponytail-audit"
    "ponytail-debt"
    "ponytail-gain"
    "ponytail-help"
  ];
in
{
  environment.systemPackages = [ dev ];
  systemd.tmpfiles.rules = [
    "d /home/buildfleet/.agents 0700 buildfleet users -"
    "d /home/buildfleet/.agents/skills 0700 buildfleet users -"
    "d /home/buildfleet/.claude/skills 0700 buildfleet users -"
  ]
  ++ lib.concatMap (name: [
    "L+ /home/buildfleet/.agents/skills/${name} - - - - ${ponytail}/skills/${name}"
    "L+ /home/buildfleet/.claude/skills/${name} - - - - ${ponytail}/skills/${name}"
  ]) names;

  # Keep stopped projects, named volumes, tagged images, and BuildKit caches warm.
  # system.control permits targeted activation without replacing NixOS's read-only
  # /etc/systemd/system tree or switching unrelated pending host configuration.
  environment.etc."systemd/system.control/docker-prune.service.d/development.conf".text = ''
    [Service]
    ExecStart=
    ExecStart=${pkgs.docker}/bin/docker image prune -f
  '';
}
