#!/bin/bash
set -euo pipefail
# Keep the npm-owned CLI untouched. Its 0.1.22 close path can kill Chromium before
# cookies flush; checkpoint with the official state command before normal closure.
for arg in "$@"; do
  case "$arg" in
    -*) continue ;;
    close|close-all) dev-ui _checkpoint; break ;;
    *) break ;;
  esac
done
exec /usr/local/bin/playwright-cli "$@"
