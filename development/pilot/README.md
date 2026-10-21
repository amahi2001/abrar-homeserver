# Side-by-side Dev Containers pilot

This compares the current `dev` launcher with the official Dev Containers CLI using
the same cached image, Compose service and agent-config snapshot. Each arm gets a
separate fixture checkout, persistent home, account directories, network and random
loopback ports under `~/.local/state/devcontainer-pilot`. Existing projects and
production containers are inspected only. Fake credentials prevent real agent
authentication or GitHub operations; this does not measure model quality or cost.

## Install the isolated CLI

Use the [official installer](https://github.com/devcontainers/cli#install-script),
pinned to CLI **0.89.0** with its own Node 24 runtime. No global Node/npm install is
needed. This NixOS host already provides `nix-ld` for the bundled runtime.

```bash
pilot="$HOME/.local/state/devcontainer-pilot"
mkdir -p "$pilot"
rtk proxy curl -fsSL https://raw.githubusercontent.com/devcontainers/cli/main/scripts/install.sh -o "$pilot/install-cli.sh"
rtk proxy sh "$pilot/install-cli.sh" --version 0.89.0 --node-version 24 --prefix "$HOME/.local/share/devcontainer-pilot/cli"
rtk proxy "$HOME/.local/share/devcontainer-pilot/cli/bin/devcontainer" --version
```

The installer supports `--update` and `--uninstall`; keep the same `--prefix` and
version when updating this recorded pilot. The CLI installation lives outside both
project homes and can be removed independently.

## Run the comparison

From this repository, build/check the cached image tag with the current launcher
assets, then set `PILOT_IMAGE` to the tag it prints. The recorded tag was
`buildfleet-dev:1c28bf09b4aaded9`; after asset or tool-lock changes, use the new tag.

```bash
pilot="$HOME/.local/state/devcontainer-pilot"
rtk proxy bun development/dev.ts build
export PILOT_IMAGE=buildfleet-dev:1c28bf09b4aaded9
rtk proxy bun development/pilot/compare.ts
```

The host script orchestrates containers; application builds, tests, browser and
desktop checks execute inside the boxes. It asserts equal image IDs, frontend
hashes and normalized tool/config outputs, then checks restart/recreation,
persistence, isolation and loopback publication. Timing alternates implementation
order, with six samples each for reuse, restart and recreation. Results and logs
remain in `$pilot`; measured findings are in [comparison.md](comparison.md).
Re-running replaces only pilot fixture files and recreates only pilot containers.
Before timing, it stops both pilot viewers and closes their browser sessions, then
resets only the disposable pilot browser profiles/state. This makes the isolation
check repeatable; later recreation checks prove that newly saved cookies survive.
Project files, agent config, saved artifacts and production profiles are preserved.
Both viewers are stopped again when the comparison finishes; start them on demand.

Use the generated shell helpers for either implementation:

```bash
rtk proxy "$pilot/current-exec" bash
rtk proxy "$pilot/standard-exec" bash
rtk proxy "$pilot/standard-exec" bun run build
rtk proxy "$pilot/current-exec" bun test
```

## Native CLI commands

`devcontainer.json` references the existing Compose file. `remoteUser: node` runs
commands without root; `overrideCommand: false` preserves the image entrypoint,
which owns UID/GID mapping and setup. UID rewriting is disabled to avoid another
image build. There are no duplicate setup hooks. [Property reference](https://github.com/devcontainers/spec/blob/main/docs/specs/devcontainerjson-reference.md).

```bash
source "$pilot/standard.env"
native="$HOME/.local/share/devcontainer-pilot/cli/bin/devcontainer"
native_args=(--workspace-folder "$DEV_SOURCE" --config "$DEV_ASSETS/pilot/devcontainer.json" --id-label devcontainers.pilot=standard --user-data-folder "$pilot/standard/cli-state")
rtk proxy "$native" read-configuration "${native_args[@]}"
rtk proxy "$native" up "${native_args[@]}"
rtk proxy "$native" exec "${native_args[@]}" bun test
```

Keep the exported environment, unique Compose project, ID label and CLI state
directory together. Native `up` does not wait for the Compose healthcheck; the
comparison script waits before measuring success. To recreate only this pilot,
checkpoint its browser first, then add `--remove-existing-container` to `up`:

```bash
rtk proxy "$pilot/standard-exec" dev-ui _checkpoint
rtk proxy "$native" up "${native_args[@]}" --remove-existing-container
```

The native CLI does not supply our active-agent lock, GitHub provisioning or
browser checkpoint policy. Use this pilot only for the disposable fixture.

## Viewers and stopping

Start viewers through either helper, then discover the actual loopback mappings:

```bash
rtk proxy "$pilot/standard-exec" dev-ui start browser
rtk proxy "$pilot/standard-exec" dev-ui start desktop
pilot_id="$(docker ps -q --filter label=devcontainers.pilot=standard)"
rtk proxy docker port "$pilot_id" 8931/tcp
rtk proxy docker port "$pilot_id" 6080/tcp
```

For each printed `127.0.0.1:PORT`, run `ssh -N -L PORT:127.0.0.1:PORT
buildfleet@buildfleet-server` from your computer. Open `http://127.0.0.1:PORT/` for
the browser or `/vnc.html?autoconnect=1&resize=remote` for desktop. VNC port 5900
stays internal. Substitute your SSH destination as needed.

Stop the two exact pilot containers; saved files remain. CLI 0.89.0 has no
supported `stop` or `down` command.

```bash
for arm in current standard; do
  ( source "$pilot/$arm.env"
    pilot_id="$(docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" --filter label=com.docker.compose.service=dev)"
    if [ -n "$pilot_id" ]; then
      rtk proxy "$pilot/$arm-exec" dev-ui _checkpoint
      rtk proxy docker stop "$pilot_id"
    fi )
done
```
