---
name: media-subtitles
description: "Use when adding subtitles to media. Match and verify."
version: 1.0.1
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [media, subtitles, captions, jellyfin]
    related_skills: [media-queue, media-review-followup]
---

# Media subtitles

## When to Use
Use when adding, downloading, or verifying subtitle/caption files for media already in a library. Use `media-queue` when the media itself needs downloading.

Default to English when language is not specified. Tell the user whether files are ordinary subtitles or SDH/closed-caption style.

## Procedure

1. Resolve the exact title, season, language, and requested episode range against the live library and queue. Enumerate actual episode filenames; do not infer media identity from a tentative recollection. Check for existing external subtitle sidecars and embedded subtitle streams before fetching. The buildfleet-server gateway PATH already provides `ffprobe`; use it to inspect tracks. If it is missing, check the gateway service PATH rather than installing a package just to inspect tracks.
2. Source subtitles by exact title, season/episode, and preferably matching source/release (for example, Amazon WEB-DL versus HDTV). Prefer episode-level subtitles matching the user's video release over generic season bundles. If only a different release is available, state that sync may differ; don't promise playback correctness without testing.
3. If a public subtitle site requires cookies, use a real browser session to load its page and establish the ordinary cookie session, then retrieve the site's offered subtitle file. Do not bypass login, CAPTCHA, or other access controls. Avoid substituting a similarly named result without checking its episode/title content.
4. Validate archives before extraction with Python's standard-library `zipfile`: confirm ZIP integrity, expected episode set, `.srt` members, no unsafe paths, and no unexplained duplicates. Select one subtitle per episode, favoring the matching release; never blindly extract every archive member into the library. Validate cue/timestamp structure and cue-number uniqueness; if numbering is duplicated or out of order, renumber cues sequentially without changing cue text or timestamps, then revalidate. Decode UTF-8 first; when a valid SRT uses a legacy encoding such as Windows-1252, normalize text to UTF-8.
5. Install external subtitles beside the organized video files, not in the Transmission source directory. Name each sidecar from the exact video basename plus its language suffix (for example, `Episode.mkv` → `Episode.en.srt`). For an additional, clearly labeled alternative, use Jellyfin's title field after the language (for example, `Episode.en.Auto-aligned.srt`) so it remains distinct from the existing track and is identifiable in the player. Do not misuse `forced`, `sdh`, or other semantic flags as labels. Preserve videos, seed data, and original subtitles; refuse target-name collisions rather than overwrite. Use readable permissions for the media service, typically `0644`.
6. Never remount `/` or `/run` for subtitle sidecars. On buildfleet-server, the gateway uses `ProtectSystem=strict`; `/srv/data/media/library/movies` and `/srv/data/media/library/tv` are writable through declarative `ReadWritePaths`. `sudo` handles directory permissions but does not override this mount namespace sandbox. Before and after installation, run `findmnt -M / -no VFS-OPTIONS` and `sudo findmnt -T <destination> -no VFS-OPTIONS` inside the gateway: root must remain `ro`, and the media destination must be `rw`. If a write still fails with `EROFS` or "Read-only file system", stop writing and inspect/correct the declarative allowlist in `/etc/nixos/hermes.nix`; do not weaken root protection, remount filesystems, or kill processes.
7. Read back the destination: confirm one nonempty sidecar per requested episode, correct episode range, expected permissions, and matching video basenames. Report the source/release match or caveat. Say Jellyfin can pick up the sidecars, but claim they are recognized/playable only after checking Jellyfin or playback.

## Diagnose reported sync problems

1. Preserve current sidecars while investigating. Compare the subtitle's exact release/source metadata with the video filename or media metadata; do not infer that an HDTV caption is the right match for a WEB-DL video just because show, season, and episode agree.
2. Check multiple episodes, not only the first, and inspect timing/cue coverage across each episode (early, middle, and late). A consistent time offset suggests a constant delay; a steadily changing difference suggests frame-rate or runtime drift; unrelated or erratic differences suggest wrong source, episode mapping, or unreliable measurements.
3. Treat audio-based auto-sync output as a diagnostic candidate, not a verified repair. Write candidates to scratch, preserve originals, and verify each generated file exists, is nonempty, parses as valid SRT, and has plausible cue coverage before interpreting tool output; a successful exit code or printed estimate alone does not prove a usable result. Do not infer frame-rate drift from one episode or one estimate. Compare early, middle, and late dialogue in the actual media, and check whether correction is a constant offset, linear drift, or neither. Do not bulk-replace a season when per-episode estimates conflict or lack independent confirmation; ensure transformed cues remain within video duration before installing.
4. Install a corrected replacement only after the match or timing transform is verified. If the user explicitly wants an unverified timing candidate as an extra choice, install it only as a separately labeled alternate while preserving originals; validate SRT structure, cue coverage, and cue bounds against video duration first. Read back installed sidecars and test playback before claiming the timing is fixed; describe untested alternates as candidates.

## Pitfalls

- Check embedded tracks before downloading: a file may already contain usable captions even when no `.srt` sidecar exists.
- A season ZIP can contain duplicate episode variants and omit episodes; validate episode numbers and map each caption to the actual video before writing.
- Different encodes can have timing offsets; matching episode labels alone does not prove sync. Automated alignment scores or one successful episode are not enough to justify season-wide replacement.
- Keep subtitle files alongside organized Jellyfin media, not only in the manual/download folder, because Jellyfin indexes organized libraries.
