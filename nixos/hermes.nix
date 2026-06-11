{ config, pkgs, lib, cloakbrowser, ... }:

{
  # Add hermes user to nixconfig group (hermes user is created by hermes-agent module)
  users.users.hermes.extraGroups = [ "wheel" "nixconfig" ];

  # Hermes Agent - AI assistant with Kimi K2.6 via OpenCode Go
  services.hermes-agent = {
    enable = true;

    # Extra Python deps for platform adapters (Telegram, Discord, etc.)
    extraPythonPackages = with pkgs.python312Packages; [ python-telegram-bot ];

    # Use OpenCode Go as the provider
    settings = {
      model = {
        base_url = "https://opencode.ai/zen/go/v1";
        default = "glm-5.1";
        provider = "opencode-go";
      };
      toolsets = [ "all" ];
      max_turns = 100;
      terminal = {
        backend = "local";
        cwd = "/var/lib/hermes/workspace";
        timeout = 180;
      };
      compression = {
        enabled = true;
        threshold = 0.85;
        summary_model = "deepseek-v4-pro";
      };
      auxiliary = {
        compression = {
          provider = "opencode-go";
          model = "deepseek-v4-pro";
          timeout = 120;
        };
      };
      memory = {
        memory_enabled = true;
        user_profile_enabled = true;
      };
      display = {
        compact = false;
        personality = "default";
      };
      agent = {
        max_turns = 60;
        restart_drain_timeout = 60;
        verbose = false;
      };
      telegram = {
        reactions = false;
        allowed_chats = "";
      };
    };

    # Secrets file containing OPENROUTER_API_KEY
    environmentFiles = [ "/var/lib/hermes/env" ];

    # Allow all users to access Hermes via messaging platforms
    environment = {
      GATEWAY_ALLOW_ALL_USERS = "true";
    };

    # Add hermes CLI to system PATH and set HERMES_HOME
    addToSystemPackages = true;
  };

  # Add python-telegram-bot to PYTHONPATH for Telegram adapter
  systemd.services.hermes-agent.environment.PYTHONPATH = "${pkgs.python312Packages.python-telegram-bot}/lib/python3.12/site-packages";

  # Allow Hermes agent to use sudo (wheel group)
  systemd.services.hermes-agent.serviceConfig.SupplementaryGroups = [ "wheel" ];
  systemd.services.hermes-agent.serviceConfig.NoNewPrivileges = lib.mkForce false;

  # Self-Interruption Prevention
  systemd.services.hermes-agent.restartIfChanged = false;
  systemd.services.hermes-agent.stopIfChanged = false;
  systemd.services.hermes-dashboard.restartIfChanged = false;
  systemd.services.hermes-dashboard.stopIfChanged = false;

  # Separate dashboard service for Hermes Desktop app
  systemd.services.hermes-dashboard = {
    description = "Hermes Agent Dashboard for Desktop app";
    after = [ "network.target" "hermes-agent.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "simple";
      User = "hermes";
      Group = "hermes";
      WorkingDirectory = "/var/lib/hermes";
      Environment = [
        "HOME=/var/lib/hermes"
        "HERMES_HOME=/var/lib/hermes/.hermes"
        "API_SERVER_HOST=0.0.0.0"
        "API_SERVER_PORT=9119"
        "API_SERVER_KEY="  # No auth required - Tailscale secures it
      ];
      EnvironmentFile = "/var/lib/hermes/env";
      ExecStart = "/var/lib/hermes/.nix-profile/bin/hermes dashboard --host 0.0.0.0 --port 9119 --insecure --no-open --skip-build";
      Restart = "always";
      RestartSec = 5;
    };
  };
  systemd.services.hermes-dashboard.serviceConfig.SupplementaryGroups = [ "wheel" ];

  # Create Hermes data directories & Symlinks
  systemd.tmpfiles.rules = [
    # Make sudo available on the gateway subprocess PATH (NixOS puts it at /run/wrappers/bin)
    "L+ /usr/bin/sudo - - - - /run/wrappers/bin/sudo"
    "d /var/lib/hermes           0755 hermes hermes -"  # Allow hermes group access
    "d /var/lib/hermes/.hermes   0755 hermes hermes -"  # Config dir accessible to group
    "z /var/lib/hermes/.hermes/.env 0644 hermes hermes -"  # API key readable by group
    "z /var/lib/hermes/.hermes/config.yaml 0644 hermes hermes -"  # Config readable by group

    # Symlink Hermes Agent skills to Devin skills directory for persistence
    "d /etc/nixos/.devin         0775 root nixconfig -"
    "d /etc/nixos/.devin/skills  0775 root nixconfig -"
    "L+ /etc/nixos/.devin/skills/nixos-environment - - - - /var/lib/hermes/.hermes/skills/devops/nixos-environment"
    "L+ /etc/nixos/.devin/skills/nixos-hermes-integration - - - - /var/lib/hermes/.hermes/skills/devops/nixos-hermes-integration"
    "L+ /etc/nixos/.devin/skills/nixos-hermes-setup - - - - /var/lib/hermes/.hermes/skills/devops/nixos-hermes-setup"
    "L+ /etc/nixos/.devin/skills/cloakbrowser-nixos-setup - - - - /var/lib/hermes/.hermes/skills/devops/cloakbrowser-nixos-setup"
  ];
}
