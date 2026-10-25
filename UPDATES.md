# Automatic maintenance

Native services follow the supported NixOS stable branch: nginx, Vaultwarden,
Nextcloud/PHP/Redis/PostgreSQL, Jellyfin, Servarr, Samba, SSH, Tailscale, Docker,
desktop services, and their dependencies. Security overrides are minimum versions;
newer nixpkgs packages take precedence. NixOS release-branch upgrades, Nextcloud,
and database major-version migrations remain deliberate administrative operations.

The obsolete unmanaged Hermes WebUI on host port 8787 and its dead Tailscale
Serve endpoint on 8443 have been retired. The managed Hermes dashboard uses 9119.

Temporary Nix runtime replacements supply patched Node.js, Brotli, c-ares,
OpenSSL, and SQLite where stable nixpkgs is behind. Library replacements check exported
symbols, SONAME compatibility, and actual APIs before deployment. They retire
automatically when nixpkgs reaches the minimum versions.
The current floors are Node.js 22.23.4, 24.22.1 and 26.12.1 from the
[October security release](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases),
and [Bun 1.4.4](https://bun.sh/blog/bun-v1.4.4).
Hermes' locked cryptography wheel also has a compatible package override for
50.0.2 with OpenSSL 4.0.3, checked with AES-GCM, X.509, and a local TLS handshake.
Its Brotli Python binding is similarly patched to brotlicffi 1.2.1.0, with the
loaded native decoder version and compression round trip checked directly.

| Component | Schedule, Eastern time | Validation and recovery |
| --- | --- | --- |
| System and flake inputs | Sunday 03:00–03:15 | Full flake tests and system build; service, DNS and application health; generation and application-state recovery |
| Codex CLI | Daily 04:30–04:45 | Same build and health gates; idle remote-server refresh every five minutes |
| Hermes | Sunday 05:00–05:15 | Latest stable release, idle gateway gate, package/runtime version and backend health |
| Hermes MCP dependencies | Tuesday 05:00–05:15 | npm/pip and installed Google API/OAuth updates, Python refresh, browser/MCP smoke checks, directory rollback |
| AdGuard Home | Wednesday 05:00–05:15 | Image pull, config snapshot, HTTP and DNS checks, image/config rollback |
| Gluetun and Transmission | Wednesday 05:30–05:45 | Coordinated image/config updates, healthy tunnel, shared namespace, firewall and authenticated RPC checks |
| GitHub Actions runners | Hourly retry; package rebuild at least weekly | Official stable vendor image, Ubuntu package updates, Node 24 checks, idle replacement, registration health and image rollback |
| Development boxes | Sunday 07:00 and hourly retry | Refreshed Node 24/Bun/Codex/Claude/uv pins, package rebuild, runtime/tool checks, existing agent/project locks, image/config rollback |

Warden updates use Node 22/npm 10 with strict engine checks. Its launcher follows
the installed manifest, keeping older releases on Node 24 and restoring that
runtime choice on rollback. Browser tooling continues to use Node 24.

Maintenance does not advance user skills/plugin locks or modify project source.
Stopped dev projects and on-demand Sonarr/Radarr stay stopped. Docker upgrades
quiesce idle CI/dev workloads and restore their prior running set; active or unknown
work defers deployment. Live restore provides additional protection during daemon
replacement, but does not replace these checks.
Nix builds have low I/O priority and bounded SSD operations to reduce contention
with live services during cold downloads and builds.

## Source and state ownership

`/etc/nixos` remains the editable configuration. Each successful system generation
exposes its immutable flake source at `/etc/nixos-deployed`. Automatic maintenance
copies that deployed source into `/var/lib/nixos-updates`, updates its pins, builds
and tests it, and deploys only that candidate. Uncommitted work in `/etc/nixos`
cannot block maintenance or become an accidental deployment.

After success, changed source pins are copied back only when the corresponding
editable file still matches the deployed baseline. Concurrent administrative edits
win; a warning identifies pins that need reconciliation before a manual rebuild.
Automatic maintenance does not commit or push unrelated administrator work.

Application snapshots quiesce writers, preserve ownership and permissions, and
include a PostgreSQL dump for Nextcloud. Uploaded files, media downloads, and
excluded dependency caches are not deleted during state recovery. Failed updates
retain their candidate, logs, and recovery state. These local snapshots protect an
update transaction; they are not an off-machine disaster-recovery backup.
Successful transactions older than 30 days are pruned, keeping at least the newest
three; failed transactions remain available for inspection.

## Inspection and recovery

```sh
systemctl list-timers --all
systemctl --failed
system-update --check
sudo systemctl start nixos-flake-update.service
sudo journalctl -u nixos-flake-update.service -n 100
sudo cat /var/lib/nixos-updates/latest.json
```

Failed jobs activate `maintenance-alert@JOB.service` and leave a dated record under
`/var/lib/nixos-updates/failures`. Review the failed job's journal and retained
transaction before attempting a manual restore. Do not restore databases while
their applications are writing.

Reboots are not initiated during service rollout. A kernel change leaves a
`reboot-required` marker in the maintenance state directory; reboot during an idle
maintenance window after checking service health.

## External limits

- Editor-owned SSH servers update when an updated VS Code/Windsurf/Devin desktop
  client reconnects. This server cannot update that workstation's editor.
- The project-owned TokenEye Headroom proxy follows its dev-box base updates,
  but Headroom and its private Python version are pinned in the project's
  `.dev/tools.lock.json`. Updating those dependencies requires the project's
  tests and release process. Its user service retries across box maintenance.
- GitHub's current runner retains a legacy Node 20 compatibility payload. Managed
  action execution and internal tools are explicitly configured to use patched
  Node 24; vendor files are not modified.
- CloakBrowser's upstream Nix package currently contains Chromium 146. Newer
  vendor binaries require a license; no license is available here. Dependency
  updates preserve the existing stealth-browser behavior and cannot certify that
  old browser as current.
- Bitwarden CLI prefers the newer package from the already-locked Codex nixpkgs
  input when stable is behind (currently 2026.9.1 versus stable's 2026.4.2).
  Warden's supported `BW_BIN` selects that Nix command; Warden 0.2.60 separately
  bundles CLI 2026.10.0. Both graphs retain dependencies flagged by npm audit
  (`tmp`, `node-forge`, and `multer`); selecting `BW_BIN` does not remove that
  risk. No existing locked Nix input provides 2026.10.0, and the latest vendor
  release still has affected dependencies. Upstream fixes remain necessary.
