{
  description = "BuildFleet NixOS Configuration";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";
    hermes-agent.url = "github:NousResearch/hermes-agent";
    cloakbrowser.url = "github:CloakHQ/CloakBrowser";
  };

  outputs = { self, nixpkgs, hermes-agent, cloakbrowser }: {
    nixosConfigurations.buildfleet-server = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      specialArgs = { inherit cloakbrowser; };
      modules = [
        ./configuration.nix
        hermes-agent.nixosModules.default
      ];
    };
  };
}
