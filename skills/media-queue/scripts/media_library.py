#!/usr/bin/env python3
"""Safely add and hard-link import existing media with Sonarr or Radarr."""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class LibraryError(RuntimeError):
    pass


MANAGERS = {
    "tv": {
        "name": "Sonarr",
        "service": "sonarr.service",
        "base": "http://127.0.0.1:8989/api/v3",
        "config": Path("/var/lib/sonarr/.config/NzbDrone/config.xml"),
        "resource": "series",
        "root": "/srv/data/media/library/tv",
    },
    "movie": {
        "name": "Radarr",
        "service": "radarr.service",
        "base": "http://127.0.0.1:7878/api/v3",
        "config": Path("/var/lib/radarr/.config/Radarr/config.xml"),
        "resource": "movie",
        "root": "/srv/data/media/library/movies",
    },
}

MANUAL_ROOT = Path("/srv/data/media/library/manual")


def require_root():
    if os.geteuid() != 0:
        raise LibraryError("Run this command as: sudo media-library …")


def normalized(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def api_key(manager):
    try:
        key = ET.parse(MANAGERS[manager]["config"]).findtext("ApiKey")
    except (ET.ParseError, OSError) as error:
        raise LibraryError(f"Cannot read {MANAGERS[manager]['name']} configuration.") from error
    if not key:
        raise LibraryError(f"{MANAGERS[manager]['name']} API key is unavailable.")
    return key


def api(manager, path, method="GET", body=None, query=None):
    url = MANAGERS[manager]["base"] + path
    if query:
        url += "?" + urlencode(query)
    headers = {"X-Api-Key": api_key(manager)}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise LibraryError(f"{MANAGERS[manager]['name']} API request failed.") from error


def require_active(manager):
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", MANAGERS[manager]["service"]],
        check=False,
    )
    if result.returncode:
        raise LibraryError(
            f"{MANAGERS[manager]['name']} is stopped. Start it first with: "
            f"sudo media-library start {MANAGERS[manager]['service'].removesuffix('.service')}"
        )


def matching_lookup(manager, title, year, tmdb_id=None):
    resource = MANAGERS[manager]["resource"]
    candidates = api(manager, f"/{resource}/lookup", query={"term": title})
    matches = [item for item in candidates if normalized(item.get("title")) == normalized(title)]
    if year is not None:
        matches = [item for item in matches if item.get("year") == year]
    if tmdb_id is not None:
        matches = [item for item in matches if item.get("tmdbId") == tmdb_id]
    if len(matches) != 1:
        summary = ", ".join(
            f"{item.get('title', 'unknown')} ({item.get('year', '?')})"
            for item in candidates[:8]
        ) or "none"
        raise LibraryError(
            f"Could not identify exactly one {manager} match for {title!r}. "
            f"Candidates: {summary}."
        )
    return matches[0]


def existing_record(manager, lookup):
    resource = MANAGERS[manager]["resource"]
    key = "tvdbId" if manager == "tv" else "tmdbId"
    for item in api(manager, f"/{resource}"):
        if item.get(key) == lookup.get(key):
            return item
    return None


def first_profile_id(manager):
    profiles = api(manager, "/qualityprofile")
    if not profiles:
        raise LibraryError(f"{MANAGERS[manager]['name']} has no quality profile.")
    return profiles[0]["id"]


def add_record(manager, lookup):
    resource = MANAGERS[manager]["resource"]
    record = dict(lookup)
    record.update({
        "qualityProfileId": first_profile_id(manager),
        "rootFolderPath": MANAGERS[manager]["root"],
        "monitored": False,
    })
    if manager == "tv":
        record.update({"seasonFolder": True, "addOptions": {"searchForMissingEpisodes": False}})
    else:
        record.update({"minimumAvailability": "released", "addOptions": {"searchForMovie": False}})
    return api(manager, f"/{resource}", "POST", record)


def scan_import(manager, folder):
    return api(manager, "/manualimport", query={
        "folder": str(folder),
        "filterExistingFiles": "false",
    })


