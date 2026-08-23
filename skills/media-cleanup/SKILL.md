---
name: media-cleanup
description: Inspect and safely remove Transmission jobs, permanently delete selected media, stop seeding while retaining organized Jellyfin media, or destroy legacy quarantine bundles. Use when the user asks to delete, remove, unseed, archive, clean up, or free space from downloaded movies, TV, anime, or torrents.
---

# Media Cleanup

Use `sudo media-cleanup` for every cleanup operation. Never delete managed
media directly in Transmission, Sonarr, Radarr, Jellyfin, or the filesystem.
The CLI constrains paths to this server's media roots and separates planning
from execution.

## Choose the operation

- `delete` (default): permanently remove the Transmission entry, its manual
  payload, and corresponding organized library links. This reclaims space only
  after the user confirms the generated plan token; it cannot be recovered.
- `remove-job`: stop and remove the Transmission entry. Leave every payload
  file on disk. This does not free space or remove anything from Jellyfin.
- `archive`: stop seeding and remove the manual Transmission payload only
  after every regular payload file is verified as a hard link in the organized
  Jellyfin library. Keep the organized TV/movie files. This usually frees
  negligible space because hard links already share the same bytes.
Use `delete` by default when the user asks to delete or clean up media. Keep
the plan-and-token confirmation mandatory. Archive and delete are blocked for
incomplete downloads.

## Two-phase workflow

1. Inspect the live queue:

   ```bash
   sudo media-cleanup list
   ```

   Present the displayed Transmission IDs, titles, state, progress, size,
   ratio, and published-file count. Ask the user to choose an ID; use the
   permanent-delete default unless they explicitly choose another mode.

2. Create a read-only plan for the chosen ID. Omit `--mode` for the default
   permanent deletion:

   ```bash
   sudo media-cleanup plan <id> [--mode <delete|remove-job|archive>]
   ```

   Report the exact effect and affected paths. Do not execute yet. Ask a
   simple `Proceed? (yes/no)` question; retain the CLI token internally. A
   previous generic request to delete or clean up is not confirmation of this
   new plan.

3. On `yes`, apply the saved plan using its internal token; on `no`, cancel it:

   ```bash
   sudo media-cleanup apply <token> --confirm
   ```

   Tokens remain an internal plan-binding safeguard, expire after 30 minutes,
   and are invalidated if the torrent or files
   change. Report the CLI's result. The CLI starts only the required Sonarr or
   Radarr service for reconciliation and stops it again afterward.

   Cancel an abandoned plan with `sudo media-cleanup cancel`.

## Paths without a Transmission job

For an exact library, manual, or incomplete path that has no active
Transmission job, create the same guarded permanent-deletion plan with:

```bash
sudo media-cleanup plan-path <absolute-path> [<absolute-path> ...]
```

The CLI accepts only non-symlinked paths beneath its managed roots, verifies
the snapshot before deletion, reconciles affected Sonarr/Radarr records, and
uses the same yes/no confirmation gate. Never substitute raw filesystem or Arr
operations for this command.

## Legacy quarantine deletion

List quarantine bundles created by an earlier cleanup policy:

```bash
sudo media-cleanup trash
```

Permanent deletion is a separate two-phase operation. Create a plan using the
exact bundle name, show every path, and wait for explicit confirmation of the
new token:

```bash
sudo media-cleanup plan-destroy <bundle-name>
sudo media-cleanup apply <token> --confirm
```

Never destroy a legacy quarantine automatically, on a timer, or based only on
age. If recovery is requested, stop and inspect the bundle manifest; do not
improvise filesystem moves.

## Guardrails

- Do not use Transmission's “delete local data” action for organized media.
- Do not run raw `rm`, `find -delete`, Arr deletion APIs, or
  `transmission-remote --remove-and-delete` as a substitute for this CLI.
- Do not cleanup a different ID because the selected torrent disappeared.
- Do not bypass hard-link verification when archive is rejected.
- Do not disable the Gluetun/Transmission network isolation while cleaning.
- Do not promise reclaimed capacity from archive; only a confirmed permanent
  deletion can release the final hard link.

The deterministic implementation is bundled at `scripts/media_cleanup.py`.
