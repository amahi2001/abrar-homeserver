---
name: vaultwarden
description: Check a dedicated Codex Vaultwarden account and look up approved redacted item metadata with Warden MCP.
---

Use Warden with a dedicated account that can access only the collection required for the requested workflow. The host registration is disabled by default. Enable it for a new session with `codex -c mcp_servers.warden.enabled=true` after the dedicated account is provisioned.

The launcher reads `credentials.json` under `${XDG_CONFIG_HOME:-$HOME/.config}/codex-warden`. The operator supplies a user-owned file with mode `0600` containing `BW_CLIENTID`, `BW_CLIENTSECRET`, and `BW_PASSWORD`. Enter those values outside the chat. Keep the dedicated credentials separate from the Hermes account.

The available tools check status, search scoped item metadata, and read redacted items. If a browser workflow requires a password, have the user sign in through that browser profile.
