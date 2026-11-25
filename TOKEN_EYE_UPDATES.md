# TokenEye main updates

`token-eye-update.timer` checks GitHub's `amahi2001/token-eye` main branch hourly,
with up to five minutes of jitter and catch-up after downtime. It uses no model
turns. The updater lives in this Nix deployment, independently of the application
checkout and running routing service.

The private `runtime/installed.json` records the deployed commit and binary SHA256.
Bootstrap it only from a verified deployment receipt and matching executable.
The updater refuses unknown binary drift and waits until main contains the deployed
commit; it never downgrades the server or resets a development branch. In November
2026 the deployed server work at `85d1f75` still needs to land on main, which is at
`218aeef`. Until then, `waiting_for_main` is expected and routing stays running.

When main advances safely, the updater exports that exact revision into ignored
`build/main-update` beneath the existing checkout. It builds and tests the headless
CLI through the existing dev box, then atomically replaces the shared executable.
All devboxes use that same runtime directory. The updater restarts only the active
routing service, preserves its saved off choice, and verifies both provider routes.
A failed activation restores the previous binary and receipt. A persistent journal
allows interrupted activation to recover on the next run. Busy project/session
locks defer updates; failures leave the previous installation in place.

Application data, agent settings, native hook approvals, dependency pins and the
development branch remain under their existing owners. This updates the headless
binary; changes requiring a new project image or host configuration need their
normal reviewed deployment. The proxy fixture's fixed production port cannot be
used while routing serves clients; headless fixtures, core Rust tests and live
route readiness provide the updater's checks instead.

```sh
systemctl --user list-timers token-eye-update.timer
systemctl --user start token-eye-update.service
journalctl --user -u token-eye-update.service -n 20
cat ~/.local/state/token-eye-nixos/runtime/update/latest.json
```

Stop automatic updates with `systemctl --user disable --now token-eye-update.timer`.
To persist removal across NixOS rebuilds, remove the updater's timer enablement and
named links in `token-eye.nix`. Routing and the separate dogfood timer keep running.

## One-time host Claude home rename

After the old home link is retired, `check` still validates the canonical
receipt. Rolling back to a retired path is rejected.

From the server configuration checkout, run `python3 scripts/token_eye_ca_alias.py check`
as the runtime owner. Save the SHA-256 from the `eligible <sha256>` result.
Eligibility means both home names resolve to the
same host account, the installed CA files match their recorded hashes, and the
two accounts' managed settings already match the new host path.

During an offline routing maintenance window, run the same command with `apply --preimage <sha256>`.
The digest pins the exact original bytes approved for that write and identifies
the rollback snapshot even if the apply process exits before reporting success.
The staged receipt also carries this ID in a Linux user extended attribute.
A subsequent `check` reports it with the current receipt.
It acquires the deployment, devbox, native configuration and proxy owner locks
and checks the box for remaining routing sockets. A busy owner or an unavailable
socket check defers the repair. The helper changes the host client directory
and installed CA path in the ownership receipt. It preserves the original
restore values and prints the SHA-256 of its mode-0600 preimage at
`~/.local/state/token-eye-nixos/runtime/update/host-ca-home-rename.<sha256>.preimage`.
Treat that file as a credential. An intervening edit can leave a snapshot of the earlier receipt. Its
separate filename allows a retry against the changed bytes.
Use `rollback --preimage <sha256>` in the same maintenance window
to restore the exact original bytes while the receipt still matches the repair.
