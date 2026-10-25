{
  description = "BuildFleet NixOS Configuration";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";
    hermes-agent.url = "github:NousResearch/hermes-agent/v0.22.5";
    cloakbrowser.url = "github:CloakHQ/CloakBrowser";
    # Tracks OpenAI Codex CLI releases independently of stable nixpkgs.
    codex-cli.url = "github:sadjow/codex-cli-nix";
    # Source for the Caveman skills used by managed Hermes.
    caveman = {
      url = "github:JuliusBrussee/caveman/v4.0.1";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      hermes-agent,
      cloakbrowser,
      codex-cli,
      caveman,
    }:
    let
      system = "x86_64-linux";
      buildfleet-server = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        specialArgs = {
          inherit cloakbrowser caveman;
          codexCli = codex-cli;
          hermesAgent = hermes-agent;
          deployedFlake = self.outPath;
        };
        modules = [
          ./configuration.nix
          hermes-agent.nixosModules.default
        ];
      };
    in
    {
      nixosConfigurations.buildfleet-server = buildfleet-server;
      packages.${system}.dev = buildfleet-server.pkgs.callPackage ./development/package.nix { };

      # Make `nix flake check` evaluate and build the exact system closure that
      # `nixos-rebuild` deploys, rather than merely checking flake syntax.
      checks.${system} = {
        nixos = buildfleet-server.config.system.build.toplevel;
        maintenance =
          buildfleet-server.pkgs.runCommand "maintenance-tests"
            {
              nativeBuildInputs = [
                (buildfleet-server.pkgs.python3.withPackages (ps: [ ps.websocket-client ]))
                buildfleet-server.pkgs.rsync
                buildfleet-server.pkgs.util-linux
              ];
            }
            ''
              export PYTHONDONTWRITEBYTECODE=1
              python -m unittest discover -s ${./scripts} -p 'test_*.py' -v
              touch "$out"
            '';
        github-actions =
          buildfleet-server.pkgs.runCommand "github-actions-maintenance-tests"
            { nativeBuildInputs = [ buildfleet-server.pkgs.python3 ]; }
            ''
              export PYTHONDONTWRITEBYTECODE=1
              python -m unittest discover -s ${./github-actions} -p test_maintain.py -v
              touch "$out"
            '';
        development =
          buildfleet-server.pkgs.runCommand "development-tests"
            {
              nativeBuildInputs = with buildfleet-server.pkgs; [
                bun
                git
                util-linux
                bash
              ];
            }
            ''
              export HOME="$TMPDIR/home"
              mkdir -p "$HOME"
              for script in ${./development}/*.sh; do bash -n "$script"; done
              bun test --timeout 30000 ${./development}/dev.test.ts
              touch "$out"
            '';
        bitwarden-sync =
          buildfleet-server.pkgs.runCommand "bitwarden-sync-tests"
            {
              nativeBuildInputs = [ buildfleet-server.pkgs.bun ];
              SYNC_TEST_SCRIPT = ./scripts/bitwarden-sync.ts;
            }
            ''
              bun test ${./scripts/bitwarden-sync.test.ts}
              touch "$out"
            '';
      };

      formatter.${system} = nixpkgs.legacyPackages.${system}.nixfmt;
    };
}
