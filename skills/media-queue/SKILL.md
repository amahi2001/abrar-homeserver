---
name: media-queue
description: Search configured Prowlarr indexers, apply the owner's movie, anime, language, source, and quality preferences, present numbered release choices, queue only user-selected numbers in Transmission through the server's VPN-isolated media pipeline, and report download-state changes. Use by default when a user asks to find, download, queue, or check the status of a movie, TV episode, season, anime, or other media release; do not use for an obviously non-media download.
---

# Media Queue

Use `sudo media-queue` for every search, queue, and status operation. It is
the only supported automation path: it checks VPN isolation before queueing and
saves completed items to the manual media library.

## Default selection policy

Use this skill by default for a clear request to download, find, or queue
media. Do not require the user to name the skill. Skip it only when the desired
download is obviously not media, such as software, source code, or a dataset.

Apply these preferences before presenting choices. Preserve the CLI's original
result numbers even when presenting preferred results first; never renumber a
result or queue anything until the user replies with its number(s).

- Exclude CAM, CAMRip, HDCAM, and telesync releases. Do not offer sub-1080p
  releases unless the user explicitly asks for one.
- Prefer 2160p/4K when the listed size is below 25 GiB. Otherwise prefer
  1440p/2K, then 1080p. Do not recommend a 4K release at or above 25 GiB
  unless the user asks for that size or no acceptable 1440p/1080p result exists.
- For films, rank YTS results first among otherwise comparable qualifying
  releases. Use other configured indexers when YTS has no suitable match.
- For anime, rank Nyaa results first. If the user did not specify subtitles or
  a dub, ask exactly once which they want before searching. English dub is the
  usual preference, but do not silently assume it when they omitted the choice.
- For other media, compare all configured indexers and rank qualifying results
  by the quality policy above, then seeders and size.

## On-demand library managers

Sonarr and Radarr are intentionally stopped while idle. Keep one-off searches
and downloads on `media-queue`; do not start either manager for those requests.

When the user explicitly asks to track, manage, organize, or maintain existing
files in the media library, start only the matching manager:

```bash
sudo media-library start sonarr  # TV shows and anime
sudo media-library start radarr  # films
```

Use Sonarr/Radarr only for library-management work. Do not use either as an
alternate torrent path or bypass `media-queue`'s numbered selection and VPN
guardrails. To organize a completed show/anime folder or a completed film,
use `media-library`, not hand-written Arr API requests. It adds any new record
unmonitored, never searches for downloads, imports in copy mode, and verifies
that every result is a hard link before reporting success.

1. Start the manager and dry-run the exact title match. A movie must include a
   year; a TV/anime title should include a year if its title is ambiguous.

   ```bash
   sudo media-library start sonarr
   sudo media-library organize-tv --path "/srv/data/media/library/manual/<folder>" --title "<exact title>" [--year YYYY] --dry-run

   sudo media-library start radarr
   sudo media-library organize-movie --path "/srv/data/media/library/manual/<folder>" --title "<exact title>" --year YYYY --dry-run
   ```

2. State the exact identified title/year and planned hard-link import. If the
   command says the match is ambiguous, ask the user which candidate is
   correct. After the user approves the plan, execute the same command with
   `--confirm` instead of `--dry-run`. Do not infer a title/year from a weak
   filename match.

3. Report the verified result, then stop every manager started for this task:

```bash
sudo media-library stop sonarr
sudo media-library stop radarr
```

## Workflow

1. Apply the default selection policy, then search before selecting. Pass the user's query literally to Prowlarr, so
   normal release terms such as `S03E01`, `E01`, `WEBRip`, `WEB-DL`, `1080p`,
   `x265`, or `English Dub` work as part of the query:

   ```bash
   sudo media-queue search "<title>" --limit 10
   ```

   Present title, source, size, seeders, and result number. Never print or
   paste Prowlarr download URLs: they contain an internal credential.
   Present the preferred qualifying releases first, with their original number,
   title, source, resolution, size, seeders, and leechers. Do not queue from a
   search request alone, and do not choose a release on the user's behalf. Ask
   the user to reply with one or more result numbers.
2. Queue only the number(s) the user explicitly selected from the immediately
   preceding search:

   ```bash
   sudo media-queue queue --query "<title and release terms>" --index <number> [<number> ...] --confirm
   ```

   The CLI resolves either a torrent descriptor or a magnet redirect entirely
   through the Gluetun namespace, then adds it to Transmission at
   `/srv/data/media/library/manual` after completion.
3. After queueing, tell the user the download is being watched. A persistent
   five-minute Telegram watcher reports a changed terminal/problem state:
   completed, paused (including with no connected peers), waiting for peers,
   removed, failed, or resumed. It is
   quiet while a download continues normally. Report current status on demand:

   ```bash
   sudo media-queue status
   ```

## Network boundaries

Never bypass or alter these controls while fulfilling a media request:

- Do not call `transmission-remote`, Docker, or Prowlarr download URLs directly.
  Use `media-queue` instead.
- Do not disable Gluetun, change its firewall, move Transmission outside its
  network namespace, or publish Transmission's RPC port.
- Do not reveal Prowlarr API keys or its generated download URLs.
- If `media-queue health` fails, report the failed guardrail and stop; do not
  fall back to a host-network download.

## Commands

```bash
sudo media-queue health
sudo media-queue search "<title>" [--limit 10]
sudo media-queue queue --query "<title and release terms>" --index <number> [<number> ...] --confirm [--dry-run]
sudo media-queue status
sudo media-queue watch
```

Use `--dry-run` to validate a specific result and all network guardrails without
adding it. The implementation is in `scripts/media_queue.py`.
