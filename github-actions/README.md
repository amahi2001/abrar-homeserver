# GitHub Actions runner

`github-actions-token-eye.service` runs one Ubuntu 24.04 x64 runner for the
private `amahi2001/token-eye` repository. It starts at boot and restarts after
failure. Jobs share four CPUs, an 8 GiB memory limit, and a persistent workspace.
The container has no host Docker socket, personal home directory, server data,
or registration PAT mounted into it. Docker/container-action workflows require
a separate runner with its own Docker daemon; this runner supports shell and
JavaScript actions.

## Registration and deployment

The host uses a root-only PAT with repository administration access to request a
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
GC root at `/var/lib/github-actions/unit` when activating it without a system
switch. This avoids deploying pending application, media, or agent changes.

Use a fine-grained PAT limited to this private repository with Administration
read/write permission when rotating the bootstrap credential. Keep it outside
Git and the Nix store. Rotate by replacing the protected file, then restart the
service while no job is running.

## Send jobs here

Change Linux jobs to:

```yaml
runs-on: [self-hosted, linux, x64, buildfleet-server]
```

For a mixed OS matrix, keep its original OS names and route only Ubuntu:

```yaml
runs-on: ${{ matrix.os == 'ubuntu-24.04' && fromJSON('["self-hosted","linux","x64","buildfleet-server"]') || matrix.os }}
```

macOS and Windows jobs still need those platforms. Standard `ubuntu-24.04` jobs
continue using GitHub-hosted minutes until their `runs-on` changes. Use this
runner only for trusted private-repository workflows.

## Operations

```bash
systemctl status github-actions-token-eye
journalctl -u github-actions-token-eye -n 50 --no-pager
gh api repos/amahi2001/token-eye/actions/runners
sudo systemctl restart github-actions-token-eye
```

NixOS rebuilds leave active jobs running. Restart manually while idle to apply
runner changes. The official runner updates itself normally; update the pinned
base image tag and digest in `Dockerfile` together, validate, rebuild NixOS, then
restart while idle. Image builds occur once per Dockerfile content hash. Rustup
is installed using Rust's official installer; workflows select their Rust version.

To remove it, stop the service, remove its import from `configuration.nix`, rebuild
NixOS, remove its two `system.control` links if installed manually, and delete the
runner in repository Settings → Actions → Runners. Remove
`/var/lib/github-actions/token-eye` and its credential only after confirming that
their retained data is no longer needed.

References: [GitHub runner images](https://github.com/actions/runner/tree/main/images),
[runner registration](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners),
[workflow routing](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/use-in-a-workflow),
[Rust installation](https://rust-lang.org/tools/install/).
