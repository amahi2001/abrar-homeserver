# Development containers

Run on `buildfleet-server`, normally over Tailscale SSH:

```bash
dev start myproject
dev start myproject --agent claude
dev start existing --repo owner/repository
dev start myproject --no-agent
dev shell myproject
dev stop myproject
dev list
```

`start` prepares the project and launches the selected agent inside its container.
Codex is the default. `--no-agent` prepares without consuming model tokens.
Personal skills, hooks, instructions, and portable preferences are maintained in
the private `amahi2001/agent-config` repository. Clone it, run
`bun install --frozen-lockfile`, then `bun run sync` on the host. This creates a
versioned snapshot under `~/.local/share/agent-config/`; `dev start` resolves its
`export` link and mounts that version read-only at `/opt/agent-config`. A changed
version recreates the container on the next start while keeping its persistent
source/home/account state. Startup installs from the local snapshot using the
image's pinned Skills CLI without GitHub or dependency downloads. Shared account
setup is serialized across projects. Host sync also adds container routing to
global Codex/Claude instructions; restart host agents after syncing.

Codex account `AGENTS.md` is the shared live instruction file. Claude account
`CLAUDE.md` imports it, so shared edits belong in `AGENTS.md`; keep Claude-specific
rules in its wrapper. Container setup seeds workspace rules only when `AGENTS.md`
is missing and preserves subsequent edits. Personal sync preserves edited shared
rules or reports a conflict when repo defaults changed too. Git/personal sync is
still needed to distribute changes between the host profile and container profile
or other machines; imports do not implement reverse Git synchronization.

Set `DEV_AGENT_CONFIG=/absolute/snapshot/path` to use a different exported snapshot.
Without an export, the image provides tools and workspace instructions; run the
host sync to install your selected skills. Skills are not duplicated in Nix lists.
Nix no longer reasserts personal host skill links; project/Hermes skill links stay
managed by Nix. Private config/authentication is never fetched during a Nix build.

Arguments after `--` are passed directly to the agent, for example:

```bash
dev start myproject -- "Implement the feature described in SPEC.md"
dev start myproject -- resume --last
dev shell myproject -- rtk git status
```

Project source lives at `~/projects/<project>`. Homes and caches live under
`~/.local/state/dev/projects/<project>/home`. Account state lives under
`~/.local/state/dev/accounts` and is shared across the user's trusted containers,
so each agent can reuse its login across projects. Workspace paths include the
project name to keep session discovery separate. GitHub credentials are refreshed
from host `gh` on startup, stored with mode 0600, and mounted read-only.
No credentials are included in the image, source repository, or Compose environment.

The container account matches the invoking user's UID/GID. It has sudo inside
the container for additional project packages. No host Docker socket, SSH keys,
or full host home is mounted. Account credentials remain readable by trusted
project code inside containers. Project Compose overrides are trusted code too.

## GitHub workflow

An explicit `--repo` or existing local GitHub origin takes precedence. Without
either, startup creates `amahi2001/<project>` privately. An existing public repo
at that default name is rejected. Missing assigned repos and network failures
never cause creation of a replacement.

New empty repos get a README and `.gitignore` on `main`; the initial push is
retried on the next start if interrupted. Development happens on an `agent/*`
branch. Starts reuse that branch and preserve local changes. Use `--task name`
to create another branch from the current HEAD when the worktree is clean.
Detached checkouts require an explicit branch before startup.

Agents are instructed to finish automatically with:

```bash
dev-finish "Describe the completed change" --check "bun test" -- src/app.ts src/app.test.ts
```

The helper runs the check first, verifies the branch is unchanged, rejects files
staged by the check, stages only supplied paths, rejects common credential/runtime
paths and preexisting staged work, commits, and pushes the task branch. Failed
checks do not stage files; failed pushes retain the commit. Rerun the same command
to retry a pending push, even when the files are unchanged or were deleted.
SSH-form GitHub remotes use the same HTTPS credentials without mounting host SSH keys.
Agent compliance with workflow instructions is behavioral, not a GitHub branch
protection rule or a comprehensive secret scanner. Force pushes and merges are
not part of the workflow.

## Agent authentication

Codex's existing host `auth.json` seeds the dedicated container account once.
Subsequent starts reuse writable persistent state, including refreshed tokens.
If authentication expires or host/container login refreshes invalidate each other:

```bash
dev shell myproject -- codex login --device-auth
```

