#!/usr/bin/env python3
"""Safely add and hard-link import existing media with Sonarr or Radarr."""

import argparse
import json
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


def matching_lookup(manager, title, year):
    resource = MANAGERS[manager]["resource"]
    candidates = api(manager, f"/{resource}/lookup", query={"term": title})
    matches = [item for item in candidates if normalized(item.get("title")) == normalized(title)]
    if year is not None:
        matches = [item for item in matches if item.get("year") == year]
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


def prepare_files(manager, files, record):
    accepted = [item for item in files if not item.get("rejections")]
    if not accepted:
        raise LibraryError("No accepted media files were found in that folder.")
    rejected = len(files) - len(accepted)
    if rejected:
        raise LibraryError(f"{rejected} file(s) were rejected; refusing a partial import.")
    for item in accepted:
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
    folder = Path(args.path).resolve()
    if not folder.is_dir():
        raise LibraryError("The source path must be an existing directory.")
    lookup = matching_lookup(manager, args.title, args.year)
    record = existing_record(manager, lookup)
    if args.dry_run:
        state = "existing record" if record else "new unmonitored record"
        print(
            f"Plan: {MANAGERS[manager]['name']} → {lookup['title']} ({lookup.get('year', '?')}); "
            f"{state}; source={folder}; mode=hard-link copy."
        )
        return
    if not args.confirm:
        raise LibraryError("Importing requires --confirm after reviewing the plan.")
    if record is None:
        record = add_record(manager, lookup)
    files = prepare_files(manager, scan_import(manager, folder), record)
    source_files = [Path(item["path"]) for item in files]
    command = api(manager, "/command", "POST", {
        "name": "ManualImport",
        "importMode": "copy",
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
        organize_parser.add_argument("--dry-run", action="store_true", help="validate the exact library match without changing it")
        organize_parser.add_argument("--confirm", action="store_true", help="required to add/import after reviewing a dry run")
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
