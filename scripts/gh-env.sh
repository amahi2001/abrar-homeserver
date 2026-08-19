#!/bin/bash
# gh-env.sh — Export GITHUB_TOKEN from stored file for tools that need it
# Installed at: /var/lib/hermes/.hermes/scripts/gh-env.sh
# Usage: source ~/.hermes/scripts/gh-env.sh
if [ -z "$GITHUB_TOKEN" ] && [ -f ~/.hermes/.gh-token ]; then
  read -r GITHUB_TOKEN < ~/.hermes/.gh-token
  export GITHUB_TOKEN
fi