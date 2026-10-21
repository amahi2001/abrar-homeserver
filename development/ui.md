# Browser and desktop tools (dev containers only)

Start the project's app on 0.0.0.0, reproduce the issue, inspect console/network
errors, capture relevant screenshots, fix it, then verify the same flow again.
Use structured Playwright snapshots and element references for interaction;
use screenshots for layout and visual checks. Read the installed playwright-cli
skill for commands, traces, recordings, cookies and session management.

`playwright-cli open http://127.0.0.1:3000` uses headless Chromium and the project
session (`PLAYWRIGHT_CLI_SESSION`). The default persistent profile and artifacts
live in `$HOME/.local/share/dev-ui/`, separate from agent account credentials.
The home config is seeded once; project `.playwright/cli.config.json` and explicit
`--config` remain supported. Keep project configs/profiles project-specific.

The image's small CLI shim checkpoints the default session before normal close;
the launcher also checkpoints before stop/recreation. This uses native state-save
and an image-owned initPage hook to avoid a 0.1.22 Chromium shutdown race. The npm
package is untouched. Custom named profiles or configs replacing `browser.initPage`
own their state workflow: use native `state-save`/`state-load`, or include
`/opt/dev/browser-state.cjs` in the project's initPage list. Never commit state files.

`dev-ui start browser` starts the upstream live dashboard on 8931 and opens a blank
session only if no browser is already open. It preserves existing tabs. From the
host use `dev view PROJECT` for the URL and SSH tunnel command. The dashboard
streams the browser and accepts human mouse/keyboard input. Stopping the viewer
leaves browser sessions open; `playwright-cli close` closes the current browser.

For Linux native apps, `dev-ui start desktop` starts a software-rendered 1440x900
desktop on :99. `dev-ui run -- xterm` launches in its display and D-Bus session.
Use `DISPLAY=:99 xdotool ...` for input and `dev-ui screenshot [FILE]` to save an
absolute image path for your image-reading tool. `dev view PROJECT desktop` opens
the noVNC viewer through an SSH tunnel. GUI processes start only on request.

`dev-ui status browser|desktop` reports health. `dev-ui stop browser|desktop`
stops the selected service. Logs and process locks are under `/tmp/dev-ui-UID`;
browser profiles, screenshots and traces persist in the project home. Desktop
windows do not survive stopping the desktop or recreating the box. Never put
browser cookies in shared Claude/Codex account directories. Viewer ports are
host-loopback only; VNC itself is internal and requires no extra model API.
