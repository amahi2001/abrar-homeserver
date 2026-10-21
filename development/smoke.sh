#!/usr/bin/env bash
# Live Docker checks; no GitHub or model calls and no real credentials required.
set -euo pipefail
image=${1:?Usage: bash smoke.sh IMAGE}
work=$(mktemp -d)
one="dev-smoke-$$-one"
two="dev-smoke-$$-two"
cleanup() {
  docker rm -f "$one" "$two" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
mkdir -p "$work"/{one,two,home-one,home-two,accounts/codex,accounts/claude,credentials}
printf 'fake-test-credential\n' > "$work/credentials/github-token"
chmod 600 "$work/credentials/github-token"
wait_ready() {
  local name=$1
  for _ in {1..50}; do
    if docker exec --user "$(id -u):$(id -g)" "$name" test -f /tmp/dev-ready 2>/dev/null; then return; fi
    sleep 0.1
  done
  docker logs "$name"
  return 1
}
start() {
  local name=$1 project=$2 project_home=$3
  docker run -d --name "$name" --init \
    -e DEV_UID="$(id -u)" -e DEV_GID="$(id -g)" -e DEV_PROJECT="$project" \
    -e GIT_AUTHOR_NAME=Test -e GIT_AUTHOR_EMAIL=test@example.invalid \
    -e GIT_COMMITTER_NAME=Test -e GIT_COMMITTER_EMAIL=test@example.invalid \
    --mount "type=bind,src=$work/$project,dst=/workspace/$project" \
    --mount "type=bind,src=$work/$project_home,dst=/home/node" \
    --mount "type=bind,src=$work/accounts/codex,dst=/agent/codex" \
    --mount "type=bind,src=$work/accounts/claude,dst=/agent/claude" \
    --mount "type=bind,src=$work/credentials,dst=/run/dev-credentials,readonly" \
    --workdir "/workspace/$project" "$image" >/dev/null
  wait_ready "$name"
}
inside() { docker exec --user "$(id -u):$(id -g)" "$one" "$@"; }
start "$one" one home-one
start "$two" two home-two
inside bash -ec '
  test "$(id -u)" = "$DEV_UID"
  test "$(id -g)" = "$DEV_GID"
  for tool in codex claude bun uv rtk node python3; do "$tool" --version; done
  test "$(command -v gh)" = /usr/local/bin/gh
  bash -lc '\''test "$(command -v gh)" = /usr/local/bin/gh'\''
  git init --quiet
  git remote add probe git@github.com:owner/repo.git
  test "$(git remote get-url probe)" = https://github.com/owner/repo.git
  git remote remove probe
  test -f "$HOME/.agents/skills/ponytail/SKILL.md"
  test -f "$CLAUDE_CONFIG_DIR/skills/caveman/SKILL.md"
  test -f "$CODEX_HOME/AGENTS.md"
  test ! -S /var/run/docker.sock
  sudo -n true
  printf "source\n" > source-marker
  printf "cached\n" > "$HOME/.cache/cache-marker"
  printf "account\n" > "$CLAUDE_CONFIG_DIR/persistence-marker"
  rtk ls .
'
docker exec --user "$(id -u):$(id -g)" "$two" bash -ec '
  test ! -e /workspace/one
  test ! -e "$HOME/.cache/cache-marker"
  test -f "$CLAUDE_CONFIG_DIR/persistence-marker"
'
# Pause setup on a restart of the same writable layer to detect a stale marker.
docker exec -i --user 0 "$one" bash -ec 'cat > /usr/local/bin/install; chmod +x /usr/local/bin/install; touch /tmp/hold-setup' <<'EOF'
#!/bin/bash
if [ -e /tmp/hold-setup ]; then
  touch /tmp/setup-paused
  while [ -e /tmp/hold-setup ]; do sleep 0.05; done
fi
exec /usr/bin/install "$@"
EOF
docker stop "$one" >/dev/null
docker start "$one" >/dev/null
for _ in {1..50}; do
  if inside test -f /tmp/setup-paused; then break; fi
  sleep 0.1
done
inside test -f /tmp/setup-paused
if inside test -e /tmp/dev-ready; then
  echo 'Stale readiness marker visible during restart setup.' >&2
  exit 1
fi
docker exec --user 0 "$one" rm /tmp/hold-setup
wait_ready "$one"
docker exec --user 0 "$one" rm /usr/local/bin/install /tmp/setup-paused
docker stop "$one" >/dev/null
docker rm "$one" >/dev/null
start "$one" one home-one
inside bash -ec 'test -s source-marker; test -s "$HOME/.cache/cache-marker"; test -s "$CLAUDE_CONFIG_DIR/persistence-marker"'
# The noninteractive agent process must retain its lock while running.
inside bash -ec 'flock -n "$HOME/.agent-session.lock" sleep 5' &
lock_pid=$!
sleep 0.3
if inside flock -n /home/node/.agent-session.lock true; then
  echo 'Session lock failed' >&2
  exit 1
fi
wait "$lock_pid"
echo 'Docker smoke checks passed: tools, skills, UID/GID, isolation, restart readiness, persistent source/home/accounts, and locking.'