Claude's host `.credentials.json` similarly seeds its container account once,
with mode `0600`. The launcher respects host `CLAUDE_CONFIG_DIR`, falling back to
`~/.claude`. Existing container credentials are never overwritten by later starts,
so Claude can retain refreshed tokens. The writable account directory is shared
across projects and persists through stop/start, recreation, and image rebuilds.
Credentials stay outside source, images, snapshots, command arguments, and logs.

After signing into Claude on the server, start a box normally:

```bash
dev start myproject --agent claude
```

If the host has no saved login, or a copied login expires/cannot refresh, sign in
once in a box with `dev shell myproject -- claude auth login`. As with Codex,
separate host/container refreshes can invalidate a copied login; a fresh box login
is retained instead of being replaced by the host's older credential file.

`CLAUDE_CONFIG_DIR` points at persistent account storage, including `.claude.json`.
Portable preferences come from the optional agent-config snapshot; unrelated host
plugins, MCP connections, and machine trust decisions stay on the host. Both agents
get the snapshot's skills/instructions when present. RTK is installed using its
official installer and the snapshot's tools.lock.json; RTK's own init commands
configure its native hooks for each agent. Codex runs without a nested OS sandbox;
Claude skips repeated tool-permission prompts. Docker mounts define their host
filesystem access. One active agent session per project is enforced with `flock`.
The launcher holds its project lock through the session; shells and explicit
`dev stop` remain available while it runs.

## Images, previews, and customization

```bash
dev build
dev rebuild myproject
```

`build` prewarms the shared image. `rebuild` explicitly rebuilds and recreates the
project container, retaining bind-mounted source, home, caches, and account state.
It refuses while an agent session holds the project lock. Ordinary `start` uses
the cached image without pulling or installing tools. Base image digest and
Agent/Bun/uv versions are pinned; RTK, Playwright CLI and Skills CLI follow the personal config
lockfiles when a snapshot is present. Changed tool locks select a new image so
updates are built once and reused across projects. Debian packages resolve at explicit image
build time. Both x86_64 Linux and the server's existing Docker daemon are required.

Ports 3000, 5173, and 8000 map to random **host-loopback** ports. Run `dev list`
to find them, then forward a selected port over SSH, for example:

```bash
ssh -L 3000:127.0.0.1:32768 buildfleet@buildfleet-server.tailcb7cdb.ts.net
```

App servers must bind `0.0.0.0` inside the container. No new public ports are
opened. Each project gets a separate Compose network and separate home/cache.

### Browser automation and optional Linux desktop

```bash
dev view myproject                  # upstream Playwright dashboard
dev view myproject desktop          # noVNC desktop
dev view myproject desktop --stop
dev shell myproject -- playwright-cli open http://127.0.0.1:3000
dev shell myproject -- dev-ui run -- xterm
dev shell myproject -- dev-ui screenshot
```

`view` requires an initialized, running box. It works during an agent session,
does not change Git branches or recreate containers, and prints the actual URL
and SSH forwarding command. Set `DEV_SSH_TARGET=user@tailnet-host` to override the
default current user/hostname. On your computer run the printed tunnel command,
then open the printed URL. Ports 8931 (browser) and 6080 (desktop) map to random
host-loopback ports; VNC port 5900 is never published.

