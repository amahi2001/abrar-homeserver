#!/usr/bin/env python3
"""Safely remove Transmission jobs and clean up organized media."""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class CleanupError(RuntimeError):
    pass


MANUAL_ROOT = Path("/srv/data/media/library/manual")
INCOMPLETE_ROOT = Path("/srv/data/media/incomplete")
LIBRARY_ROOTS = {
    "tv": Path("/srv/data/media/library/tv"),
    "movie": Path("/srv/data/media/library/movies"),
}
TRASH_ROOT = Path("/srv/data/media/.media-cleanup-trash")
PLAN_FILE = Path("/var/lib/media-queue/cleanup-plan.json")
EVENT_DIR = Path("/var/lib/media-queue/events")
PLAN_LIFETIME = 30 * 60
CONTAINER_ROOT = PurePosixPath("/downloads")
TRANSMISSION = [
    "docker", "exec", "transmission-vpn", "sh", "-ceu",
    'export TR_AUTH="$USER:$PASS"; exec transmission-remote 127.0.0.1:9091 --authenv "$@"',
    "--",
]
MANAGERS = {
    "tv": {
        "service": "sonarr.service",
        "base": "http://127.0.0.1:8989/api/v3",
        "config": Path("/var/lib/sonarr/.config/NzbDrone/config.xml"),
        "resource": "series",
        "rescan": "RescanSeries",
        "idKey": "seriesId",
    },
    "movie": {
        "service": "radarr.service",
        "base": "http://127.0.0.1:7878/api/v3",
        "config": Path("/var/lib/radarr/.config/Radarr/config.xml"),
        "resource": "movie",
        "rescan": "RescanMovie",
        "idKey": "movieId",
    },
}
STATUS = {
    0: "stopped",
    1: "checking",
    2: "checking",
    3: "queued",
    4: "downloading",
    5: "queued-to-seed",
    6: "seeding",
}


def require_root():
    if os.geteuid() != 0:
        raise CleanupError("Run this command as: sudo media-cleanup …")


def command(args):
    result = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise CleanupError(detail)
    return result.stdout.strip()


def save_private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def emit_event(message):
    EVENT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    save_private_json(
        EVENT_DIR / f"{time.time_ns()}-cleanup.json",
        {"message": message},
    )


def readable_size(value):
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "unknown"


