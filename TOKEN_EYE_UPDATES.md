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
