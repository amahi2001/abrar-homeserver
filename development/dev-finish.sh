#!/bin/bash
set -euo pipefail
if [ "$#" -lt 5 ] || [ "$2" != --check ]; then
  echo 'Usage: dev-finish "commit message" --check "test command" -- path [path ...]' >&2
  exit 2
fi
message=$1
check=$3
shift 3
[ "$1" = -- ] || { echo 'Explicit paths required after --' >&2; exit 2; }
shift
[ "$#" -gt 0 ] && [ -n "$check" ] && [ -n "$message" ] || exit 2
branch=$(git symbolic-ref --short HEAD)
case "$branch" in agent/*) ;; *) echo 'Refusing to publish outside an agent/* task branch.' >&2; exit 1 ;; esac
# Existing staged work belongs to its author; never silently include it.
git diff --cached --quiet || { echo 'Index already has staged changes; review them before dev-finish.' >&2; exit 1; }
bash -o pipefail -c "$check"
[ "$(git symbolic-ref --short HEAD)" = "$branch" ] || { echo 'Check changed the branch; refusing to stage or commit.' >&2; exit 1; }
git add -- "$@"
while IFS= read -r -d '' path; do
  case "/$path" in
    */.env|*/.env.*|*/auth.json|*/.credentials.json|*/hosts.yml|*/.claude.json|*/.codex/*|*/.claude/*|*/node_modules/*|*/.venv/*|*.pem|*.key|*.secret)
      case "$path" in *.example) continue ;; esac
      echo "Refusing credential/runtime file: $path. Changes remain staged for review." >&2; exit 1 ;;
  esac
done < <(git diff --cached --name-only -z)
if git diff --cached --quiet; then
  echo 'No new changes; pushing any pending commits.'
else
  git commit -m "$message"
fi
git push --set-upstream origin "$branch"
