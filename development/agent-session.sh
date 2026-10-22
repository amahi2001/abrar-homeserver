#!/bin/bash
set -euo pipefail
exec 9> "$HOME/.agent-session.lock"
flock -n 9 || { echo 'Agent already active for this project. Use dev shell or resume its terminal.' >&2; exit 1; }
agent=${1:-codex}
shift || true
# Refresh dirty files and discover checkouts added since container startup.
dev-memory index
case "$agent" in
  codex)
    if ! codex login status >/dev/null 2>&1; then
      echo 'Codex needs initial authentication. Run: codex login --device-auth' >&2
      exit 1
    fi
    # Container mounts, rather than a nested OS sandbox, define the workspace.
    exec codex --no-daemon --sandbox danger-full-access --ask-for-approval never -c "projects.\"$PWD\".trust_level=\"trusted\"" "$@"
    ;;
  claude) exec claude --dangerously-skip-permissions "$@" ;;
  *) echo "Unknown agent: $agent" >&2; exit 2 ;;
esac
