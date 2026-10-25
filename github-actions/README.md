# Personal GitHub Actions runners

Ubuntu 24.04 x64 runners serve the private personal repositories listed in
`repositories.nix`: TokenEye, prompt-goat, and prompt-optimizer. Each has a
`github-actions-REPOSITORY.service`, its own registration, and a separate persistent
workspace. Services start at boot and restart after failure.

The entire fleet shares **four CPUs and 8 GiB RAM**, plus at most 2 GiB swap,
through `githubactions.slice`. Limits apply to the Docker containers as well as
their jobs. Idle listeners use a small amount of memory; CPU capacity and memory
are not reserved in advance. Concurrent jobs share this budget and may exhaust
it if several memory-heavy builds run together.

Containers have no host Docker socket, personal home directory, server data,
or registration PAT mounted into them. Docker/container-action workflows require
a separate runner with its own Docker daemon. These runners support shell and
JavaScript actions. Only private repositories owned by `amahi2001` pass the
startup check.

## Default for existing and new projects

The personal `agent-config` instructions prefer these runners for compatible
Linux jobs. GitHub has no personal-account-wide runner setting: repository-level
runners are dedicated to one repository. New private projects need one registration
before their workflows can use this server:

1. Add the repository name to `repositories.nix`.
2. Validate and deploy the server configuration, then verify its runner is online.
3. Route trusted Linux jobs to the labels below and verify a job on the server.

Repositories without Actions workflows do not need idle runner services. Private
projects keep their existing owners and URLs. Public projects retain free standard
GitHub-hosted runners; macOS, Windows, and untrusted fork PR jobs also keep their
existing hosted runners. An offline server leaves self-hosted jobs queued instead
of silently spending paid hosted minutes.

## Registration and deployment

The host uses a root-only PAT with administration access to the listed repositories to request a
fresh one-hour registration token on each start. The PAT never enters the image
or container. Bootstrap from an already authenticated administrator's GitHub CLI:

```bash
sudo install -d -m 0700 /var/lib/secrets
umask 077
gh auth token | sudo tee /var/lib/secrets/github-actions-token >/dev/null
sudo chmod 0600 /var/lib/secrets/github-actions-token
nix flake check
sudo nixos-rebuild switch --flake /etc/nixos#buildfleet-server
```

When unrelated host changes are pending, build this module and activate only its
generated unit through `/etc/systemd/system.control`, following `development.nix`.
The unit and boot target link are also declared in `environment.etc`, so normal
NixOS rebuilds keep this override synchronized. Retain the generated unit with a
GC root for each generated unit under `/var/lib/github-actions/units` when activating
without a system switch. Include `githubactions.slice` and the per-service boot
links. This avoids deploying pending application, media, or agent changes.

Use a fine-grained PAT limited to the listed private repositories with Administration
read/write permission when rotating the bootstrap credential. Keep it outside
Git and the Nix store. Rotate by replacing the protected file, then restart the
service while no job is running.

## Send jobs here

Change Linux jobs to:

```yaml
runs-on: [self-hosted, linux, x64, buildfleet-server]
```

For workflows that accept fork PRs, explicitly keep those jobs hosted:

```yaml
runs-on: ${{ github.event.repository.private && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && fromJSON('["self-hosted","linux","x64","buildfleet-server"]') || 'ubuntu-latest' }}
```

For a mixed OS matrix, keep its original OS names and route only Ubuntu:

```yaml
runs-on: ${{ github.event.repository.private && matrix.os == 'ubuntu-24.04' && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && fromJSON('["self-hosted","linux","x64","buildfleet-server"]') || matrix.os }}
```

macOS and Windows jobs still need those platforms. Standard `ubuntu-24.04` jobs
continue using GitHub-hosted minutes until their `runs-on` changes. Use this
runner only for trusted private-repository workflows.

## Operations

```bash
systemctl status github-actions-token-eye
systemctl status github-actions-prompt-goat github-actions-prompt-optimizer
systemctl show githubactions.slice -p CPUQuotaPerSecUSec -p MemoryMax -p MemorySwapMax
journalctl -u github-actions-token-eye -n 50 --no-pager
gh api repos/amahi2001/token-eye/actions/runners
sudo systemctl restart github-actions-token-eye
```

NixOS rebuilds leave active jobs running. Restart manually while idle to apply
runner changes. The official runner updates itself normally; update the pinned
base image tag and digest in `Dockerfile` together, validate, rebuild NixOS, then
restart while idle. Image builds occur once per Dockerfile content hash. Rustup
is installed using Rust's official installer; workflows select their Rust version.

To remove one project, stop its service, remove its name from `repositories.nix`,
rebuild NixOS, remove its two `system.control` links and GC root if installed manually,
and delete its runner in repository Settings → Actions → Runners. Remove its
`/var/lib/github-actions/REPOSITORY` workspace only after confirming that retained
data is no longer needed. The host registration credential is shared; keep it
while other project runners still need it.

References: [GitHub runner images](https://github.com/actions/runner/tree/main/images),
[runner registration](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners),
[workflow routing](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow),
[Rust installation](https://rust-lang.org/tools/install/).