def transmission_torrents():
    raw = command(TRANSMISSION + ["--json", "--torrent", "all", "--info"])
    try:
        torrents = json.loads(raw)["result"]["torrents"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise CleanupError("Transmission returned an unexpected response.") from error
    if not isinstance(torrents, list):
        raise CleanupError("Transmission returned an unexpected response.")
    return torrents


def find_torrent(reference):
    torrents = transmission_torrents()
    if reference.isdecimal():
        matches = [item for item in torrents if item.get("id") == int(reference)]
    else:
        if len(reference) < 7 or not re.fullmatch(r"[0-9a-fA-F]+", reference):
            raise CleanupError("Use a displayed Transmission ID or at least seven hash characters.")
        matches = [
            item for item in torrents
            if str(item.get("hash_string", "")).lower().startswith(reference.lower())
        ]
    if len(matches) != 1:
        raise CleanupError("That reference does not identify exactly one current torrent.")
    return matches[0]


def source_path(torrent, *, require_exists=True):
    container_dir = PurePosixPath(str(torrent.get("download_dir", "")))
    try:
        relative = container_dir.relative_to(CONTAINER_ROOT)
    except ValueError as error:
        raise CleanupError("Torrent download directory is outside /downloads.") from error
    source = (Path("/srv/data/media") / Path(*relative.parts) / torrent["name"]).resolve()
    if not source.is_relative_to(MANUAL_ROOT):
        raise CleanupError("Torrent payload is outside the completed manual library.")
    if require_exists and not source.exists():
        raise CleanupError("Torrent payload is missing from the completed manual library.")
    return source


def regular_files(source):
    if source.is_symlink():
        raise CleanupError("Refusing a symlinked torrent payload.")
    if source.is_file():
        return [source]
    result = []
    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            if (base / name).is_symlink():
                raise CleanupError("Refusing a payload containing a symlinked directory.")
        for name in file_names:
            path = base / name
            if path.is_symlink():
                raise CleanupError("Refusing a payload containing a symlinked file.")
            if path.is_file():
                result.append(path)
    return sorted(result)


def library_inode_index():
    index = {}
    for kind, root in LIBRARY_ROOTS.items():
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                index.setdefault((stat.st_dev, stat.st_ino), []).append((kind, path))
    return index


def file_snapshot(source, index):
    files = []
    for path in regular_files(source):
        stat = path.stat()
        links = [
            {"kind": kind, "path": str(link)}
            for kind, link in sorted(index.get((stat.st_dev, stat.st_ino), []), key=lambda item: str(item[1]))
        ]
        files.append({
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "links": links,
            "mtimeNs": stat.st_mtime_ns,
            "path": str(path),
            "size": stat.st_size,
        })
    return files


def path_target(path):
    raw_path = Path(path)
    if not raw_path.is_absolute() or raw_path.is_symlink():
        raise CleanupError("Use an existing, non-symlinked absolute managed-media path.")
    resolved = raw_path.resolve()
    if not resolved.exists():
        raise CleanupError(f"Managed-media path does not exist: {resolved}")
    for kind, root in LIBRARY_ROOTS.items():
        if resolved.is_relative_to(root) and resolved != root:
            return {"kind": kind, "path": resolved, "root": root}
    for name, root in {"manual": MANUAL_ROOT, "incomplete": INCOMPLETE_ROOT}.items():
        if resolved.is_relative_to(root) and resolved != root:
            return {"kind": name, "path": resolved, "root": root}
    raise CleanupError("Managed-media path is outside an allowed cleanup root.")


def path_snapshot(paths):
    targets = [path_target(path) for path in paths]
    resolved_paths = [target["path"] for target in targets]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise CleanupError("Each managed-media path may be planned only once.")
    if any(
        left != right and left.is_relative_to(right)
        for left in resolved_paths
        for right in resolved_paths
    ):
        raise CleanupError("Do not plan both a managed-media path and one of its children.")
    return {
        "targets": [
            {
                "kind": target["kind"],
                "path": str(target["path"]),
                "root": str(target["root"]),
                "files": [
                    {
                        "device": (stat := file.stat()).st_dev,
                        "inode": stat.st_ino,
                        "mtimeNs": stat.st_mtime_ns,
                        "path": str(file),
                        "size": stat.st_size,
                    }
                    for file in regular_files(target["path"])
                ],
            }
            for target in targets
        ]
    }


def plan_snapshot(torrent, mode):
    source = source_path(torrent, require_exists=(mode != "remove-job"))
    files = [] if mode == "remove-job" or not source.exists() else file_snapshot(source, library_inode_index())
    return {
        "downloadDir": torrent.get("download_dir"),
        "files": files,
        "hash": torrent.get("hash_string"),
        "id": torrent.get("id"),
        "name": torrent.get("name"),
        "size": torrent.get("size_when_done", 0),
        "source": str(source),
    }


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_cleanup_plan(torrent, mode, snapshot):
    complete = torrent.get("left_until_done") == 0
    if mode in {"archive", "purge", "delete"} and not complete:
        raise CleanupError("Archive, purge, and permanent deletion are blocked until the torrent is fully downloaded.")
    if mode == "archive":
        if not snapshot["files"]:
            raise CleanupError("Archive requires at least one regular payload file.")
        unpublished = [item["path"] for item in snapshot["files"] if not item["links"]]
        if unpublished:
            raise CleanupError(
                "Archive is unsafe because these payload files are not hard-linked into the library: "
                + "; ".join(unpublished)
            )


def show_list():
    torrents = sorted(transmission_torrents(), key=lambda item: int(item.get("id", 0)))
    if not torrents:
        print("Transmission has no torrents.")
        return
    index = library_inode_index()
    for torrent in torrents:
        try:
            source = source_path(torrent, require_exists=False)
            files = file_snapshot(source, index) if source.exists() else []
            published = sum(1 for item in files if item["links"])
            publication = f"published={published}/{len(files)}" if files else "payload=missing"
        except CleanupError as error:
            publication = f"inspection={error}"
        progress = 100 * float(torrent.get("percent_done", 0))
        print(
            f"{torrent.get('id')}. {torrent.get('name', 'unknown')}\n"
            f"   state={STATUS.get(torrent.get('status'), 'unknown')} progress={progress:.1f}% "
            f"size={readable_size(torrent.get('size_when_done', 0))} "
            f"ratio={float(torrent.get('upload_ratio', 0)):.2f} {publication}"
        )


def create_plan(reference, mode):
    torrent = find_torrent(reference)
    snapshot = plan_snapshot(torrent, mode)
    validate_cleanup_plan(torrent, mode, snapshot)
    token = secrets.token_hex(4)
    plan = {
        "action": "cleanup",
        "createdAt": int(time.time()),
        "digest": digest(snapshot),
        "mode": mode,
        "snapshot": snapshot,
        "token": token,
    }
    save_private_json(PLAN_FILE, plan)
    print(f"Cleanup plan ({mode}) for Transmission ID {snapshot['id']}: {snapshot['name']}")
    print(f"Source: {snapshot['source']}")
    payload_size = sum(item["size"] for item in snapshot["files"]) or snapshot["size"]
    print(f"Payload: {len(snapshot['files'])} inspected file(s), {readable_size(payload_size)}")
    if mode == "remove-job":
        print("Effect: remove only the Transmission job; every file remains on disk.")
    elif mode == "archive":
        print("Effect: stop seeding, remove the Transmission job, and unlink the manual payload.")
        print("Verified library paths retained:")
        for item in snapshot["files"]:
            for link in item["links"]:
                print(f"  KEEP {link['path']}")
    elif mode == "purge":
        print("Effect: remove the job and move the following source/library paths to quarantine:")
        print(f"  MOVE {snapshot['source']}")
        for path in sorted({link["path"] for item in snapshot["files"] for link in item["links"]}):
            print(f"  MOVE {path}")
        print("Quarantine does not free the media bytes until separately destroyed.")
    else:
        print("Effect: permanently delete the Transmission job and the following source/library paths:")
        print(f"  DELETE {snapshot['source']}")
        for path in sorted({link["path"] for item in snapshot["files"] for link in item["links"]}):
            print(f"  DELETE {path}")
        print("This cannot be recovered after confirmation.")
    print(f"Plan expires in 30 minutes. Apply only after user confirmation: media-cleanup apply {token} --confirm")


def create_path_delete_plan(paths):
    snapshot = path_snapshot(paths)
    token = secrets.token_hex(4)
    plan = {
        "action": "path-delete",
        "createdAt": int(time.time()),
        "digest": digest(snapshot),
        "snapshot": snapshot,
        "token": token,
    }
    save_private_json(PLAN_FILE, plan)
    files = [file for target in snapshot["targets"] for file in target["files"]]
    unique = {(item["device"], item["inode"]): item["size"] for item in files}
    print(f"Permanent deletion plan for {len(snapshot['targets'])} managed-media path(s)")
    print(f"Files: {len(files)}; releasable bytes: {readable_size(sum(unique.values()))}")
    for target in snapshot["targets"]:
        print(f"  DELETE {target['path']}")
    print(f"Plan expires in 30 minutes. Apply only after user confirmation: media-cleanup apply {token} --confirm")


def stop_torrent(torrent_id):
    command(TRANSMISSION + ["--torrent", str(torrent_id), "--stop"])


def remove_torrent(torrent_id):
    command(TRANSMISSION + ["--torrent", str(torrent_id), "--remove"])


def stop_and_remove(torrent_id):
    stop_torrent(torrent_id)
    remove_torrent(torrent_id)


def delete_manual_source(source):
    source = source.resolve()
    if not source.is_relative_to(MANUAL_ROOT) or source == MANUAL_ROOT:
        raise CleanupError("Refusing to remove a path outside the manual payload root.")
    if source.is_dir():
        shutil.rmtree(source)
    else:
        source.unlink()


def delete_library_path(kind, path):
    root = LIBRARY_ROOTS[kind]
    path = path.resolve()
    if not path.is_relative_to(root) or path == root:
        raise CleanupError("Refusing to remove a path outside an organized library root.")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    parent = path.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def delete_managed_path(path, root):
    path = path.resolve()
    if not path.is_relative_to(root) or path == root:
        raise CleanupError("Refusing to remove a path outside a managed cleanup root.")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    parent = path.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def api_key(kind):
    try:
        key = ET.parse(MANAGERS[kind]["config"]).findtext("ApiKey")
    except (ET.ParseError, OSError) as error:
        raise CleanupError(f"Cannot read the {kind} manager configuration.") from error
    if not key:
        raise CleanupError(f"The {kind} manager API key is unavailable.")
    return key


def manager_api(kind, path, method="GET", body=None, query=None):
    url = MANAGERS[kind]["base"] + path
    if query:
        url += "?" + urlencode(query)
    data = None
    headers = {"X-Api-Key": api_key(kind)}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            contents = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise CleanupError(f"The {kind} manager API request failed.") from error
    return json.loads(contents) if contents else None


def ensure_manager(kind, started):
    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", MANAGERS[kind]["service"]],
        check=False,
    ).returncode == 0
    if not active:
        command(["media-library", "start", "sonarr" if kind == "tv" else "radarr"])
        started.add(kind)


