#!/usr/bin/env bash
# Hermes runs this through bash every five minutes.  The CLI emits output only
# when a watched download changes to a state worth notifying about.
# Hermes cron has a deliberately minimal PATH; use the NixOS system-profile
# path so sudo cannot lose the media-queue command through secure_path.
exec /usr/bin/sudo /run/current-system/sw/bin/media-queue watch
