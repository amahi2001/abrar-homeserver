#!/usr/bin/env bash
set -euo pipefail
# Empty stdout is intentional: Hermes no-agent jobs stay silent for owned films.
exec /usr/bin/sudo /run/current-system/sw/bin/media-queue availability Buddy --year 2026 --limit 10
