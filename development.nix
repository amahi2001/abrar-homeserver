{
  pkgs,
  ...
}:
let
  dev = pkgs.callPackage ./development/package.nix { };
in
{
  environment.systemPackages = [ dev ];
  # Personal host skills/config are synced from the private agent-config repo.
  # Keep this module independent of private GitHub credentials.

  # Keep stopped projects, named volumes, tagged images, and BuildKit caches warm.
  # system.control permits targeted activation without replacing NixOS's read-only
  # /etc/systemd/system tree or switching unrelated pending host configuration.
  environment.etc."systemd/system.control/docker-prune.service.d/development.conf".text = ''
    [Service]
    ExecStart=
    ExecStart=${pkgs.docker}/bin/docker image prune -f
  '';
}
