#!/bin/bash
# gh-token.sh — Extract GitHub token from git-credentials
# Installed at: /var/lib/hermes/.hermes/scripts/gh-token.sh
grep 'github.com' ~/.git-credentials | head -1 | sed 's|.*://[^:]*:\([^@]*\)@.*|\1|'