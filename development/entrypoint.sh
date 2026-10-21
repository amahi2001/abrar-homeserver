#!/bin/bash
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
  # Only change container account metadata; host project files keep their owner.
  if [ "$(id -g node)" != "$DEV_GID" ]; then groupmod -o -g "$DEV_GID" node; fi
  if [ "$(id -u node)" != "$DEV_UID" ]; then usermod -o -u "$DEV_UID" node; fi
  mkdir -p /home/node/.cache /home/node/.local/bin /home/node/.agents/skills
  chown node:node /home/node /home/node/.cache /home/node/.local /home/node/.local/bin /home/node/.agents /home/node/.agents/skills
  exec setpriv --reuid=node --regid=node --init-groups "$0" "$@"
fi
mkdir -p "$CODEX_HOME" "$CLAUDE_CONFIG_DIR"
for skill in /opt/dev/skills/*/; do
  ln -sfn "$skill" "$HOME/.agents/skills/$(basename "$skill")"
done
mkdir -p "$CLAUDE_CONFIG_DIR/skills"
for skill in /opt/dev/skills/*/; do
  ln -sfn "$skill" "$CLAUDE_CONFIG_DIR/skills/$(basename "$skill")"
done
install -m 600 /opt/dev/instructions.md "$CODEX_HOME/AGENTS.md"
install -m 600 /opt/dev/instructions.md "$CLAUDE_CONFIG_DIR/CLAUDE.md"
git config --global user.name "$GIT_AUTHOR_NAME"
git config --global user.email "$GIT_AUTHOR_EMAIL"
git config --global credential.https://github.com.helper '!gh auth git-credential'
git config --global url.https://github.com/.insteadOf git@github.com:
git config --global init.defaultBranch main
git config --global push.default current
touch /tmp/dev-ready
exec "$@"