def records_for_paths(kind, paths):
    records = manager_api(kind, f"/{MANAGERS[kind]['resource']}")
    result = set()
    for path in paths:
        matches = []
        for record in records:
            record_path = Path(record.get("path", "")).resolve()
            if path == record_path or path.is_relative_to(record_path):
                matches.append((len(record_path.parts), record))
        if not matches:
            raise CleanupError(f"No {kind} manager record owns library path {path}.")
        result.add(max(matches, key=lambda item: item[0])[1]["id"])
    return result


def wait_for_manager_command(kind, command_id):
    for _ in range(90):
        result = manager_api(kind, f"/command/{command_id}")
        if result.get("status") == "completed":
            return
        if result.get("status") in {"failed", "aborted"}:
            raise CleanupError(f"The {kind} manager rescan {result.get('status')}.")
        time.sleep(1)
    raise CleanupError(f"The {kind} manager rescan timed out.")


def purge_to_quarantine(torrent, snapshot):
    source = Path(snapshot["source"])
    library_paths = {
        kind: sorted({
            Path(link["path"])
            for item in snapshot["files"]
            for link in item["links"]
            if link["kind"] == kind
        })
        for kind in LIBRARY_ROOTS
    }
    started = set()
    record_ids = {}
    try:
        for kind, paths in library_paths.items():
            if paths:
                ensure_manager(kind, started)
                record_ids[kind] = records_for_paths(kind, paths)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        bundle = TRASH_ROOT / f"{stamp}-{str(torrent['hash_string'])[:12]}"
        if bundle.exists():
            raise CleanupError("The generated quarantine bundle already exists.")
        bundle.mkdir(mode=0o700, parents=True)
        manifest = {
            "createdAt": int(time.time()),
            "libraryPaths": [str(path) for paths in library_paths.values() for path in paths],
            "name": torrent["name"],
            "originalSource": str(source),
            "status": "moving",
            "torrentHash": torrent["hash_string"],
            "torrentId": torrent["id"],
        }
        save_private_json(bundle / "manifest.json", manifest)

        stop_torrent(torrent["id"])
        relative_source = source.relative_to(MANUAL_ROOT)
        trash_source = bundle / "manual" / relative_source
        trash_source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.rename(source, trash_source)

        for kind, paths in library_paths.items():
            for path in paths:
                root = LIBRARY_ROOTS[kind]
                destination = bundle / kind / path.relative_to(root)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.rename(path, destination)
                parent = path.parent
                while parent != root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent

        remove_torrent(torrent["id"])
        for kind, ids in record_ids.items():
            for record_id in ids:
                body = {
                    "name": MANAGERS[kind]["rescan"],
                    MANAGERS[kind]["idKey"]: record_id,
                }
                task = manager_api(kind, "/command", "POST", body)
                wait_for_manager_command(kind, task["id"])

        manifest["status"] = "quarantined"
        manifest["quarantineSource"] = str(trash_source)
        save_private_json(bundle / "manifest.json", manifest)
        return bundle
    finally:
        for kind in started:
            subprocess.run(
                ["media-library", "stop", "sonarr" if kind == "tv" else "radarr"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def delete_permanently(torrent, snapshot):
    source = Path(snapshot["source"])
    library_paths = {
        kind: sorted({
            Path(link["path"])
            for item in snapshot["files"]
            for link in item["links"]
            if link["kind"] == kind
        })
        for kind in LIBRARY_ROOTS
    }
    started = set()
    record_ids = {}
    try:
        for kind, paths in library_paths.items():
            if paths:
                ensure_manager(kind, started)
                record_ids[kind] = records_for_paths(kind, paths)

        stop_torrent(torrent["id"])
        delete_manual_source(source)
        for kind, paths in library_paths.items():
            for path in paths:
                delete_library_path(kind, path)
        remove_torrent(torrent["id"])
        for kind, ids in record_ids.items():
            for record_id in ids:
                body = {
                    "name": MANAGERS[kind]["rescan"],
                    MANAGERS[kind]["idKey"]: record_id,
                }
                task = manager_api(kind, "/command", "POST", body)
                wait_for_manager_command(kind, task["id"])
    finally:
        for kind in started:
            subprocess.run(
                ["media-library", "stop", "sonarr" if kind == "tv" else "radarr"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def apply_path_delete(plan):
    snapshot = plan["snapshot"]
    planned_paths = [target["path"] for target in snapshot["targets"]]
    current = path_snapshot(planned_paths)
    if digest(current) != plan.get("digest"):
        raise CleanupError("Managed-media paths changed after planning. Inspect and create a new deletion plan.")
    targets = current["targets"]
    library_paths = {
        kind: [Path(target["path"]) for target in targets if target["kind"] == kind]
        for kind in LIBRARY_ROOTS
    }
    started = set()
    record_ids = {}
    try:
        for kind, paths in library_paths.items():
            if paths:
                ensure_manager(kind, started)
                record_ids[kind] = records_for_paths(kind, paths)
        for target in targets:
            delete_managed_path(Path(target["path"]), Path(target["root"]))
        for kind, ids in record_ids.items():
            for record_id in ids:
                body = {
                    "name": MANAGERS[kind]["rescan"],
                    MANAGERS[kind]["idKey"]: record_id,
                }
                task = manager_api(kind, "/command", "POST", body)
                wait_for_manager_command(kind, task["id"])
    finally:
        for kind in started:
            subprocess.run(
                ["media-library", "stop", "sonarr" if kind == "tv" else "radarr"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    PLAN_FILE.unlink(missing_ok=True)
    details = ", ".join(target["path"] for target in targets)
    emit_event(f"Managed media permanently deleted: {details}")
    print(f"Permanently deleted {len(targets)} managed-media path(s).")


def load_plan(token):
    try:
        plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CleanupError("No valid cleanup plan exists. Create a new plan first.") from error
    if not secrets.compare_digest(str(plan.get("token", "")), token):
        raise CleanupError("The confirmation token does not match the current plan.")
    if time.time() - int(plan.get("createdAt", 0)) > PLAN_LIFETIME:
        raise CleanupError("The cleanup plan expired. Inspect and create a new plan.")
    return plan


def apply_plan(token):
    plan = load_plan(token)
    if plan.get("action") == "destroy-trash":
        apply_destroy(plan)
        PLAN_FILE.unlink(missing_ok=True)
        return
    if plan.get("action") == "path-delete":
        apply_path_delete(plan)
        return
    if plan.get("action") != "cleanup":
        raise CleanupError("The saved plan has an unsupported action.")
    snapshot = plan["snapshot"]
    torrent = find_torrent(str(snapshot["id"]))
    current = plan_snapshot(torrent, plan["mode"])
    validate_cleanup_plan(torrent, plan["mode"], current)
    if digest(current) != plan.get("digest"):
        raise CleanupError("Torrent files changed after planning. Inspect and create a new plan.")

    mode = plan["mode"]
    if mode == "remove-job":
        stop_and_remove(torrent["id"])
        result = "Transmission job removed; payload left on disk."
    elif mode == "archive":
        stop_and_remove(torrent["id"])
        delete_manual_source(Path(snapshot["source"]))
        result = "Transmission job and verified manual source removed; Jellyfin library retained."
    elif mode == "purge":
        bundle = purge_to_quarantine(torrent, snapshot)
        result = f"Transmission job and library media moved to quarantine: {bundle.name}"
    elif mode == "delete":
        delete_permanently(torrent, snapshot)
        result = "Transmission job and all planned media paths permanently deleted."
    else:
        raise CleanupError("The saved cleanup mode is invalid.")
    PLAN_FILE.unlink(missing_ok=True)
    emit_event(f"Media cleanup complete: {snapshot['name']} — {result}")
    print(result)


def trash_bundles():
    if not TRASH_ROOT.exists():
        return []
    return sorted(
        [path for path in TRASH_ROOT.iterdir() if path.is_dir() and not path.is_symlink()],
        key=lambda path: path.name,
    )


def trash_snapshot(bundle):
    files = regular_files(bundle)
    return {
        "bundle": bundle.name,
        "files": [
            {
                "device": (stat := path.stat()).st_dev,
                "inode": stat.st_ino,
                "mtimeNs": stat.st_mtime_ns,
                "path": str(path),
                "size": stat.st_size,
            }
            for path in files
        ],
    }


def show_trash():
    bundles = trash_bundles()
    if not bundles:
        print("Media cleanup quarantine is empty.")
        return
    for number, bundle in enumerate(bundles, 1):
        snapshot = trash_snapshot(bundle)
        unique = {(item["device"], item["inode"]): item["size"] for item in snapshot["files"]}
        print(f"{number}. {bundle.name} files={len(snapshot['files'])} retained={readable_size(sum(unique.values()))}")


def find_bundle(name):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{12}", name):
        raise CleanupError("Use an exact quarantine bundle name from media-cleanup trash.")
    bundle = (TRASH_ROOT / name).resolve()
    if not bundle.is_relative_to(TRASH_ROOT) or not bundle.is_dir() or bundle.is_symlink():
        raise CleanupError("That quarantine bundle does not exist.")
    return bundle


def create_destroy_plan(name):
    bundle = find_bundle(name)
    snapshot = trash_snapshot(bundle)
    token = secrets.token_hex(4)
    save_private_json(PLAN_FILE, {
        "action": "destroy-trash",
        "createdAt": int(time.time()),
        "digest": digest(snapshot),
        "snapshot": snapshot,
        "token": token,
    })
    unique = {(item["device"], item["inode"]): item["size"] for item in snapshot["files"]}
    print(f"Permanent deletion plan: {bundle.name}")
    print(f"Files: {len(snapshot['files'])}; retained bytes: {readable_size(sum(unique.values()))}")
    for item in snapshot["files"]:
        print(f"  DELETE {item['path']}")
    print(f"Plan expires in 30 minutes. Apply only after user confirmation: media-cleanup apply {token} --confirm")


def apply_destroy(plan):
    bundle = find_bundle(plan["snapshot"]["bundle"])
    current = trash_snapshot(bundle)
    if digest(current) != plan.get("digest"):
        raise CleanupError("Quarantine contents changed after planning. Create a new deletion plan.")
    name = bundle.name
    shutil.rmtree(bundle)
    emit_event(f"Media cleanup quarantine permanently deleted: {name}")
    print(f"Permanently deleted quarantine bundle: {name}")


def self_test():
    assert readable_size(1024) == "1.0 KiB"
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    assert STATUS[6] == "seeding"
    print("Media cleanup unit tests passed.")


def cancel_plan():
    if PLAN_FILE.exists():
        PLAN_FILE.unlink()
        print("Cancelled the pending media cleanup plan.")
    else:
        print("No media cleanup plan is pending.")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list current Transmission media")
    plan = subparsers.add_parser("plan", help="create a guarded cleanup plan")
    plan.add_argument("reference", help="Transmission ID or torrent hash prefix")
    plan.add_argument(
        "--mode",
        choices=("remove-job", "archive", "delete"),
        default="delete",
        help="cleanup mode (default: delete permanently)",
    )
    path_delete = subparsers.add_parser("plan-path", help="plan permanent deletion of exact managed-media paths without a Transmission job")
    path_delete.add_argument("path", nargs="+", help="absolute path below a managed library, manual, or incomplete root")
    apply = subparsers.add_parser("apply", help="apply the current confirmed plan")
    apply.add_argument("token")
    apply.add_argument("--confirm", action="store_true", required=True)
    subparsers.add_parser("cancel", help="cancel the current pending plan")
    subparsers.add_parser("trash", help="list recoverable quarantine bundles")
    destroy = subparsers.add_parser("plan-destroy", help="plan permanent quarantine deletion")
    destroy.add_argument("bundle")
    subparsers.add_parser("self-test")
    return parser.parse_args()


def main():
    require_root()
    args = parse_args()
    if args.command == "list":
        show_list()
    elif args.command == "plan":
        create_plan(args.reference, args.mode)
    elif args.command == "plan-path":
        create_path_delete_plan(args.path)
    elif args.command == "apply":
        apply_plan(args.token)
    elif args.command == "cancel":
        cancel_plan()
    elif args.command == "trash":
        show_trash()
    elif args.command == "plan-destroy":
        create_destroy_plan(args.bundle)
    elif args.command == "self-test":
        self_test()


if __name__ == "__main__":
    try:
        main()
    except CleanupError as error:
        print(f"media-cleanup: {error}", file=sys.stderr)
        raise SystemExit(2)
