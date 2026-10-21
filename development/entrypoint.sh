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
flock -u 9
exec 9>&-
git config --global user.name "$GIT_AUTHOR_NAME"
git config --global user.email "$GIT_AUTHOR_EMAIL"
git config --global credential.https://github.com.helper '!gh auth git-credential'
git config --global url.https://github.com/.insteadOf git@github.com:
git config --global init.defaultBranch main
git config --global push.default current
touch /tmp/dev-ready
exec "$@"
