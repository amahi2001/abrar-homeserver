{
  pkgs,
  ponytail,
  caveman,
}:
let
  assets = pkgs.runCommand "dev-container-assets" { } ''
    mkdir -p $out/skills
    cp -r ${./.}/. $out/
    chmod -R u+w $out
    for name in ponytail ponytail-review ponytail-audit ponytail-debt ponytail-gain ponytail-help; do
      cp -r ${ponytail}/skills/$name $out/skills/
    done
    for name in caveman caveman-commit caveman-review; do
      cp -r ${caveman}/skills/$name $out/skills/
    done
    cp ${ponytail}/LICENSE $out/skills/PONYTAIL-LICENSE
    cp ${caveman}/LICENSE $out/skills/CAVEMAN-LICENSE
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
  ];
  text = ''
    export DEV_ASSETS=${assets}
    exec bun ${./dev.ts} "$@"
  '';
}
