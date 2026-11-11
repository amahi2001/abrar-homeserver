---
name: youtube-queue
description: Search YouTube, download shared YouTube videos or approved playlists directly, and publish them to the dedicated Jellyfin YouTube library. Use for requests to save, download, watch later, retry, or check YouTube videos, and for a bare shared YouTube video link. Movie and TV release searches belong to media-queue.
---

# YouTube Queue

Use `sudo youtube-queue` for this workflow. It runs native yt-dlp as the
unprivileged youtube-download user over the server's normal internet connection,
persists jobs independently of the chat, and verifies Jellyfin indexing.
Default quality is **up to 1080p**. Use `--height 2160` only when the user asks
for 4K; 1440 and 4320 are also supported on explicit request.

Use a stable scope for every search, preview, queue, and status call:
`sudo youtube-queue --scope 'telegram:<current-chat-id>:<thread-id-or-empty>' …`.
For Desktop/CLI use the current Hermes session ID instead. The CLI can inherit
HERMES_SESSION_* context, but an explicit scope survives sudo's environment
filtering. Never invent a shared scope or reuse another conversation's results.

## Receive a request

- A bare/shared video link, or a request to download that link, authorizes
  queueing immediately: `queue --url '<YouTube URL>'`. Links in a request to
  summarize or discuss a video do not authorize a download.
- A text description requesting a YouTube video calls `search '<description>'`.
  Present the returned entries with their original 1-based numbers, title,
  channel and duration. Wait for the user's choice, then call
  `queue --snapshot '<snapshot>' --index <number> [<number> …]`. Do not search
  again to resolve numbers. If the selection is stale or ambiguous, search again.
- A playlist URL calls `preview '<playlist URL>'`. Show its title, video count,
  total known duration and unknown-duration count. Ask for approval of that list,
  then call `queue --snapshot '<snapshot>' --confirm-playlist`. This downloads
  only the saved list; videos added to the playlist later are not included.
- A watch/share URL with a playlist parameter means the single selected video.
  If the user explicitly requests its whole playlist, construct the corresponding
  `https://www.youtube.com/playlist?list=<list-id>` URL and use the preview flow.

Searches and previews download no media. Snapshots expire after 24 hours for
queueing. Public YouTube videos, Shorts and completed live recordings work;
channel subscriptions, ongoing livestreams and authenticated videos do not.

## Report progress

After queueing, report the title/job ID and that the background queue is watching
it. `status` returns queued, downloading, paused, awaiting-index, ready, or failed.
The five-minute no-agent Telegram watcher stays silent during normal progress,
and reports readiness, failures and required action. Do not claim a video is
available in Jellyfin until the status is `ready` with an item ID.

Duplicates reuse the existing job/file and retain its existing quality.
For an explicit failed-job retry: `retry '<YouTube video ID>'`. Paused disk
jobs recover automatically; retries preserve partial downloads. Do not promise
that retry can resolve private videos or YouTube authentication/bot challenges.

Jellyfin's **YouTube** library is published at
`/srv/data/media/library/youtube`. Local NFO metadata and thumbnails provide
video titles and descriptions; movie database matching is disabled. The media
is kept until deletion is requested. Source info JSON and partial files are
not published. Playback uses the existing Jellyfin URLs, including
`https://jellyfin.amahi.dev`.

## Diagnose problems

`sudo youtube-queue health` checks the native downloader and Jellyfin.
`sudo youtube-queue status` shows job blockers. `systemctl status
youtube-queue-worker.service youtube-queue-worker.timer --no-pager` and its
journal show worker health. After provisioning the root-only API credential,
`sudo youtube-queue setup` creates the library idempotently.

Jellyfin can skip registering an empty library's filesystem watch. If an item
remains unindexed after a minute, the worker requests one library validation
pass and keeps checking visibility. Do not repeatedly force scans while it waits.

Use the queue CLI rather than raw yt-dlp, Transmission or Sonarr/Radarr for
these jobs. Do not expose the staging directory,
print the Jellyfin API credential, or overwrite an existing published video.
Keep YouTube requests routed here. Movie/TV downloads stay on media-queue with
Gluetun, VPN checks and Transmission isolation intact.
