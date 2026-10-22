#!/usr/bin/env bash
set -euo pipefail

./config.sh --unattended --replace \
  --url "$RUNNER_URL" --name "$RUNNER_NAME" --labels "$RUNNER_LABELS" \
  --work /home/runner/_work --token "$RUNNER_TOKEN"
unset RUNNER_TOKEN
exec ./run.sh