def scan_source(manager, source):
    scan_root = source if source.is_dir() else source.parent
    candidates = scan_import(manager, scan_root)
    selected = []
    for item in candidates:
        item_path = Path(item.get("path", "")).resolve()
        if (source.is_file() and item_path == source) or (
            source.is_dir() and item_path.is_relative_to(source)
        ):
            selected.append(item)
    if not selected:
        raise LibraryError("Sonarr/Radarr did not identify any media at that source path.")
    return selected


def replacement_rejections_only(item):
    allowed = (
        "not an upgrade for existing episode file",
        "not an upgrade for existing movie file",
        "movie file with same quality already exists",
    )
    rejections = item.get("rejections") or []
    return rejections and all(
        any(str(rejection.get("reason", "")).lower().startswith(prefix) for prefix in allowed)
        for rejection in rejections
    )


def accepted_files(files, replace_existing=False):
    rejected_items = [item for item in files if item.get("rejections")]
    if replace_existing and any(not replacement_rejections_only(item) for item in rejected_items):
        raise LibraryError("The media has a rejection unrelated to replacing an existing file.")
    accepted = files if replace_existing else [item for item in files if not item.get("rejections")]
    if not accepted:
        reasons = "; ".join(
            rejection.get("reason", "unknown rejection")
            for item in rejected_items
            for rejection in item.get("rejections", [])
        )
        raise LibraryError(f"No accepted media files were found: {reasons}")
    if rejected_items and not replace_existing:
        raise LibraryError(
            f"{len(rejected_items)} file(s) were rejected; refusing a partial import."
        )
    return accepted


