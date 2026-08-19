{
  description = "BuildFleet NixOS Configuration";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";
    hermes-agent.url = "github:NousResearch/hermes-agent";
    cloakbrowser.url = "github:CloakHQ/CloakBrowser";
    # Tracks OpenAI Codex CLI releases independently of stable nixpkgs.
    codex-cli.url = "github:sadjow/codex-cli-nix";
    # Source for the Caveman skills exposed to Codex and managed Hermes.
    caveman = {
      url = "github:JuliusBrussee/caveman/v1.9.1";
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
        };
        modules = [
          ./configuration.nix
          hermes-agent.nixosModules.default
        ];
      };
    in
    {
      nixosConfigurations.buildfleet-server = buildfleet-server;

      # Make `nix flake check` evaluate and build the exact system closure that
      # `nixos-rebuild` deploys, rather than merely checking flake syntax.
      checks.${system}.nixos = buildfleet-server.config.system.build.toplevel;

      formatter.${system} = nixpkgs.legacyPackages.${system}.nixfmt;
    };
}
