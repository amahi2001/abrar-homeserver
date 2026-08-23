#!/usr/bin/env python3
"""Organize completed Transmission media with on-demand Sonarr or Radarr."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath


class OrganizerError(RuntimeError):
    pass


STATE_FILE = Path("/var/lib/media-queue/organizer-state.json")
EVENT_DIR = Path("/var/lib/media-queue/events")
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


def record_state(state, key, torrent, status, detail, *, notify=None):
    previous = state["items"].get(key, {})
    changed = previous.get("status") != status or previous.get("detail") != detail
    state["items"][key] = {
        "detail": detail,
        "name": torrent.get("name", "unknown media"),
        "status": status,
        "torrentId": torrent.get("id"),
        "updatedAt": int(time.time()),
    }
    if changed and notify:
        emit_event(key, notify)


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
    stem = Path(name).stem
    tv_match = re.search(
        r"(?i)^(?P<title>.+?)[ ._-]+S(?P<season>\d{1,2})(?:E\d{1,3})?\b",
        stem,
    )
    if tv_match:
        return "tv", clean_title(tv_match.group("title")), None
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


def run(dry_run=False):
    state = load_state()
    library_inodes = published_inodes()
    started_managers = {}
    try:
        for torrent in transmission_torrents():
            if torrent.get("left_until_done") != 0:
                continue
            key = torrent.get("hash_string") or str(torrent.get("id"))
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
                    record_state(state, key, torrent, "organized", "Already published in Jellyfin library.")
                    continue
                if previous.get("status") == "needs-review" and not dry_run:
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
                record_state(
                    state,
                    key,
                    torrent,
                    "organized",
                    detail,
                    notify=f"Media organized for Jellyfin via {manager_name(kind).title()}: {torrent['name']}",
                )
                library_inodes = published_inodes()
            except OrganizerError as error:
                status = "needs-review" if (
                    "Cannot safely infer" in str(error)
                    or "rejected" in str(error).lower()
                    or "No accepted media files" in str(error)
                    or "Could not identify exactly one" in str(error)
                ) else "error"
                if dry_run:
                    print(f"Would require review: {torrent.get('name', 'unknown')} — {error}")
                    continue
                record_state(
                    state,
                    key,
                    torrent,
                    status,
                    str(error),
                    notify=f"Media organization needs review: {torrent.get('name', 'unknown')} — {error}",
                )
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
    for item in sorted(state["items"].values(), key=lambda value: value.get("name", "")):
        print(f"{item.get('status', 'unknown'):>12}  {item.get('name', 'unknown')}")
        print(f"              {item.get('detail', '')}")


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