The cached image includes Playwright CLI 0.1.22 (overridden by
`tools.lock.json`'s `playwrightCli.version`), matching Chromium and its dependencies,
and Xvfb/Openbox/D-Bus/x11vnc/noVNC/xdotool/scrot/xterm. Installation follows
[upstream](https://github.com/microsoft/playwright-cli): npm plus
`playwright-cli install-browser --with-deps chromium`. The native skill installer
owns `$HOME/.agents/skills/playwright-cli`, linked into Claude's configured directory.
It runs once per project home/version and needs no network or browser download.

Browser and desktop processes remain stopped until requested. The browser uses
headless Chromium, the project name as its default session, and a persistent
profile/artifacts in `$HOME/.local/share/dev-ui/`. The home config is seeded once;
project Playwright configs are preserved. Starting the browser viewer preserves
open tabs; stopping it leaves browser sessions open. `playwright-cli close` closes
the current browser. Desktop rendering uses software on display :99 at 1440x900.
Running windows do not survive desktop stop/container recreation; saved files do.
Both agents read `/opt/dev/ui.md` through their shared instruction reference.

Inside a box use `dev-ui start|stop|status browser|desktop`, `dev-ui run -- COMMAND`,
or `dev-ui screenshot [FILE]`. Runtime locks/PIDs/logs stay in `/tmp/dev-ui-UID`;
profiles/screenshots/traces persist in the project home, separate from shared
agent authentication. UI failures report logs and release their control lock.
No PinchTab, extra MCP/model API, GPU passthrough or macOS/Windows-native support.

The pinned CLI's close path was observed force-killing Chromium before recent
cookies flush. An image-owned shim in `/opt/dev/bin` checkpoints the default
project session with native `state-save` before normal closure; the launcher does
the same before stop/recreation. The supported `browser.initPage` hook restores
that state on a new session. The npm-owned executable and skill remain untouched.
Snapshots are mode 600 under the project home. Custom named profiles/configs that
replace `browser.initPage` must manage their own state with native state-save/load,
or include `/opt/dev/browser-state.cjs` in their hook list. Abrupt host/container
kills can lose changes since the last checkpoint. Recheck this workaround when
upgrading the CLI; remove it once the shutdown regression test passes without it.

For project databases or extra runtimes, create `.dev/compose.yaml` in the repo.
It is merged with the shared Compose file, using the repository as the base
directory for relative paths. Each project has its own image tag backed by the
shared image's layers, so customized builds cannot replace another project's base.
For example:

```yaml
services:
  dev:
    build:
      context: .
      dockerfile: .dev/Dockerfile
      args:
        BASE_IMAGE: ${DEV_BASE_IMAGE}
```

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev
```

Leave the inherited image name in place. New custom images build on first start;
use `dev rebuild <project>` after changing a recipe. The metadata `baseImage`
records the shared base. Named database volumes survive
stop/start and rebuild; `dev` never deletes volumes.

## NixOS integration and validation

`development.nix` installs `dev` and narrows Docker's existing weekly pruning to
dangling images. Personal host agent settings/skills are managed by agent-config.
Stopped containers, tagged images, volumes, and build caches are preserved.

```bash
nix build /etc/nixos#dev --out-link ~/.local/state/dev-package
nix build /etc/nixos#checks.x86_64-linux.development --no-link -L
```

The development check allows 30 seconds per test for slower machines.

The initial targeted rollout uses `~/.local/bin/dev` linked to that GC-rooted
package, plus this module's Docker-prune drop-in. This avoids
activating unrelated pending server changes. The normal next NixOS rebuild
manages the same settings system-wide. After editing launcher/image/skills,
rebuild the package above before running `dev rebuild` for existing projects.
An offline Docker smoke check can test the defaults or a personal snapshot:

```bash
bash development/smoke.sh IMAGE_TAG
bash development/smoke.sh IMAGE_TAG ~/.local/share/agent-config/export
DEV_UI_SMOKE=1 bash development/smoke.sh IMAGE_TAG ~/.local/share/agent-config/export
```

The UI smoke test runs offline local fixtures: browser input/errors/reload fixes,
screenshots/traces/video, live dashboard and noVNC mouse/keyboard control, native
terminal input, service restart/failure recovery, and cookie/artifact persistence
and isolation after recreation. It uses temporary homes and fake credentials.

`DEV_ROOT` and `DEV_STATE_ROOT` can relocate a new installation; existing project
metadata rejects accidental relocation. `DEV_GITHUB_OWNER` changes the default
account for new projects and must match the authenticated GitHub identity.

### Browser/desktop rollout verification (2026-10-21)

- Nix development check: 27 launcher tests, 188 assertions. Agent-config: 10 tests,
  63 assertions, including published-version validation and existing auth/config preservation.
- Offline Docker UI smoke with the personal snapshot: click/type flow, console and
  HTTP errors, reload after a UI change, screenshots, raw traces and WebM recording;
  dashboard streaming/input; noVNC input; terminal/xdotool/screenshots; repeat starts,
  occupied-port failure, crashed-process recovery and cleanup all passed.
- Recreated temporary boxes retained cookies/artifacts and kept a second project
  isolated. A stale Chromium lock pointing to PID 1 was cleared. Native skill
  installation timestamps and shared instruction edits survived unchanged.
- Codex app-server `skills/list` and Claude's initialization metadata both reported
  `playwright-cli`. Discovery required no model call (Claude's API was pointed at
  an unreachable local endpoint with a fake test key).
- Real SSH forwarding carried dashboard frames, browser mouse/keyboard actions,
  and noVNC keyboard input that wrote a marker from xterm. Temporary SSH keys,
  listener and tunnels were removed after verification.
- Deployed the targeted package/image to the sandbox first, then idle agent-config.
  A launcher-driven sandbox rebuild retained a newly set cookie, both agents'
  authentication and the existing branch. Warm starts measured 1.26s / 1.27s;
  unchanged starts downloaded nothing and launched no UI services.
- Previous tagged images remain available for rollback. Unrelated pending NixOS
  changes were not activated. GPU and macOS/Windows-native UI testing remain deferred.
