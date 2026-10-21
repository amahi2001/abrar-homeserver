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

Claude is installed but requires your initial account sign-in:

```bash
dev start myproject --agent claude
```

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
Agent/Bun/uv versions are pinned; RTK and Skills CLI follow the personal config
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
```

`DEV_ROOT` and `DEV_STATE_ROOT` can relocate a new installation; existing project
metadata rejects accidental relocation. `DEV_GITHUB_OWNER` changes the default
account for new projects and must match the authenticated GitHub identity.
