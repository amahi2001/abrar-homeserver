#!/bin/bash
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
  rm -f /tmp/dev-ready
  # Only change container account metadata; host project files keep their owner.
  if [ "$(id -g node)" != "$DEV_GID" ]; then groupmod -o -g "$DEV_GID" node; fi
  if [ "$(id -u node)" != "$DEV_UID" ]; then usermod -o -u "$DEV_UID" node; fi
  mkdir -p /home/node/.cache /home/node/.local/bin /home/node/.agents/skills
  chown node:node /home/node /home/node/.cache /home/node/.local /home/node/.local/bin /home/node/.agents /home/node/.agents/skills
  exec setpriv --reuid=node --regid=node --init-groups "$0" "$@"
fi
mkdir -p "$CODEX_HOME" "$CLAUDE_CONFIG_DIR"
# Account config is shared between projects; serialize setup across containers.
exec 9>"$CODEX_HOME/.dev-config.lock"
flock 9
mkdir -p "$CLAUDE_CONFIG_DIR/skills"
# Seed once: shared account instructions may have been edited by either agent.
if [ ! -f "$CODEX_HOME/AGENTS.md" ]; then
  install -m 600 /opt/dev/instructions.md "$CODEX_HOME/AGENTS.md"
fi
# Remove only the old, image-owned preamble before personal sync migrates its block.
if [ -f "$CLAUDE_CONFIG_DIR/CLAUDE.md" ] && [ -f "$CODEX_HOME/.dev-container-instructions.md" ]; then
  bun -e 'const fs = require("node:fs"); const [path, marker] = process.argv.slice(1); const text = fs.readFileSync(path, "utf8"), base = fs.readFileSync(marker, "utf8"); if (text.startsWith(base)) fs.writeFileSync(path, text.slice(base.length), {mode: 0o600});' "$CLAUDE_CONFIG_DIR/CLAUDE.md" "$CODEX_HOME/.dev-container-instructions.md"
fi
if [ ! -s "$CLAUDE_CONFIG_DIR/CLAUDE.md" ]; then
  printf '@%s/AGENTS.md\n\nEdit shared rules in the imported AGENTS.md.\n' "$CODEX_HOME" > "$CLAUDE_CONFIG_DIR/CLAUDE.md"
  chmod 600 "$CLAUDE_CONFIG_DIR/CLAUDE.md"
fi
install -m 600 /opt/dev/instructions.md "$CODEX_HOME/.dev-container-instructions.md"
if [ -f /opt/agent-config/sync.js ]; then
  bun /opt/agent-config/sync.js --container
fi
# Upstream owns MCP entries, skills, profiles and hooks for both harnesses.
dev-memory setup
# Native Playwright owns this skill; its version stamp belongs to the project home.
pw_version=$(playwright-cli --version)
pw_stamp="$HOME/.local/state/dev-playwright-version"
if [ ! -f "$HOME/.agents/skills/playwright-cli/SKILL.md" ] || [ "$(cat "$pw_stamp" 2>/dev/null || true)" != "$pw_version" ]; then
  playwright-cli install --skills=agents --global
  mkdir -p "$(dirname "$pw_stamp")"
  printf '%s\n' "$pw_version" > "$pw_stamp"
fi
ln -sfn "$HOME/.agents/skills/playwright-cli" "$CLAUDE_CONFIG_DIR/skills/playwright-cli"
mkdir -p "$HOME/.playwright" "$HOME/.local/share/dev-ui/browser-profile" "$HOME/.local/share/dev-ui/artifacts"
# A fresh container has no browser; persisted singleton links refer to old PIDs/sockets.
rm -f "$HOME/.local/share/dev-ui/browser-profile/SingletonLock" "$HOME/.local/share/dev-ui/browser-profile/SingletonSocket" "$HOME/.local/share/dev-ui/browser-profile/SingletonCookie"
if [ ! -f "$HOME/.playwright/cli.config.json" ]; then
  bun -e 'const home=process.env.HOME; await Bun.write(`${home}/.playwright/cli.config.json`, JSON.stringify({browser:{browserName:"chromium",userDataDir:`${home}/.local/share/dev-ui/browser-profile`,initPage:["/opt/dev/browser-state.cjs"],launchOptions:{channel:"chromium",headless:true}},outputDir:`${home}/.local/share/dev-ui/artifacts`},null,2)+"\n");'
fi
if ! grep -Fq '/opt/dev/ui.md' "$CODEX_HOME/AGENTS.md"; then
  printf '\nWhen DEV_CONTAINER=1, read /opt/dev/ui.md for browser automation, visual debugging and optional desktop access.\n' >> "$CODEX_HOME/AGENTS.md"
fi
flock -u 9
exec 9>&-
git config --global user.name "$GIT_AUTHOR_NAME"
git config --global user.email "$GIT_AUTHOR_EMAIL"
git config --global credential.https://github.com.helper '!gh auth git-credential'
git config --global url.https://github.com/.insteadOf git@github.com:
git config --global init.defaultBranch main
git config --global push.default current
dev-memory index
touch /tmp/dev-ready
exec "$@"
