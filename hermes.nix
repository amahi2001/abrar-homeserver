{
  config,
  options,
  pkgs,
  lib,
  cloakbrowser,
  caveman,
  hermesAgent,
  ...
}:

let
  # The upstream lock's 50.0.1 wheel statically embeds vulnerable OpenSSL 4.0.2.
  # Use pyproject.nix's package overlay so the remaining locked graph stays intact.
  hermesPythonSecurity = final: prev: {
    brotlicffi =
      if lib.versionOlder prev.brotlicffi.version "1.2.1.0" then
        prev.brotlicffi.overrideAttrs (_: {
          version = "1.2.1.0";
          src = pkgs.fetchurl {
            url = "https://files.pythonhosted.org/packages/f1/93/e2735a441a0f071b8094a2b9baadad5fb9811708c088b294ccb0e90aeb46/brotlicffi-1.2.1.0-cp39-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl";
            sha256 = "0a7281a39de91432e90c27ab9c4f9feb3a9cf33fda3873534c2a88270d9f60a2";
          };
        })
      else
        prev.brotlicffi;
    cryptography =
      if
        lib.versionAtLeast prev.cryptography.version "50.0.0"
        && lib.versionOlder prev.cryptography.version "50.0.2"
      then
        prev.cryptography.overrideAttrs (_: {
          version = "50.0.2";
          src = pkgs.fetchurl {
            url = "https://files.pythonhosted.org/packages/21/69/64cef1f702bf6657e0cc186ed1a2891d50d29fb41586b254e1c07adea261/cryptography-50.0.2-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl";
            sha256 = "630ebfea3bf689d075f82316324ff7433dc447fe6bc1bfc76524b74b4a9567d2";
          };
        })
      else
        prev.cryptography;
  };

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

    # The gateway's local API shares the dashboard's public-URL auth gate.
    : "''${HERMES_DASHBOARD_SESSION_TOKEN:?Set HERMES_DASHBOARD_SESSION_TOKEN in /var/lib/hermes/env}"
    export HERMES_DASHBOARD_BASIC_AUTH_USERNAME=hermes
    export HERMES_DASHBOARD_BASIC_AUTH_PASSWORD="$HERMES_DASHBOARD_SESSION_TOKEN"
    export HERMES_DASHBOARD_BASIC_AUTH_SECRET="$(${pkgs.coreutils}/bin/printf 'hermes-dashboard-session:%s' "$HERMES_DASHBOARD_SESSION_TOKEN" | ${pkgs.coreutils}/bin/sha256sum | ${pkgs.coreutils}/bin/cut -d ' ' -f 1)"

    exec ${config.services.hermes-agent.package}/bin/hermes gateway
  '';

  # Use the same flake-pinned Hermes package as the messaging gateway. The
  # dashboard was previously launched from hermes' unrelated Nix profile,
  # which remained on 0.16.0 after system upgrades.
  hermesDashboard = pkgs.writeShellScript "hermes-dashboard" ''
    set -euo pipefail
    : "''${HERMES_DASHBOARD_SESSION_TOKEN:?Set HERMES_DASHBOARD_SESSION_TOKEN in /var/lib/hermes/env}"
    export HERMES_DASHBOARD_BASIC_AUTH_USERNAME=hermes
    export HERMES_DASHBOARD_BASIC_AUTH_PASSWORD="$HERMES_DASHBOARD_SESSION_TOKEN"
    export HERMES_DASHBOARD_BASIC_AUTH_SECRET="$(${pkgs.coreutils}/bin/printf 'hermes-dashboard-session:%s' "$HERMES_DASHBOARD_SESSION_TOKEN" | ${pkgs.coreutils}/bin/sha256sum | ${pkgs.coreutils}/bin/cut -d ' ' -f 1)"
    exec ${config.services.hermes-agent.package}/bin/hermes dashboard --host 0.0.0.0 --port 9119 --no-open --skip-build
  '';
