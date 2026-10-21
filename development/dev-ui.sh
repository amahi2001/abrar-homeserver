#!/bin/bash
set -euo pipefail
runtime="/tmp/dev-ui-$(id -u)"
artifacts="$HOME/.local/share/dev-ui/artifacts"
mkdir -p "$runtime" "$artifacts"
chmod 700 "$runtime"
export PLAYWRIGHT_CLI_SESSION="${PLAYWRIGHT_CLI_SESSION:-${DEV_PROJECT:-default}}"

usage() { echo 'Usage: dev-ui start|stop|status browser|desktop | run -- COMMAND | screenshot [FILE]' >&2; exit 2; }
action=${1:-}
service=${2:-}
if [ "$action" = _checkpoint ]; then
  exec 8>"$runtime/checkpoint.lock"
  flock -w 30 8 || { echo 'dev-ui: browser checkpoint is busy' >&2; exit 1; }
  state="$HOME/.local/share/dev-ui/browser-state.json"
  sessions=$(/usr/local/bin/playwright-cli list --json)
  if jq -e --arg name "$PLAYWRIGHT_CLI_SESSION" '.browsers | any(.name == $name and .status == "open")' <<< "$sessions" >/dev/null; then
    # Rename only a complete snapshot; failed saves must preserve the last one.
    timeout 15 /usr/local/bin/playwright-cli state-save "$state.tmp.json" >"$runtime/checkpoint.log" 2>&1 || { cat "$runtime/checkpoint.log" >&2; exit 1; }
    chmod 600 "$state.tmp.json"
    mv -f "$state.tmp.json" "$state"
  fi
  exit
fi
if [ "$action" = _desktop ]; then
  export DISPLAY=:99 LIBGL_ALWAYS_SOFTWARE=1
  # Keep the display, window manager and viewer in one supervised process group.
  trap 'trap "" TERM; kill -TERM -- -$$ 2>/dev/null || true' EXIT
  dbus-daemon --session --nofork --print-address=3 3>"$runtime/dbus-address" &
  for _ in {1..100}; do [ -s "$runtime/dbus-address" ] && break; sleep 0.1; done
  export DBUS_SESSION_BUS_ADDRESS="$(cat "$runtime/dbus-address")"
  Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
  for _ in {1..100}; do timeout 1 xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
  timeout 1 xdpyinfo >/dev/null
  openbox --sm-disable &
  x11vnc -display :99 -listen 127.0.0.1 -no6 -rfbport 5900 -forever -shared -nopw &
  websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 &
  wait -n
  exit 1
fi
if [ "$action" = run ]; then
  [ "$service" = -- ] && [ "$#" -ge 3 ] || usage
  "$0" start desktop
  shift 2
  # Display teardown terminates connected GUI apps; saved files remain in the home.
  exec env DISPLAY=:99 LIBGL_ALWAYS_SOFTWARE=1 DBUS_SESSION_BUS_ADDRESS="$(cat "$runtime/dbus-address")" "$@"
fi
if [ "$action" = screenshot ]; then
  [ "$#" -le 2 ] || usage
  "$0" start desktop >/dev/null
  file=$(realpath -m -- "${2:-$artifacts/desktop-$(date +%Y%m%d-%H%M%S-%N).png}")
  mkdir -p "$(dirname "$file")"
  DISPLAY=:99 scrot --overwrite "$file"
  printf '%s\n' "$file"
  exit
fi
[[ "$action" =~ ^(start|stop|status)$ ]] && [[ "$service" =~ ^(browser|desktop)$ ]] && [ "$#" = 2 ] || usage
exec 8>"$runtime/$service.lock"
flock -w 30 8 || { echo "dev-ui: $service control is busy" >&2; exit 1; }
pidfile="$runtime/$service.pid"
log="$runtime/$service.log"
pid= stamp=
if [ -f "$pidfile" ]; then read -r pid stamp < "$pidfile" || true; fi
alive() {
  [[ "$pid" =~ ^[0-9]+$ ]] && [ -r "/proc/$pid/stat" ] &&
    [ "$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null)" = "$stamp" ] && kill -0 "$pid" 2>/dev/null
}
ready() {
  alive || return 1
  if [ "$service" = browser ]; then
    curl -fsS --max-time 1 http://127.0.0.1:8931/ >/dev/null
  else
    DISPLAY=:99 timeout 1 xdpyinfo >/dev/null 2>&1 && [ -s "$runtime/dbus-address" ] &&
      curl -fsS --max-time 1 http://127.0.0.1:6080/vnc.html >/dev/null &&
      timeout 1 bash -c 'exec 3<>/dev/tcp/127.0.0.1/5900'
  fi
}
stop() {
  if alive; then
    kill -TERM -- "-$pid" 2>/dev/null || true
    for _ in {1..30}; do kill -0 -- "-$pid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL -- "-$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
  if [ "$service" = desktop ]; then rm -f "$runtime/dbus-address"; fi
}
if [ "$action" = status ]; then
  if ready 2>/dev/null; then echo "$service running (pid $pid)"; else echo "$service stopped or unhealthy; log: $log"; fi
  exit
fi
if [ "$action" = stop ]; then stop; echo "$service stopped"; exit; fi
ensure_browser() {
  # Listing sessions never navigates an existing browser. Only open if none is open.
  local sessions
  if ! sessions=$(playwright-cli list --json 2>>"$log"); then return 1; fi
  if ! jq -e '.browsers | any(.status == "open")' <<< "$sessions" >/dev/null; then
    playwright-cli open about:blank >>"$log" 2>&1 || return 1
  fi
}
failed() {
  stop
  echo "dev-ui: $service failed to start; log: $log" >&2
  tail -n 40 "$log" >&2
  exit 1
}
if ready 2>/dev/null; then
  if [ "$service" = browser ]; then ensure_browser || failed; fi
  echo "$service running (pid $pid)"; exit
fi
stop
port=8931
if [ "$service" = desktop ]; then port=6080; fi
if timeout 1 bash -c 'exec 3<>/dev/tcp/127.0.0.1/"$1"' _ "$port" 2>/dev/null; then
  echo "dev-ui: $service port $port is already in use; log: $log" | tee "$log" >&2
  exit 1
fi
if [ "$service" = browser ]; then
  ensure_browser || failed
  setsid playwright-cli show --port 8931 --host 0.0.0.0 >"$log" 2>&1 8>&- </dev/null &
else
  setsid "$0" _desktop >"$log" 2>&1 8>&- </dev/null &
fi
pid=$!
stamp=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null) || failed
printf '%s %s\n' "$pid" "$stamp" > "$pidfile"
for _ in {1..100}; do
  if ready 2>/dev/null; then echo "$service running (pid $pid); log: $log"; exit; fi
  alive || break
  sleep 0.1
done
failed