def probe_runtime_minutes(files):
    total_seconds = 0.0
    for item in files:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", item["path"],
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            duration = float(result.stdout.strip())
        except (
            KeyError, OSError, ValueError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise LibraryError("Could not verify the movie runtime; review it before importing.") from error
        if not math.isfinite(duration) or duration <= 0:
            raise LibraryError("Could not verify the movie runtime; review it before importing.")
        total_seconds += duration
    return total_seconds / 60


def validate_movie_runtime(lookup, files):
    expected = lookup.get("runtime")
    if not isinstance(expected, (int, float)) or expected < 10:
        return
    actual = probe_runtime_minutes(files)
    # Allow ordinary catalog differences and longer cuts; stop gross mismatches.
    if abs(actual - expected) < 30 or 0.65 <= actual / expected <= 1.5:
        return
    title = lookup["title"]
    year = lookup.get("year")
    try:
        alternatives = api("movie", "/movie/lookup", query={"term": title})
    except LibraryError:
        alternatives = []
    suggestions = [
        candidate for candidate in alternatives
        if normalized(candidate.get("title")) == normalized(title)
        and candidate.get("tmdbId") != lookup.get("tmdbId")
        and isinstance(candidate.get("year"), int)
        and isinstance(year, int)
        and abs(candidate["year"] - year) <= 1
        and isinstance(candidate.get("runtime"), (int, float))
        and abs(candidate["runtime"] - actual) <= max(10, actual * 0.15)
    ]
    suggestions.sort(key=lambda candidate: abs(candidate["runtime"] - actual))
    hint = ""
    if suggestions:
        candidate = suggestions[0]
        hint = (
            f" Possible same-title match: {candidate['title']} ({candidate['year']}, "
            f"TMDb {candidate.get('tmdbId', '?')}, {candidate['runtime']} min)."
        )
    raise LibraryError(
        f"Movie runtime mismatch: file {actual:.0f} min; selected {title} "
        f"({year}, TMDb {lookup.get('tmdbId', '?')}) {expected} min."
        f"{hint} Review the file before importing."
    )


def prepare_files(manager, files, record, replace_existing=False):
    accepted = accepted_files(files, replace_existing)
    for item in accepted:
        item.pop("rejections", None)
        if manager == "tv":
            series = item.get("series") or {}
            episodes = item.get("episodes") or []
            if series.get("id") != record.get("id") or not episodes:
                raise LibraryError("The scanned TV files do not all match the selected series.")
            item["seriesId"] = record["id"]
            item["episodeIds"] = [episode["id"] for episode in episodes]
        else:
            movie = item.get("movie") or {}
            if movie.get("id") != record.get("id"):
                raise LibraryError("The scanned movie file does not match the selected movie.")
            item["movieId"] = record["id"]
    return accepted


def wait_for_command(manager, command_id):
    for _ in range(60):
        command = api(manager, f"/command/{command_id}")
        if command.get("status") == "completed":
            return
        if command.get("status") in {"failed", "aborted"}:
            raise LibraryError(f"{MANAGERS[manager]['name']} import command {command.get('status')}.")
        time.sleep(1)
    raise LibraryError(f"{MANAGERS[manager]['name']} import timed out.")


def verify_hardlinks(source_files, library_path):
    destinations = [path for path in Path(library_path).rglob("*") if path.is_file()]
    for source in source_files:
        try:
            source_stat = source.stat()
        except OSError as error:
            raise LibraryError("An import source disappeared during verification.") from error
        if source_stat.st_nlink < 2 or not any(
            (destination.stat().st_dev, destination.stat().st_ino) ==
            (source_stat.st_dev, source_stat.st_ino)
            for destination in destinations
        ):
            raise LibraryError("Hard-link verification failed; the source was left intact.")


def organize(args):
    manager = args.kind
    require_active(manager)
    source = Path(args.path).resolve()
    if not source.exists() or not (source.is_file() or source.is_dir()):
        raise LibraryError("The source path must be an existing file or directory.")
    if not source.is_relative_to(MANUAL_ROOT):
        raise LibraryError("The source path must be inside the completed manual library.")
    # TMDB IDs disambiguate Radarr movies only; Sonarr's TV parser does not
    # define this optional argument.
    lookup = matching_lookup(manager, args.title, args.year, getattr(args, "tmdb_id", None))
    record = existing_record(manager, lookup)
    if not args.dry_run and not args.confirm:
        raise LibraryError("Importing requires --confirm after reviewing the plan.")
    if args.replace_existing and record is None:
        raise LibraryError("Replacement requires an existing Sonarr/Radarr record.")
    files = scan_source(manager, source)
    if manager == "movie":
        validate_movie_runtime(lookup, files)
    if args.dry_run:
        state = "existing record" if record else "new unmonitored record"
        replacement = " forced replacement" if args.replace_existing else ""
        if record:
            prepare_files(manager, files, record, args.replace_existing)
            validation = "scan matches record"
        else:
            validation = "preflight only; final match checked after record creation"
        print(
            f"Plan: {MANAGERS[manager]['name']} → {lookup['title']} ({lookup.get('year', '?')}); "
            f"{state}; source={source}; mode=hard-link copy{replacement}; {validation}."
        )
        return
    if record is None:
        record = add_record(manager, lookup)
        files = scan_source(manager, source)
    files = prepare_files(
        manager,
        files,
        record,
        args.replace_existing,
    )
    source_files = [Path(item["path"]) for item in files]
    command = api(manager, "/command", "POST", {
        "name": "ManualImport",
        "importMode": "copy",
        "replaceExistingFiles": args.replace_existing,
        "files": files,
    })
    wait_for_command(manager, command["id"])
    verify_hardlinks(source_files, record["path"])
    print(
        f"Imported {len(files)} file(s) into {record['path']} using verified hard links. "
        "Source files remain intact for Transmission seeding."
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for kind in ("tv", "movie"):
        organize_parser = subparsers.add_parser(f"organize-{kind}")
        organize_parser.set_defaults(kind=kind)
        organize_parser.add_argument("--path", required=True, help="completed-media directory under the library")
        organize_parser.add_argument("--title", required=True, help="exact Sonarr/Radarr title")
        organize_parser.add_argument("--year", type=int, required=(kind == "movie"))
        if kind == "movie":
            organize_parser.add_argument("--tmdb-id", type=int, help="disambiguate duplicate title/year metadata")
        organize_parser.add_argument("--dry-run", action="store_true", help="validate the exact library match without changing it")
        organize_parser.add_argument("--confirm", action="store_true", help="required to add/import after reviewing a dry run")
        organize_parser.add_argument(
            "--replace-existing",
            action="store_true",
            help="explicitly replace an existing episode/movie rejected only as not an upgrade",
        )
    return parser.parse_args()


def main():
    args = parse_args()
    require_root()
    organize(args)


if __name__ == "__main__":
    try:
        main()
    except LibraryError as error:
        print(f"media-library: {error}", file=sys.stderr)
        raise SystemExit(2)