in
{
  system.checks = [
    (pkgs.runCommand "hermes-cryptography-security-check" { } ''
      ${config.services.hermes-agent.package.hermesVenv}/bin/python3 -P ${./scripts/check-hermes-crypto.py}
      touch "$out"
    '')
  ];

  # Add hermes user to nixconfig group (hermes user is created by hermes-agent module)
  users.users.hermes.extraGroups = [
    "wheel"
    "nixconfig"
  ];

  # Hermes Agent - AI assistant with GPT-6 Luna via OpenAI Codex
  services.hermes-agent = {
    enable = true;

    # Hermes pins its own nixpkgs; replace both its Node runtime and npm wrapper dependency.
    package = options.services.hermes-agent.package.default.override (
      original:
      {
        callPackage =
          let
            patchedCallPackage =
              path: args:
              let
                accepted = builtins.functionArgs (if builtins.isFunction path then path else import path);
                patched = {
                  nodejs = pkgs.nodejs_26;
                  nodejs_22 = pkgs.nodejs_22;
                  nodejs_24 = pkgs.nodejs_24;
                  nodejs_26 = pkgs.nodejs_26;
                  callPackage = patchedCallPackage;
                };
                result = original.callPackage path (args // builtins.intersectAttrs accepted patched);
              in
              if
                builtins.attrNames accepted == [
                  "buildPackages"
                  "newScope"
                  "pkgs"
                  "python"
                  "stdenv"
                ]
              then
                result
                // {
                  overrideScope = overlay: result.overrideScope (lib.composeExtensions overlay hermesPythonSecurity);
                }
              else
                result;
          in
          patchedCallPackage;
      }
      // lib.optionalAttrs (options.services.hermes-agent.package.default.version == "0.0.0") (
        let
          pinned = (builtins.fromJSON (builtins.readFile ./flake.lock)).nodes.hermes-agent.original;
          release = builtins.match "v([0-9]{1,3}\\.[0-9]+\\.[0-9]+)" (pinned.ref or "");
        in
        # New upstream sources obtain their runtime identity from this build stamp.
        assert lib.assertMsg (
          release != null && hermesAgent ? rev
        ) "Hermes requires a GitHub release input with a locked commit for its runtime version stamp";
        {
          version = builtins.head release;
          rev = hermesAgent.rev;
        }
      )
    );

    # Extra Python deps and dependency groups for platform adapters and providers
    extraDependencyGroups = [
      "anthropic"
      "messaging"
    ];

    # Use OpenAI Codex as the provider
    settings = {
      model = {
        base_url = "https://chatgpt.com/backend-api/codex";
        default = "gpt-6-luna";
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
      # Global exclusions apply to CLI, Desktop and messaging, and survive updates.
      # Keep the installed files so any skill can be enabled again.
      skills.disabled = [
        # Preserve the existing exclusions.
        "ai-music-generation"
        "airtable"
        "ascii-art"
        "ascii-video"
        "audiocraft-audio-generation"
        "baoyu-article-illustrator"
        "baoyu-comic"
        "baoyu-infographic"
        "box"
        "cavecrew"
        "caveman-help"
        "caveman-review"
        "caveman-stats"
        "gif-search"
        "heartmula"
        "linear"
        "minecraft-modpack-server"
        "obsidian"
        "openhue"
        "pokemon-player"
        "songsee"
        "songwriting-and-ai-music"
        "teams-meeting-pipeline"
        "touchdesigner-mcp"
        "yuanbao"
        # No recorded use or matching tasks in the conversation and usage history.
        "comfyui"
        "dspy"
        "godmode"
        "jupyter-live-kernel"
        "manim-video"
        "obliteratus"
        "p5js"
        "petdex"
        "pixel-art"
        "polymarket"
        "pretext"
        "research-paper-writing"
        "sec-financial-research"
        "segment-anything-model"
        "spotify"
        "xurl"
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
      cloakbrowser = {
        command = "/var/lib/hermes/.hermes/.venv/bin/cloakbrowsermcp";
        args = [
          "--caps"
          "all"
        ];
        env = {
          CLOAKBROWSER_BINARY_PATH = "${
            cloakbrowser.packages.${pkgs.stdenv.hostPlatform.system}.default
          }/bin/cloakbrowser-chrome";
          LD_LIBRARY_PATH = lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
          PLAYWRIGHT_NODEJS_PATH = "${pkgs.nodejs}/bin/node";
        };
        timeout = 120;
        connect_timeout = 60;
        enabled = true;
      };

      warden-mcp = {
        # Hermes writes environmentFiles into its generated dotenv, but MCP
        # subprocesses do not inherit those values. Load the protected source
        # file explicitly so Warden receives its required BW_* credentials.
        command = "${pkgs.writeShellScript "warden-mcp" ''
          set -a
          . /var/lib/hermes/env
          set +a
          export BW_BIN=${pkgs.bitwarden-cli}/bin/bw
          exec ${pkgs.python3}/bin/python -P ${./scripts/warden-runtime.py} \
            ${pkgs.nodejs_22}/bin/node ${pkgs.nodejs}/bin/node \
            /var/lib/hermes/.hermes/warden-mcp/node_modules/@icoretech/warden-mcp/package.json \
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
  # Permit sidecars in organized libraries without remounting the gateway root.
  systemd.services.hermes-agent.serviceConfig.ReadWritePaths = lib.mkAfter [
    "/var/lib/media-queue"
    "/srv/data/media/library/movies"
    "/srv/data/media/library/tv"
  ];

  # Self-Interruption Prevention
  systemd.services.hermes-agent.restartIfChanged = false;
  systemd.services.hermes-agent.stopIfChanged = false;
  # A package change must replace the running Desktop backend.
  systemd.services.hermes-dashboard.restartIfChanged = true;
  systemd.services.hermes-dashboard.stopIfChanged = true;

  # Add RTK to the systemd service PATHs
  systemd.services.hermes-agent.path = [
    pkgs.rtk
    pkgs.bitwarden-cli
    pkgs.nodejs
    pkgs.jellyfin-ffmpeg
    config.services.transmission.package
  ];
  systemd.services.hermes-agent.environment.PYTHONPATH =
    "${webSearchFallback}/lib/python3.12/site-packages";
  systemd.services.hermes-agent.environment.PLAYWRIGHT_NODEJS_PATH = "${pkgs.nodejs}/bin/node";
  systemd.services.hermes-dashboard.path = [ pkgs.rtk ];
  systemd.services.hermes-dashboard.environment.PYTHONPATH =
    "${webSearchFallback}/lib/python3.12/site-packages";
  systemd.services.hermes-dashboard.environment.PLAYWRIGHT_NODEJS_PATH = "${pkgs.nodejs}/bin/node";

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
      ExecStart = hermesDashboard;
      Restart = "always";
      RestartSec = 5;
    };
  };
  systemd.services.hermes-dashboard.serviceConfig.SupplementaryGroups = [ "wheel" ];

  # Shared writable control directories make Hermes reject its own gateway.
  systemd.tmpfiles.settings."00-hermes-control" =
    lib.genAttrs [ "/var/lib/hermes" "/var/lib/hermes/.hermes" ] (_: {
      d = {
        mode = "2750";
        user = "hermes";
        group = "hermes";
      };
    });
  system.activationScripts.hermes-control-permissions =
    lib.stringAfter [ "hermes-agent-setup" ] ''
      chmod 2750 /var/lib/hermes /var/lib/hermes/.hermes
    '';

  # Create Hermes data directories & Symlinks
  systemd.tmpfiles.rules = [
    # Make sudo available on the gateway subprocess PATH (NixOS puts it at /run/wrappers/bin)
    "L+ /usr/bin/sudo - - - - /run/wrappers/bin/sudo"
    "d /var/lib/hermes/.hermes/skills/productivity 0755 hermes hermes -"
    "d /var/lib/hermes/.hermes/skills/media 0755 hermes hermes -"
    "L+ /var/lib/hermes/.hermes/skills/media/media-queue - - - - /etc/nixos/skills/media-queue"
    "L+ /var/lib/hermes/.hermes/skills/media/media-cleanup - - - - /etc/nixos/skills/media-cleanup"
    "L+ /var/lib/hermes/.hermes/skills/media/media-subtitles - - - - /etc/nixos/skills/media-subtitles"
    "d /var/lib/hermes/.hermes/scripts 0755 hermes hermes -"
    # A real local copy is required: Hermes rejects scheduler scripts that
    # resolve through a symlink outside ~/.hermes/scripts.
    "C+ /var/lib/hermes/.hermes/scripts/media-queue-watch.sh 0755 hermes hermes - /etc/nixos/skills/media-queue/scripts/media-queue-watch.sh"
    "C+ /var/lib/hermes/.hermes/scripts/morning_brew_daily.py 0750 hermes hermes - /etc/nixos/scripts/morning_brew_daily.py"
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

    # Keep Hermes skills pinned in flake.lock.
    # Personal global agent skills are synced from the separate agent-config repo.
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
