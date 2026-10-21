# Dev Containers pilot — 2026-10-21

The official Dev Containers CLI reproduced our development environment and frontend
output using the existing image, entrypoint and Compose file. No production boxes
were replaced. The pilot remains available alongside the current launcher.

## Startup measurements

Six samples per implementation and case, alternating order on the same NixOS host.
Times include the complete command and healthy-container confirmation. Both use the
same cached image, configuration snapshot and warmed persistent homes.

| Case | Current `dev` median | Dev Containers median | Current range | Dev Containers range |
|---|---:|---:|---:|---:|
| Reuse running box | 1.435s | 0.475s | 1.421–1.441s | 0.469–0.493s |
| Start stopped box | 2.043s | 1.712s | 2.033–2.061s | 1.702–1.739s |
| Recreate from cached image | 2.056s | 1.923s | 2.046–2.691s | 1.908–1.937s |

Stop/removal time is excluded from both startup measurements. Recreation removes
the test container, then invokes normal startup; it does not run `dev rebuild`,
which also builds an image. No image build, initial CLI installation or first-home
setup is included. The diagnostic `initialReadySeconds` values in the raw data are
not comparable cold-start measurements.

These measure different amounts of host policy: `dev` also checks Git identity,
branch, local credentials and agent locks. Native `up` handles container lifecycle
alone. Its lower overhead does not establish the speed of a complete replacement
for our launcher. Native `up` returns before health readiness; the pilot explicitly
waits for the same healthcheck before recording success.

## Output and behavior comparison

| Check | Current launcher | Dev Containers |
|---|---|---|
| Cached image ID | Same | Same |
| Frontend build | Same SHA256 | Same SHA256 |
| Application tests | 1 passed, 3 assertions | 1 passed, 3 assertions |
| Rendered form flow | `Saved Ada` | `Saved Ada` |
| Frontend screenshot | Identical PNG bytes | Identical PNG bytes |
| Tools, skills, instructions and RTK hooks | Same normalized output | Same normalized output |
| Native Codex/Claude skill discovery | All 14 selected skills | All 14 selected skills |
| Browser snapshots, errors, screenshots, trace, video | Passed | Passed |
| Live dashboard mouse/keyboard | Passed | Passed |
| Linux desktop, xdotool, noVNC keyboard, screenshot | Passed | Passed |
| Repeated start/stop, occupied port, crash recovery | Passed | Passed |
| Published viewer HTTP access from host loopback | Passed | Passed |
| Recreated box: cookies, artifacts, files, fake auth | Preserved | Preserved |
| Learned shared instruction and native skill stamp | Preserved | Preserved |
| UID/GID, isolated mounts, no Docker socket | Passed | Passed |
| Viewer publication | Random loopback ports; no 5900 | Random loopback ports; no 5900 |

Frontend bundle SHA256:
`5e70742084db1e74d3aed2eca5f2e274d3e597e78d0b09ccc75831e32d1baf02`.
Screenshot SHA256 in both boxes:
`f132e8bebf2b8d82d14d08e3c4d6fb9fbf8665cda53bc7dceb468d1c7643d07d`.
One copy of the identical output is saved as [frontend.png](frontend.png).

The image provides Bun 1.4.3, Node 24.22.0, Python 3.11.2, uv 0.13.4, RTK 1.0.1,
Codex 0.163.0, Claude Code 2.1.306 and Playwright CLI 0.1.22. The host pilot uses
official CLI 0.89.0 with its separately bundled Node 24.22.1; no global Node/npm
installation was added.

The current launcher and native CLI both used image
`sha256:8cd2a82cf62bed79f702af696eb77c6f3ca41d8142276bfb08b366a6c6011f4c`.
Full samples, hashes, tool versions, discovered skills and viewer mappings are in
[results.json](results.json). Full logs and each project's screenshots, traces and
recordings remain under `~/.local/state/devcontainer-pilot` on the server.

## What this would replace

Dev Containers can own standardized container configuration, startup, command
execution and discovery. Our image's agent setup, persistent mounts and optional
UI services work unchanged. The native configuration needs no duplicate lifecycle
installation hooks.

Keep our private GitHub defaults, branch preparation, account seeding, config/tool
locks, active-agent protection, browser checkpoints and `dev-finish` policy in a
thin host layer. The standalone native CLI does not provide those policies. The
pilot's environment export is explicit and limited to disposable fixtures; it is
not a production replacement launcher.

Recommendation: use Dev Containers as the lifecycle foundation for a later
migration, while preserving our image and policy. This experiment establishes
compatibility and a modest stopped-box startup gain; the largest measured gain is
reusing an already-running container.

## Validation and limits

Both live comparison runs passed build, rendered-output, UI and recreation checks.
The repeat run also verifies native harness skill discovery and host-published
viewer access. The Nix development check passed 27 tests / 188 assertions. A
concurrent comparison was rejected with exit 73 before mutation. Ponytail review
found no blocking issue.

Existing sandbox and agent-config containers retained their IDs, image IDs,
start times and restart counts. No NixOS activation or production launcher change
was performed. Pilot viewers finish stopped; the two pilot boxes remain running
for inspection through the [documented helpers](README.md).

This uses a small frontend fixture and a Linux terminal with software rendering.
Fake credentials verify file persistence and permissions; real OAuth refresh,
GitHub pushes, model-generated code quality, token use and model latency were not
tested. No model API calls or new remote repositories were required. Human viewer
input was automated locally; a new SSH tunnel was not separately retested here.
