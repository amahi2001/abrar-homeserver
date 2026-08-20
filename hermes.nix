{
  config,
  pkgs,
  lib,
  cloakbrowser,
  caveman,
  ...
}:

let
  webSearchFallback = pkgs.writeTextDir "lib/python3.12/site-packages/sitecustomize.py" (
    builtins.readFile ./hermes-web-search-fallback.py
  );

  # Derive the Telegram user allowlist at runtime from the private home-chat
  # identifier when no explicit allowlist is present. This keeps the identifier
  # out of the Nix store while replacing the unsafe global allow-all bypass.
  hermesGateway = pkgs.writeShellScript "hermes-gateway" ''
    set -a
    . /var/lib/hermes/env
    set +a

    if [ -z "''${TELEGRAM_ALLOWED_USERS:-}" ] && [ -n "''${TELEGRAM_HOME_CHANNEL:-}" ]; then
      export TELEGRAM_ALLOWED_USERS="$TELEGRAM_HOME_CHANNEL"
    fi
    unset GATEWAY_ALLOW_ALL_USERS

    exec ${config.services.hermes-agent.package}/bin/hermes gateway
  '';
in
{
  # Add hermes user to nixconfig group (hermes user is created by hermes-agent module)
  users.users.hermes.extraGroups = [
    "wheel"
    "nixconfig"
  ];

  # Hermes Agent - AI assistant with GPT-5.6 Luna via OpenAI Codex
  services.hermes-agent = {
    enable = true;

    # Extra Python deps and dependency groups for platform adapters and providers
    extraDependencyGroups = [
      "anthropic"
      "messaging"
    ];

    # Use OpenAI Codex as the provider
    settings = {
      model = {
        base_url = "https://chatgpt.com/backend-api/codex";
        default = "gpt-5.6-luna";
        provider = "openai-codex";
      };
      toolsets = [ "all" ];
      # Use Exa first for search, then retry transient/provider failures with
      # Tavily and Brave. Native Hermes has no fallback-chain setting, so the
      # small sitecustomize policy hook below implements this declared order.
      web = {
        backend = "tavily";
        search_backend = "exa";
        search_fallback_backends = [
          "tavily"
          "brave-free"
        ];
        extract_backend = "tavily";
      };
      # Keep RTK and the Photon iMessage adapter enabled across NixOS rebuilds.
      plugins.enabled = [
        "rtk-rewrite"
        "photon-platform"
      ];
      max_turns = 20;
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
        title_generation = {
          provider = "opencode-go";
          model = "deepseek-v4-pro";
          timeout = 60;
        };
      };
      memory = {
        memory_enabled = true;
        user_profile_enabled = true;
      };
      display = {
        compact = false;
        personality = "default";
        # Suppress user-facing footers for failed file mutations. Tool errors
        # remain in the agent's context and logs for normal recovery.
        file_mutation_verifier = false;
      };
      agent = {
        # Bound tool loops so a completed task cannot leave Telegram showing
        # a stale Working status indefinitely. Hermes makes a final summary
        # request when this limit is reached.
        max_turns = 20;
        restart_drain_timeout = 60;
        verbose = false;
      };
      telegram = {
        reactions = false;
        allowed_chats = "";
        # Telegram Bot API native rendering for tables, task lists, and <details>.
        rich_messages = true;
      };
    };

    # MCP Servers
    mcpServers = {
      warden-mcp = {
        # Hermes writes environmentFiles into its generated dotenv, but MCP
        # subprocesses do not inherit those values. Load the protected source
        # file explicitly so Warden receives its required BW_* credentials.
        command = "${pkgs.writeShellScript "warden-mcp" ''
          set -a
          . /var/lib/hermes/env
          set +a
          export BW_BIN=${pkgs.bitwarden-cli}/bin/bw
          exec ${pkgs.nodejs}/bin/node \
            /var/lib/hermes/.hermes/warden-mcp/node_modules/@icoretech/warden-mcp/bin/warden-mcp.js \
            "$@"
        ''}";
        args = [ "--stdio" ];
        timeout = 120;
        connect_timeout = 30;
        enabled = true;
      };
    };

    # Secrets file containing OPENROUTER_API_KEY
    environmentFiles = [ "/var/lib/hermes/env" ];

    environment = {
      # TELEGRAM_HOME_CHANNEL stays in /var/lib/hermes/env so the personal chat
      # identifier is not published with this public repository.

      # Abort a Codex streaming request that connects but never produces its
      # first event, allowing Hermes to return an error instead of hanging.
      HERMES_CODEX_TTFB_STRICT = "1";
      HERMES_CODEX_TTFB_TIMEOUT_SECONDS = "90";
    };

    # Add hermes CLI to system PATH and set HERMES_HOME
    addToSystemPackages = true;
  };

  # Allow Hermes agent to use sudo (wheel group)
  systemd.services.hermes-agent.serviceConfig.SupplementaryGroups = [ "wheel" ];
  systemd.services.hermes-agent.serviceConfig.NoNewPrivileges = lib.mkForce false;
  systemd.services.hermes-agent.serviceConfig.ExecStart = lib.mkForce hermesGateway;
  # The media watcher persists its notification state here. Keep the gateway
  # sandbox read-only elsewhere while permitting this single root-owned path.
  systemd.services.hermes-agent.serviceConfig.ReadWritePaths =
    lib.mkAfter [ "/var/lib/media-queue" ];

  # Self-Interruption Prevention
  systemd.services.hermes-agent.restartIfChanged = false;
  systemd.services.hermes-agent.stopIfChanged = false;
  systemd.services.hermes-dashboard.restartIfChanged = false;
  systemd.services.hermes-dashboard.stopIfChanged = false;

  # Add RTK to the systemd service PATHs
  systemd.services.hermes-agent.path = [
    pkgs.rtk
    pkgs.bitwarden-cli
    pkgs.nodejs
    config.services.transmission.package
  ];
  systemd.services.hermes-agent.environment.PYTHONPATH =
    "${webSearchFallback}/lib/python3.12/site-packages";
  systemd.services.hermes-dashboard.path = [ pkgs.rtk ];
  systemd.services.hermes-dashboard.environment.PYTHONPATH =
    "${webSearchFallback}/lib/python3.12/site-packages";

  # Separate dashboard service for Hermes Desktop app
  systemd.services.hermes-dashboard = {
    description = "Hermes Agent Dashboard for Desktop app";
    after = [
      "network.target"
      "hermes-agent.service"
    ];
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
        "API_SERVER_KEY=" # No auth required - Tailscale secures it
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
    "d /var/lib/hermes           0755 hermes hermes -" # Allow hermes group access
    "d /var/lib/hermes/.hermes   0755 hermes hermes -" # Config dir accessible to group
    "d /var/lib/hermes/.hermes/skills/productivity 0755 hermes hermes -"
    "d /var/lib/hermes/.hermes/skills/media 0755 hermes hermes -"
    "L+ /var/lib/hermes/.hermes/skills/media/media-queue - - - - /etc/nixos/skills/media-queue"
    "d /var/lib/hermes/.hermes/scripts 0755 hermes hermes -"
    # A real local copy is required: Hermes rejects scheduler scripts that
    # resolve through a symlink outside ~/.hermes/scripts.
    "C+ /var/lib/hermes/.hermes/scripts/media-queue-watch.sh 0755 hermes hermes - /etc/nixos/skills/media-queue/scripts/media-queue-watch.sh"
    "z /var/lib/hermes/.hermes/.env 0640 hermes hermes -" # Secrets readable only by Hermes
    "z /var/lib/hermes/.hermes/config.yaml 0644 hermes hermes -" # Config readable by group
    "d /var/lib/hermes/.config/warden-mcp-bw-profiles 0755 hermes hermes -"

    # Symlink Hermes Agent skills to Devin skills directory for persistence
    "d /etc/nixos/.devin         0775 root nixconfig -"
    "d /etc/nixos/.devin/skills  0775 root nixconfig -"
    "L+ /etc/nixos/.devin/skills/nixos-environment - - - - /var/lib/hermes/.hermes/skills/devops/nixos-environment"
    "L+ /etc/nixos/.devin/skills/nixos-hermes-integration - - - - /var/lib/hermes/.hermes/skills/devops/nixos-hermes-integration"
    "L+ /etc/nixos/.devin/skills/nixos-hermes-setup - - - - /var/lib/hermes/.hermes/skills/devops/nixos-hermes-setup"
    "L+ /etc/nixos/.devin/skills/cloakbrowser-nixos-setup - - - - /var/lib/hermes/.hermes/skills/devops/cloakbrowser-nixos-setup"

    # Codex discovers repository-scoped skills from .agents/skills. Keep the
    # skill sources owned by Hermes and expose stable links in this repository.
    "d /etc/nixos/.agents         0775 root nixconfig -"
    "d /etc/nixos/.agents/skills  0775 root nixconfig -"
    "L+ /etc/nixos/.agents/skills/nixos-environment - - - - /var/lib/hermes/.hermes/skills/devops/nixos-environment"
    "L+ /etc/nixos/.agents/skills/nixos-hermes-integration - - - - /var/lib/hermes/.hermes/skills/devops/nixos-hermes-integration"
    "L+ /etc/nixos/.agents/skills/nixos-hermes-setup - - - - /var/lib/hermes/.hermes/skills/devops/nixos-hermes-setup"
    "L+ /etc/nixos/.agents/skills/cloakbrowser-nixos-setup - - - - /var/lib/hermes/.hermes/skills/devops/cloakbrowser-nixos-setup"

    # Caveman is a prompt-only skill.  Link its upstream source from the Nix
    # store so upgrades are pinned in flake.lock and rebuilds cannot overwrite
    # it.  Codex reads the global skill directory and the repository link;
    # Hermes reads its managed productivity skill directory.
    "L+ /home/buildfleet/.codex/skills/caveman - - - - ${caveman}/skills/caveman"
    "L+ /home/buildfleet/.codex/skills/caveman-commit - - - - ${caveman}/skills/caveman-commit"
    "L+ /home/buildfleet/.codex/skills/caveman-review - - - - ${caveman}/skills/caveman-review"
    "L+ /home/buildfleet/.codex/skills/caveman-help - - - - ${caveman}/skills/caveman-help"
    "L+ /home/buildfleet/.codex/skills/caveman-stats - - - - ${caveman}/skills/caveman-stats"
    "L+ /home/buildfleet/.codex/skills/caveman-compress - - - - ${caveman}/skills/caveman-compress"
    "L+ /home/buildfleet/.codex/skills/cavecrew - - - - ${caveman}/skills/cavecrew"
    "L+ /etc/nixos/.agents/skills/caveman - - - - ${caveman}/skills/caveman"
    "L+ /etc/nixos/.agents/skills/caveman-commit - - - - ${caveman}/skills/caveman-commit"
    "L+ /etc/nixos/.agents/skills/caveman-review - - - - ${caveman}/skills/caveman-review"
    "L+ /etc/nixos/.agents/skills/caveman-help - - - - ${caveman}/skills/caveman-help"
    "L+ /etc/nixos/.agents/skills/caveman-stats - - - - ${caveman}/skills/caveman-stats"
    "L+ /etc/nixos/.agents/skills/caveman-compress - - - - ${caveman}/skills/caveman-compress"
    "L+ /etc/nixos/.agents/skills/cavecrew - - - - ${caveman}/skills/cavecrew"
    "L+ /var/lib/hermes/.hermes/skills/productivity/caveman - - - - ${caveman}/skills/caveman"
    "L+ /var/lib/hermes/.hermes/skills/productivity/caveman-commit - - - - ${caveman}/skills/caveman-commit"
    "L+ /var/lib/hermes/.hermes/skills/productivity/caveman-review - - - - ${caveman}/skills/caveman-review"
    "L+ /var/lib/hermes/.hermes/skills/productivity/caveman-help - - - - ${caveman}/skills/caveman-help"
    "L+ /var/lib/hermes/.hermes/skills/productivity/caveman-stats - - - - ${caveman}/skills/caveman-stats"
    "L+ /var/lib/hermes/.hermes/skills/productivity/caveman-compress - - - - ${caveman}/skills/caveman-compress"
    "L+ /var/lib/hermes/.hermes/skills/productivity/cavecrew - - - - ${caveman}/skills/cavecrew"

    # Hermes-created Markdown commonly defaults to 0600. Codex runs as the
    # interactive user, so make the exposed instructions and references
    # world-readable while leaving write access with the Hermes owner.
    "z /var/lib/hermes/.hermes/skills/devops/nixos-environment/SKILL.md 0644 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-environment/references/* 0644 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-environment/templates/* 0644 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-environment/scripts/* 0755 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-hermes-integration/SKILL.md 0644 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-hermes-integration/references/* 0644 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-hermes-setup/SKILL.md 0644 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-hermes-setup/references/* 0644 hermes hermes -"
    "z /var/lib/hermes/.hermes/skills/devops/nixos-hermes-setup/templates/* 0755 hermes hermes -"
  ];
}
