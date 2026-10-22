{ pkgs }:
let
  assets = pkgs.runCommand "dev-container-assets" { } ''
    mkdir -p $out
    cp -r ${./.}/. $out/
    chmod -R u+w $out
  '';
in
pkgs.writeShellApplication {
  name = "dev";
  runtimeInputs = with pkgs; [
    bun
    docker
    docker-compose
    git
    gh
    util-linux
    coreutils
    bash
    gnused
    curl
    gnutar
    gzip
    xz
  ];
  text = ''
    export DEV_ASSETS=${assets}
    exec bun ${./dev.ts} "$@"
  '';
}
