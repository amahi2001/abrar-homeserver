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

The helper runs the check first, verifies the branch is unchanged, stages only
supplied paths, rejects common credential/runtime paths and preexisting staged
work, commits, and pushes the task branch. Failed checks do not stage files;
failed pushes retain the commit. Rerun the same command to retry a pending push,
even when the files are unchanged. SSH-form GitHub remotes use the same HTTPS
credentials without mounting host SSH keys.
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
The entire host agent configuration, unrelated plugins, and MCP connections are
not copied into containers. Both agents get pinned Ponytail and Caveman skills,
concise workspace instructions, and RTK. Codex runs without a nested OS sandbox;
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
agent/RTK/Bun/uv versions are pinned; Debian packages resolve at explicit image
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

`development.nix` installs `dev`, links all six pinned Ponytail skills for host
Codex and Claude, and narrows Docker's existing weekly pruning to dangling images.
Stopped containers, tagged images, volumes, and build caches are preserved.

```bash
nix build /etc/nixos#dev --out-link ~/.local/state/dev-package
nix build /etc/nixos#checks.x86_64-linux.development --no-link -L
```

The initial targeted rollout uses `~/.local/bin/dev` linked to that GC-rooted
package, plus only this module's tmpfiles and Docker-prune drop-in. This avoids
activating unrelated pending server changes. The normal next NixOS rebuild
manages the same settings system-wide. After editing launcher/image/skills,
rebuild the package above before running `dev rebuild` for existing projects.
`DEV_ROOT` and `DEV_STATE_ROOT` can relocate a new installation; existing project
metadata rejects accidental relocation. `DEV_GITHUB_OWNER` changes the default
account for new projects and must match the authenticated GitHub identity.
