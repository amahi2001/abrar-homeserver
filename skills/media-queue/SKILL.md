---
name: media-queue
description: Search configured Prowlarr indexers, apply the owner's release preferences, queue only user-selected results through VPN-isolated Transmission, organize approved media with Sonarr or Radarr, publish organized libraries through Jellyfin, and diagnose download or playback failures. Use by default when a user asks to find, download, queue, organize, watch, stream, or check the status of a movie, TV episode, season, anime, or other media release; do not use for an obviously non-media download.
---

# Media Queue

Use `sudo media-queue` for every search, queue, and status operation. It is
the only supported automation path: it checks VPN isolation before queueing and
saves completed items to the manual media library.

Keep the media flow explicit:

1. Transmission downloads through Gluetun into
   `/srv/data/media/library/manual`.
2. The systemd `media-organizer` timer discovers every completed torrent,
   including downloads added manually in Transmission. It starts Sonarr for
   TV/anime or Radarr for films, hard-links the media into `tv` or `movies`,
   then stops the manager again.
3. Jellyfin publishes only the organized `tv` and `movies` libraries. A file
   that exists only in `manual` is downloaded but not yet available in
   Jellyfin.

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
and downloads on `media-queue`; do not start either manager immediately after
queueing. The independent `media-organizer` timer starts the matching manager
only after Transmission reports completion, imports the media, and stops it.

Check the automatic pipeline with:

```bash
sudo media-organizer status
systemctl status media-organizer.timer --no-pager
```

Use the manual flow only when automatic classification reports `needs-review`,
or when the user explicitly asks to replace, track, or repair existing library
content. Start only the matching manager:

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

For a user-confirmed replacement of a corrupt existing episode/movie, use the
same dry-run and confirmation sequence with `--replace-existing`. Never let the
automatic timer force a quality downgrade or overwrite by itself.

## Jellyfin playback and publishing

Use Jellyfin as the preferred playback layer, especially on the NVIDIA Shield;
do not default to VLC over SMB when the user asks how to watch organized media.
Jellyfin is always on and reads these paths:

- TV and anime: `/srv/data/media/library/tv`
- Films: `/srv/data/media/library/movies`

Use `http://192.168.1.89:8096` on trusted home Wi-Fi and
`http://buildfleet-server.tailcb7cdb.ts.net:8096` over Tailscale. Port 8096 is
restricted to those two interfaces and is not public. Jellyfin runs on the host
network; it must never be moved into or routed through Gluetun.

After an approved Sonarr/Radarr import, tell the user the item is published to
the corresponding Jellyfin library. If it does not appear, check that the hard
link exists under the organized path and let Jellyfin's library monitor detect
it before considering a manual scan. Do not expose the `manual` or incomplete
download directories to Jellyfin as a shortcut.

For a Jellyfin player error, inspect evidence before changing settings:

```bash
systemctl is-active jellyfin
curl --fail --silent http://127.0.0.1:8096/health
sudo journalctl -u jellyfin --since "30 minutes ago" --no-pager
sudo ls -1t /var/lib/jellyfin/log/FFmpeg.Transcode-*.log | head
```

ASS/SSA subtitles may require burn-in and therefore a GPU transcode even when
the video codec otherwise supports Direct Play. If an FFmpeg log reports
`CUDA_ERROR_NO_DEVICE`, inspect `systemctl show jellyfin -p DeviceAllow`. The
declarative service must allow the DRM render node plus `/dev/nvidia0`,
`/dev/nvidiactl`, and `/dev/nvidia-uvm`. Fix this only in `/etc/nixos/files.nix`,
validate with `nixos-rebuild dry-build`, then apply with `nixos-rebuild switch`.
Do not work around it by disabling the sandbox or hardware acceleration.

Encoding policy is declarative because `forceEncodingConfig = true`; dashboard
changes to transcoding settings are overwritten on service restart. Preserve
the two-stream limit, NVDEC/NVENC acceleration, and throttled transcoding unless
the user explicitly asks to change that policy.

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
3. After queueing, tell the user the download and automatic organization are
   being watched. A persistent five-minute Telegram watcher discovers both
   tool-queued and manually added Transmission downloads, and reports a changed
   terminal/problem state:
   completed, paused (including with no connected peers), waiting for peers,
   removed, failed, resumed, organized for Jellyfin, or requiring organization
   review. It is quiet while a download continues normally. Report current
   status on demand:

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
sudo media-organizer status
```

Use `--dry-run` to validate a specific result and all network guardrails without
adding it. The implementation is in `scripts/media_queue.py`.
