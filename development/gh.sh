#!/bin/bash
set -euo pipefail
GH_TOKEN=$(cat /run/dev-credentials/github-token)
export GH_TOKEN
exec /usr/bin/gh "$@"
