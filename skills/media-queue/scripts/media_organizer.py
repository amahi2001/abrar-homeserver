#!/usr/bin/env python3
"""Organize completed Transmission media with on-demand Sonarr or Radarr."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath


class OrganizerError(RuntimeError):
    pass


STATE_FILE = Path("/var/lib/media-queue/organizer-state.json")
EVENT_DIR = Path("/var/lib/media-queue/events")
REVIEW_NOTICE_VERSION = 1
REVIEW_REMINDER_SECONDS = 24 * 60 * 60
MANUAL_ROOT = Path("/srv/data/media/library/manual")
LIBRARY_ROOTS = (
    Path("/srv/data/media/library/tv"),
    Path("/srv/data/media/library/movies"),
)
CONTAINER_DOWNLOAD_ROOT = PurePosixPath("/downloads")
MEDIA_SUFFIXES = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".webm", ".wmv",
}
TRANSMISSION = [
    "docker", "exec", "transmission-vpn", "sh", "-ceu",
    'export TR_AUTH="$USER:$PASS"; exec transmission-remote 127.0.0.1:9091 --authenv "$@"',
    "--",
]


def command(args):
    completed = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise OrganizerError(detail)
    return completed.stdout.strip()


def save_private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("items", {})
    return state


def emit_event(key, message):
    EVENT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "", key)[:48] or "media"
    path = EVENT_DIR / f"{time.time_ns()}-{safe_key}.json"
    save_private_json(path, {"message": message})


def record_state(state, key, torrent, status, detail, *, notify=None, source=None):
    previous = state["items"].get(key, {})
    changed = previous.get("status") != status or previous.get("detail") != detail
    state["items"][key] = {
        **previous,
        "detail": detail,
        "name": torrent.get("name", "unknown media"),
        "status": status,
        "torrentId": torrent.get("id"),
        "updatedAt": int(time.time()),
    }
    if source is not None:
        state["items"][key]["sourcePath"] = str(source)
    if changed and notify:
        emit_event(key, notify)
    return changed


def review_message(key, item):
    detail = item.get("detail", "Unknown import blocker.")
    lower = detail.casefold()
    if any(marker in lower for marker in ("cannot safely infer", "exactly one", "runtime", "does not match", "do not all match")):
        question = "Which exact title and release year should be used (and is this TV or a movie)?"
    elif "not an upgrade for existing" in lower or "same quality already exists" in lower:
        question = (
            "Should the existing library copy be kept, or should Hermes inspect the rejected files "
            "and propose an explicit replacement for your approval?"
        )
    elif "reject" in lower or "no accepted media" in lower:
        question = (
            "May Hermes inspect the rejected files and explain what needs correcting "
            "before showing a dry-run plan for your approval?"
        )
    else:
        question = "May Hermes investigate this blocker and show an import dry-run plan for your approval?"
    return (
        f"Media import needs review: {item.get('name', 'unknown media')}\n"
        f"Torrent hash: {key}\nBlocker: {detail}\n"
        "Import has not been verified; Jellyfin availability is unconfirmed.\n"
        f"Action needed: {question}\n"
        f'Send Hermes a new message: "Review media {key}" with your answer. '
        "This no-agent cron only delivers alerts; it cannot process replies or approve imports. "
        "Hermes must show the dry-run plan and ask for approval before importing. "
        f"CLI inspection: sudo media-organizer review {key}"
    )


def notify_review(state, key, *, force=False):
    item = state["items"][key]
    now = int(time.time())
    if (force or item.get("reviewNoticeVersion") != REVIEW_NOTICE_VERSION
            or now - item.get("reviewNotifiedAt", 0) >= REVIEW_REMINDER_SECONDS):
        emit_event(key, review_message(key, item))
        item["reviewNoticeVersion"] = REVIEW_NOTICE_VERSION
        item["reviewNotifiedAt"] = now


def transmission_torrents():
    raw = command(TRANSMISSION + ["--json", "--torrent", "all", "--info"])
    try:
        torrents = json.loads(raw)["result"]["torrents"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise OrganizerError("Transmission returned an unexpected response.") from error
    if not isinstance(torrents, list):
        raise OrganizerError("Transmission returned an unexpected response.")
    return torrents


def source_path(torrent):
    container_dir = PurePosixPath(torrent.get("download_dir", ""))
    try:
        relative_dir = container_dir.relative_to(CONTAINER_DOWNLOAD_ROOT)
    except ValueError as error:
        raise OrganizerError("Torrent download path is outside /downloads.") from error
    source = (Path("/srv/data/media") / Path(*relative_dir.parts) / torrent["name"]).resolve()
    if not source.is_relative_to(MANUAL_ROOT):
        raise OrganizerError("Torrent source is outside the completed manual library.")
    if not source.exists():
        raise OrganizerError("Completed torrent payload is missing from the host library.")
    return source


def media_files(source):
    candidates = [source] if source.is_file() else source.rglob("*")
    return [
        path for path in candidates
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
    ]


def published_inodes():
    result = set()
    for root in LIBRARY_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result.add((stat.st_dev, stat.st_ino))
    return result


def is_published(source, library_inodes):
    files = media_files(source)
    if not files:
        return False
    for path in files:
        stat = path.stat()
        if (stat.st_dev, stat.st_ino) not in library_inodes:
            return False
    return True


def clean_title(value):
    value = re.sub(r"^\[[^]]+\]\s*", "", value)
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+-\s+$", "", value)
    return re.sub(r"\s+", " ", value).strip(" -_()[]")


def classify(name):
    # Torrent names are often extensionless directories containing dots. Only
    # remove a known media extension, never the last release-name component.
    stem = Path(name).stem if Path(name).suffix.lower() in MEDIA_SUFFIXES else name
    tv_match = re.search(
        r"(?i)^(?P<title>.+?)[ ._-]+S(?P<season>\d{1,2})(?:E\d{1,3})?\b",
        stem,
    )
    if tv_match:
        raw_title = re.sub(
            r"(?i)[ ._-]+Season[ ._-]*\d{1,2}$", "", tv_match.group("title")
        )
        year_match = re.match(
            r"^(?P<title>.+?)[ ._\-[(]+(?P<year>(?:19|20)\d{2})[)\]]?$",
            raw_title,
        )
        if year_match:
            return "tv", clean_title(year_match.group("title")), int(year_match.group("year"))
        return "tv", clean_title(raw_title), None
    movie_match = re.search(
        r"(?i)^(?P<title>.+?)[ ._\-\[(]+(?P<year>(?:19|20)\d{2})\b",
        stem,
    )
    if movie_match:
        return "movie", clean_title(movie_match.group("title")), int(movie_match.group("year"))
    raise OrganizerError("Cannot safely infer TV episode/season or movie title and year.")


def manager_name(kind):
    return "sonarr" if kind == "tv" else "radarr"


def organize_torrent(torrent, source, kind, title, year, started_managers):
    manager = manager_name(kind)
    if manager not in started_managers:
        was_active = subprocess.run(
            ["systemctl", "is-active", "--quiet", f"{manager}.service"],
            check=False,
        ).returncode == 0
        if not was_active:
            command(["media-library", "start", manager])
        started_managers[manager] = not was_active
    args = [
        "media-library",
        f"organize-{kind}",
        "--path", str(source),
        "--title", title,
    ]
    if year is not None:
        args += ["--year", str(year)]
    args += ["--confirm"]
    return command(args)


def requires_review(error):
    detail = str(error).casefold()
    return any(marker in detail for marker in (
        "cannot safely infer",
        "rejected",
        "no accepted media files",
        "could not identify exactly one",
        "movie runtime mismatch",
        "could not verify the movie runtime",
        "did not identify any media",
        "scanned movie file does not match",
        "scanned tv files do not all match",
        "verification failed",
    ))


def run(dry_run=False):
    state = load_state()
    library_inodes = published_inodes()
    started_managers = {}
    try:
        for torrent in transmission_torrents():
            if torrent.get("left_until_done") != 0:
                continue
            key = torrent.get("hash_string") or str(torrent.get("id"))
            source = None
            try:
                previous = state["items"].get(key, {})
                # An explicit later replacement may intentionally unlink one
                # file from an older season/movie torrent. Never let the
                # automatic pass restore that superseded library file. A user
                # can opt back in with `media-organizer retry <hash>`.
                if previous.get("status") == "organized" and not dry_run:
                    continue
                source = source_path(torrent)
                if is_published(source, library_inodes):
                    if dry_run:
                        print(f"Verified existing organized hard links: {torrent['name']} ← {source}")
                        continue
                    record_state(
                        state, key, torrent, "organized",
                        "Verified hard links in the organized Jellyfin library paths.",
                        source=source,
                        notify=(
                            f"Verified media import into Jellyfin library paths: {torrent['name']} "
                            f"(torrent {key}). All source media files have organized hard links; "
                            "Jellyfin's library monitor still needs to index them."
                            if previous.get("status") in {"needs-review", "error", "retry"} else None
                        ),
                    )
                    continue
                if previous.get("status") == "needs-review" and not dry_run:
                    notify_review(state, key)
                    continue
                kind, title, year = classify(torrent["name"])
                if dry_run:
                    year_text = f" ({year})" if year is not None else ""
                    print(f"Would organize via {manager_name(kind)}: {title}{year_text} ← {source}")
                    continue
                detail = organize_torrent(
                    torrent,
                    source,
                    kind,
                    title,
                    year,
                    started_managers,
                )
                library_inodes = published_inodes()
                if not is_published(source, library_inodes):
                    raise OrganizerError(
                        "Import verification failed: not every source media file has a hard link "
                        "in the organized library. Inspect the import before trying again."
                    )
                record_state(
                    state,
                    key,
                    torrent,
                    "organized",
                    detail,
                    source=source,
                    notify=(
                        f"Verified media import into Jellyfin library paths via {manager_name(kind).title()}: "
                        f"{torrent['name']} (torrent {key}). Jellyfin's library monitor still needs to index it."
                    ),
                )
            except OrganizerError as error:
                status = "needs-review" if requires_review(error) else "error"
                if dry_run:
                    print(f"Would require review: {torrent.get('name', 'unknown')} — {error}")
                    continue
                changed = record_state(
                    state,
                    key,
                    torrent,
                    status,
                    str(error),
                    source=source,
                )
                notify_review(state, key, force=changed)
    finally:
        for manager, started_here in started_managers.items():
            if started_here:
                subprocess.run(
                    ["media-library", "stop", manager],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        if not dry_run:
            save_private_json(STATE_FILE, state)


def show_status():
    state = load_state()
    if not state["items"]:
        print("No completed media has been evaluated yet.")
        return
    for key, item in sorted(state["items"].items(), key=lambda entry: entry[1].get("name", "")):
        print(f"{item.get('status', 'unknown'):>12}  {item.get('name', 'unknown')}")
        print(f"              Torrent hash: {key}")
        print(f"              {item.get('detail', '')}")


def show_review(key):
    item = load_state()["items"].get(key)
    if item is None:
        raise OrganizerError("No organizer state exists for that torrent hash.")
    if item.get("status") not in {"needs-review", "error"}:
        raise OrganizerError("No pending import review for that torrent; inspect sudo media-organizer status.")
    print(review_message(key, item))
    torrent = next((torrent for torrent in transmission_torrents()
                    if (torrent.get("hash_string") or str(torrent.get("id"))) == key), None)
    if torrent is None or torrent.get("left_until_done") != 0:
        raise OrganizerError("This torrent is no longer completed in Transmission; inspect sudo media-queue status first.")
    source = source_path(torrent)
    print(f"Verified completed source path: {source}")
    try:
        kind, title, year = classify(torrent["name"])
    except OrganizerError:
        print("Provide the media kind (tv/movie), exact title, and release year before planning an import.")
        return
    print("Suggested preflight; correct the title/year from your answer before running:")
    print(shlex.join(["sudo", "media-library", "start", manager_name(kind)]))
    args = ["sudo", "media-library", f"organize-{kind}", "--path", str(source), "--title", title]
    if year is not None:
        args += ["--year", str(year)]
    print(shlex.join(args + ["--dry-run"]))
    print("Review the plan with the user, then use the media-queue skill's confirmation flow. "
          "Stop any manager started for this review when finished.")


def retry(key):
    state = load_state()
    if key == "all":
        for item in state["items"].values():
            if item.get("status") in {"needs-review", "error"}:
                item["status"] = "retry"
    elif key in state["items"]:
        state["items"][key]["status"] = "retry"
    else:
        raise OrganizerError("No organizer state exists for that torrent hash.")
    save_private_json(STATE_FILE, state)


def forget(key):
    if not re.fullmatch(r"[0-9a-f]{40}", key, re.IGNORECASE):
        raise OrganizerError("Use the exact 40-character torrent hash from a confirmed stale record.")
    state = load_state()
    if key not in state["items"]:
        raise OrganizerError("No organizer state exists for that torrent hash.")
    del state["items"][key]
    save_private_json(STATE_FILE, state)
    print(f"Removed organizer history for torrent hash {key}.")


def self_test():
    fixtures = {
        "One Punch Man - S03E05 - Monster King.mkv": ("tv", "One Punch Man", None),
        "One.Punch.Man.S03.1080p.WEB-DL": ("tv", "One Punch Man", None),
        "Backrooms.2026.2160p.WEB-DL": ("movie", "Backrooms", 2026),
        "Obsession (2026) 1080p.mkv": ("movie", "Obsession", 2026),
    }
    for name, expected in fixtures.items():
        actual = classify(name)
        if actual != expected:
            raise OrganizerError(f"Classifier failed for {name!r}: {actual!r} != {expected!r}")
    print("Media organizer classifier tests passed.")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("status")
    review_parser = subparsers.add_parser("review", help="inspect one completed torrent and print a guarded preflight handoff")
    review_parser.add_argument("hash", help="exact torrent hash from the review alert")
    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("hash", help="torrent hash or 'all'")
    forget_parser = subparsers.add_parser("forget")
    forget_parser.add_argument("hash", help="exact torrent hash whose stale organizer history should be removed")
    subparsers.add_parser("self-test")
    return parser.parse_args()


def main():
    if os.geteuid() != 0:
        raise OrganizerError("Run this command as: sudo media-organizer …")
    args = parse_args()
    if args.command == "run":
        run(args.dry_run)
    elif args.command == "status":
        show_status()
    elif args.command == "review":
        show_review(args.hash)
    elif args.command == "retry":
        retry(args.hash)
    elif args.command == "forget":
        forget(args.hash)
    elif args.command == "self-test":
        self_test()


if __name__ == "__main__":
    try:
        main()
    except OrganizerError as error:
        print(f"media-organizer: {error}", file=sys.stderr)
        raise SystemExit(2)
