#!/usr/bin/env python3
"""Persistent direct YouTube downloads published to Jellyfin."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

STATE = Path("/var/lib/youtube-queue")
STAGING = Path("/srv/data/media/.youtube-incomplete")
LIBRARY = Path("/srv/data/media/library/youtube")
CONFIG = Path("/etc/youtube-queue.json")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
TERMINAL = {"ready", "failed"}
RESERVE = 5 * 1024**3


class QueueError(RuntimeError):
    pass


class Paused(QueueError):
    pass


def settings():
    return json.loads(CONFIG.read_text())


def clean(value, limit=500):
    text = re.sub(r"https?://\S+", "[URL]", str(value))
    return " ".join(text.split())[-limit:]


def video_url(video_id):
    if not VIDEO_ID.fullmatch(video_id):
        raise QueueError("Invalid YouTube video ID.")
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_url(value):
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise QueueError("Invalid YouTube link.") from error
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password or port not in (None, 80, 443):
        raise QueueError("Send a public YouTube video or playlist link.")
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
        raise QueueError("Only YouTube links are supported.")
    query = parse_qs(parsed.query)
    parts = parsed.path.strip("/").split("/")
    video_id = None
    if host.endswith("youtu.be"):
        video_id = parts[0]
    elif parts[0] in ("shorts", "embed", "live") and len(parts) == 2:
        video_id = parts[1]
    elif parsed.path.rstrip("/") == "/watch":
        video_id = query.get("v", [None])[0]
    if video_id:
        return "video", video_url(video_id)
    playlist = query.get("list", [""])[0]
    if parsed.path.rstrip("/") == "/playlist" and re.fullmatch(r"[A-Za-z0-9_-]{10,150}", playlist):
        return "playlist", "https://www.youtube.com/playlist?" + urlencode({"list": playlist})
    raise QueueError("Send a video or playlist link; channel subscriptions and live recording are not supported.")


def scope_default():
    platform = os.environ.get("HERMES_SESSION_PLATFORM", "")
    chat = os.environ.get("HERMES_SESSION_CHAT_ID", "")
    thread = os.environ.get("HERMES_SESSION_THREAD_ID", "")
    if platform and chat:
        return f"{platform}:{chat}:{thread}"
    return os.environ.get("HERMES_SESSION_KEY") or os.environ.get("HERMES_SESSION_ID")


def require_scope(scope):
    if not scope or len(scope) > 250:
        raise QueueError("Supply --scope with the current conversation ID.")
    return scope


@contextlib.contextmanager
def database():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / "queue.db", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
            created REAL NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, metadata TEXT NOT NULL,
            height INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', path TEXT, item_id TEXT,
            created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, title TEXT NOT NULL,
            ids TEXT NOT NULL, skipped INTEGER NOT NULL, playlist INTEGER NOT NULL,
            created REAL NOT NULL, notification TEXT NOT NULL DEFAULT ''
        );
    """)
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def update_job(video_id, **values):
    values["updated"] = time.time()
    columns = ", ".join(f"{key}=?" for key in values)
    with database() as db:
        db.execute(f"UPDATE jobs SET {columns} WHERE id=?", [*values.values(), video_id])


def run(args, timeout=30):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise QueueError(clean(result.stderr or result.stdout or "Command failed."))
    return result.stdout.strip()


def ytdlp(args, stage=None, timeout=180):
    config = settings()
    account = pwd.getpwnam("youtube-download")
    command = [config["ytDlp"], "--ignore-config", "--no-plugin-dirs", "--no-colors",
               "--js-runtimes", "node", "--socket-timeout", "30", *args]
    with tempfile.TemporaryDirectory(prefix="youtube-queue-") as temporary_home, \
         tempfile.TemporaryFile(mode="w+") as output, tempfile.TemporaryFile(mode="w+") as errors:
        os.chown(temporary_home, account.pw_uid, account.pw_gid)
        # Keep gateway credentials out of the unprivileged downloader's environment.
        environment = {"PATH": os.defpath, "HOME": temporary_home, "TMPDIR": temporary_home,
                       "LANG": "C.UTF-8", "SSL_CERT_FILE": "/etc/ssl/certs/ca-bundle.crt"}
        process = subprocess.Popen(command, stdout=output, stderr=errors, env=environment,
                                   cwd=stage or temporary_home, user=account.pw_uid,
                                   group=account.pw_gid, extra_groups=[], start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise QueueError("YouTube operation timed out; retry the request.")
                if stage and shutil.disk_usage(stage).free <= config.get("reserveBytes", RESERVE):
                    raise Paused("Low disk space; retaining the partial download until space is available.")
                time.sleep(1)
            if process.returncode:
                errors.seek(0)
                raise QueueError(clean(errors.read()[-10000:]) or "YouTube download failed.")
            output.seek(0)
            return output.read(32 * 1024 * 1024)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)


def entry(item):
    if not item or not VIDEO_ID.fullmatch(str(item.get("id", ""))):
        return None
    return {key: item.get(key) for key in (
        "id", "title", "channel", "uploader", "duration", "upload_date", "description",
        "availability", "live_status", "is_live", "thumbnail")}


def extract(url, flat=False):
    args = ["--dump-single-json", "--skip-download", "--ignore-errors"]
    args += ["--flat-playlist"] if flat else ["--no-playlist"]
    try:
        result = json.loads(ytdlp([*args, "--", url]))
    except json.JSONDecodeError as error:
        raise QueueError("YouTube returned no usable video information.") from error
    if not isinstance(result, dict):
        raise QueueError("YouTube returned no usable video information.")
    return result


def snapshot(scope, kind, title, entries):
    identifier = uuid.uuid4().hex[:12]
    payload = {"title": title, "entries": entries}
    with database() as db:
        db.execute("DELETE FROM snapshots WHERE created<?", (time.time() - 7 * 86400,))
        db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                   (identifier, require_scope(scope), kind, time.time(), json.dumps(payload)))
    return {"snapshot": identifier, "kind": kind, **payload}


def search(query, scope):
    if not query.strip() or len(query) > 500:
        raise QueueError("Supply a video description under 500 characters.")
    data = extract("ytsearch5:" + query, flat=True)
    entries = [entry(item) for item in data.get("entries", [])]
    return snapshot(scope, "search", query, [item for item in entries if item])


def preview(url, scope):
    kind, canonical = normalize_url(url)
    data = extract(canonical, flat=(kind == "playlist"))
    entries = [entry(item) for item in data.get("entries", [])] if kind == "playlist" else [entry(data)]
    result = snapshot(scope, kind, data.get("title") or "YouTube", entries)
    result["video_count"] = len(entries)
    durations = [item.get("duration") if item else None for item in entries]
    result["known_duration_seconds"] = sum(value for value in durations if isinstance(value, (int, float)))
    result["unknown_durations"] = sum(not isinstance(value, (int, float)) for value in durations)
    return result


def queue(scope, url=None, snapshot_id=None, indices=None, confirm=False, height=1080):
    require_scope(scope)
    playlist = False
    if snapshot_id:
        with database() as db:
            saved = db.execute("SELECT * FROM snapshots WHERE id=? AND scope=?", (snapshot_id, scope)).fetchone()
        if not saved or saved["created"] < time.time() - 86400:
            raise QueueError("Preview expired or belongs to another conversation; search or preview again.")
        payload = json.loads(saved["payload"])
        playlist = saved["kind"] == "playlist"
        if playlist:
            if not confirm or indices:
                raise QueueError("A playlist requires approval of the full preview and --confirm-playlist.")
            entries = payload["entries"]
        elif saved["kind"] == "search":
            if not indices or any(index < 1 or index > len(payload["entries"]) for index in indices):
                raise QueueError("Select valid result numbers from the preceding search.")
            entries = [payload["entries"][index - 1] for index in dict.fromkeys(indices)]
        else:
            entries = payload["entries"]
        title = payload["title"]
    elif url:
        kind, canonical = normalize_url(url)
        if kind == "playlist":
            raise QueueError("Preview the playlist first, then confirm its snapshot.")
        video_id = parse_qs(urlparse(canonical).query)["v"][0]
        entries, title = [{"id": video_id, "title": video_id}], video_id
    else:
        raise QueueError("Supply a YouTube URL or a saved search/playlist snapshot.")
    valid = {item["id"]: item for item in entries if item}
    if not valid:
        raise QueueError("No downloadable videos were found in this preview.")
    now = time.time()
    identifier = uuid.uuid4().hex[:12]
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        for video_id, item in valid.items():
            video_url(video_id)
            db.execute("INSERT OR IGNORE INTO jobs (id,title,metadata,height,created,updated) VALUES (?,?,?,?,?,?)",
                       (video_id, item.get("title") or video_id, json.dumps(item), height, now, now))
        db.execute("INSERT INTO requests (id,scope,title,ids,skipped,playlist,created) VALUES (?,?,?,?,?,?,?)",
                   (identifier, scope, title, json.dumps(list(valid)), len(entries) - len(valid), playlist, now))
        jobs = [dict(db.execute("SELECT id,title,status,height,item_id,error FROM jobs WHERE id=?", (video_id,)).fetchone())
                for video_id in valid]
    subprocess.run(["systemctl", "start", "--no-block", "youtube-queue-worker.service"],
                   capture_output=True, check=False)
    return {"request": identifier, "title": title, "jobs": jobs,
            "note": "Existing videos keep their current quality; failed jobs require retry."}


def jellyfin(method, path, data=None, query=None):
    config = settings()
    try:
        key = Path(config["jellyfinKeyFile"]).read_text().strip()
        if not key:
            raise OSError("empty key")
        url = config["jellyfinUrl"].rstrip("/") + path
        if query:
            url += "?" + urlencode(query)
        request = Request(url, method=method, data=json.dumps(data).encode() if data is not None else None,
                          headers={"X-Emby-Token": key, "Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except (OSError, HTTPError, URLError, ValueError) as error:
        raise QueueError("Jellyfin API unavailable; check the local service and protected API credential.") from error


def setup_library():
    libraries = jellyfin("GET", "/Library/VirtualFolders")
    existing = [item for item in libraries if str(LIBRARY) in item.get("Locations", [])]
    if existing:
        if existing[0].get("CollectionType") != "movies":
            raise QueueError("The YouTube path already uses a different library type; review it before setup.")
        return {"library": existing[0]["Name"], "status": "already configured"}
    if any(item.get("Name") == "YouTube" for item in libraries):
        raise QueueError("A YouTube library already points elsewhere; review it before setup.")
    jellyfin("POST", "/Library/VirtualFolders", {
        "LibraryOptions": {"Enabled": True, "EnableRealtimeMonitor": True, "EnableEmbeddedTitles": True,
                           "SaveLocalMetadata": False, "EnableChapterImageExtraction": False,
                           "EnableTrickplayImageExtraction": False,
                           "PathInfos": [{"Path": str(LIBRARY)}],
                           "TypeOptions": [{"Type": "Movie", "MetadataFetchers": [],
                                            "ImageFetchers": [], "ImageFetcherOrder": [], "MetadataFetcherOrder": []}]}
    }, {"name": "YouTube", "collectionType": "movies", "refreshLibrary": "true"})
    return {"library": "YouTube", "status": "created", "path": str(LIBRARY)}


def indexed_item(path):
    libraries = jellyfin("GET", "/Library/VirtualFolders")
    library = next((item for item in libraries if str(LIBRARY) in item.get("Locations", [])), None)
    if not library:
        raise QueueError("YouTube library is not configured; run sudo youtube-queue setup.")
    offset = 0
    while True:
        result = jellyfin("GET", "/Items", query={"ParentId": library["ItemId"], "Recursive": "true",
                                                   "Fields": "Path", "IncludeItemTypes": "Movie",
                                                   "StartIndex": offset, "Limit": 200})
        items = result.get("Items", [])
        item = next((item for item in items if item.get("Path") == str(path)), None)
        if item or not items or offset + len(items) >= result.get("TotalRecordCount", len(items)):
            return item
        offset += len(items)


def metadata_files(stage, metadata):
    movie = ET.Element("movie")
    fields = {"title": metadata.get("title"), "studio": metadata.get("channel") or metadata.get("uploader"),
              "plot": (metadata.get("description") or "") + "\n\n" + video_url(metadata["id"]),
              "dateadded": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    date = str(metadata.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", date):
        fields.update(year=date[:4], premiered=f"{date[:4]}-{date[4:6]}-{date[6:]}")
    for key, value in fields.items():
        if value:
            ET.SubElement(movie, key).text = str(value)
    ET.SubElement(movie, "uniqueid", {"type": "youtube", "default": "true"}).text = metadata["id"]
    ET.ElementTree(movie).write(stage / "movie.nfo", encoding="utf-8", xml_declaration=True)
    thumbnail = stage / "video.jpg"
    if thumbnail.exists():
        thumbnail.replace(stage / "poster.jpg")
    # yt-dlp's full info JSON includes temporary signed stream URLs; publish only curated NFO metadata.
    for file in stage.glob("*.info.json"):
        file.unlink()


def safe_name(title, video_id):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .")
    # Leave room for the stable ID within filesystem byte limits, including Unicode titles.
    name = name.encode("utf-8")[:170].decode("utf-8", "ignore") or "YouTube"
    return f"{name} [{video_id}]"


def verify_media(path):
    try:
        info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
        return any(stream.get("codec_type") == "video" for stream in info.get("streams", [])) and float(info["format"].get("duration", 0)) > 0
    except (QueueError, KeyError, ValueError, subprocess.TimeoutExpired):
        return False


def download(job):
    metadata = entry(extract(video_url(job["id"])))
    if not metadata:
        raise QueueError("Video is unavailable.")
    if metadata.get("is_live") or metadata.get("live_status") in ("is_live", "is_upcoming"):
        raise QueueError("Active livestreams and upcoming premieres are not supported; send the finished recording later.")
    update_job(job["id"], title=metadata.get("title") or job["id"], metadata=json.dumps(metadata))
    stage = STAGING / job["id"]
    stage.mkdir(parents=True, mode=0o2770, exist_ok=True)
    account = pwd.getpwnam("youtube-download")
    os.chown(stage, account.pw_uid, account.pw_gid)
    os.chmod(stage, 0o2770)
    if shutil.disk_usage(stage).free <= settings().get("reserveBytes", RESERVE):
        raise Paused("Low disk space; waiting for at least 5 GiB free.")
    height = job["height"]
    # Caption/thumbnail failures are optional. A usable video.mkv and FFprobe
    # validation below remain mandatory even when yt-dlp ignores sidecar errors.
    ytdlp(["--no-playlist", "--no-progress", "--ignore-errors", "--retries", "2", "--fragment-retries", "2",
           "--continue", "--no-overwrites", "--write-info-json", "--write-thumbnail", "--convert-thumbnails", "jpg",
           "--write-subs", "--write-auto-subs", "--sub-langs", "en,en-orig,en-US,en-GB", "--sub-format", "srt/vtt/best",
           "--convert-subs", "srt", "--embed-metadata", "--embed-chapters", "--no-embed-info-json",
           "--merge-output-format", "mkv", "--remux-video", "mkv", "--format",
           f"bv*[height<={height}]+ba/b[height<={height}]", "--format-sort", "res,vcodec:h264,acodec:aac",
           "--output", "video.%(ext)s", "--", video_url(job["id"])], stage=stage, timeout=6 * 3600)
    media = stage / "video.mkv"
    if not media.exists() or not verify_media(media):
        raise QueueError("The completed file failed FFprobe validation; it has not been published.")
    metadata_files(stage, metadata)
    final = LIBRARY / safe_name(metadata.get("title") or job["id"], job["id"])
    # Persist the destination before rename so a crash between publication and DB update is recoverable.
    update_job(job["id"], path=str(final / "video.mkv"))
    for path in stage.iterdir():
        if path.is_symlink() or not path.is_file():
            raise QueueError("Unexpected download output; publication stopped.")
        if path.name not in {"video.mkv", "movie.nfo", "poster.jpg"} and not (
                path.name.startswith("video.") and path.suffix == ".srt"):
            # A changed format after interruption can leave old fragments behind.
            # They are no longer needed once the merged video has been validated.
            path.unlink()
            continue
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, 0o660)
    if final.exists():
        raise QueueError("A published directory already exists; review it before retrying.")
    stage.rename(final)
    update_job(job["id"], status="awaiting-index", error="", next_attempt=0)
    try:
        jellyfin("POST", "/Library/Media/Updated", {"Updates": [{"Path": str(final), "UpdateType": "Created"}]})
    except QueueError:
        pass  # Realtime monitoring and the next worker pass retry indexing independently.


def process_pending_index():
    with database() as db:
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE status='awaiting-index'")]
    for job in jobs:
        path = Path(job["path"])
        if not path.is_file():
            update_job(job["id"], status="failed", error="Published file is missing; review the library before retrying.")
            continue
        try:
            item = indexed_item(path)
            if item:
                update_job(job["id"], status="ready", item_id=item["Id"], error="")
            else:
                if time.time() - job["updated"] >= 60 and job["next_attempt"] == 0:
                    # Jellyfin skips an empty library's physical root during
                    # validation, so its first file has no inotify watch yet.
                    # One validation pass registers it. Normal imports use the
                    # realtime monitor; never loop global scans while waiting.
                    jellyfin("POST", "/Library/Refresh")
                    update_job(job["id"], error="Downloaded, awaiting Jellyfin library scan.", next_attempt=time.time())
        except QueueError as error:
            if job["error"] != str(error):
                update_job(job["id"], error=str(error))


def worker():
    with (STATE / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Resume downloads interrupted by a reboot or service restart.
        with database() as db:
            interrupted = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE status='downloading'")]
        for job in interrupted:
            if job["path"] and Path(job["path"]).is_file() and verify_media(Path(job["path"])):
                update_job(job["id"], status="awaiting-index")
            else:
                update_job(job["id"], status="queued")
        process_pending_index()
        with database() as db:
            jobs = [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','paused') AND next_attempt<=? ORDER BY created", (time.time(),))]
        for job in jobs:
            try:
                if shutil.disk_usage(STAGING).free <= settings().get("reserveBytes", RESERVE):
                    raise Paused("Low disk space; waiting for at least 5 GiB free.")
                update_job(job["id"], status="downloading", error="")
                download(job)
            except Paused as error:
                update_job(job["id"], status="paused", error=str(error), next_attempt=time.time() + 60)
                break
            except (QueueError, OSError, subprocess.SubprocessError) as error:
                attempts = job["attempts"] + 1
                detail = clean(error)
                permanent = any(text in detail.lower() for text in (
                    "private video", "unavailable", "removed", "sign in", "not a bot", "not supported",
                    "already exists", "validation", "not available", "unexpected download"))
                status = "failed" if permanent or attempts >= 3 else "queued"
                update_job(job["id"], status=status, attempts=attempts, error=detail,
                           next_attempt=time.time() + 60 * 2**attempts)
            process_pending_index()


def retry(video_id):
    with database() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (video_id,)).fetchone()
        if not job:
            raise QueueError("Unknown video ID.")
        if job["status"] not in ("failed", "paused"):
            raise QueueError("Only failed or paused jobs can be retried.")
        if job["path"] and Path(job["path"]).exists():
            raise QueueError("Published media already exists; investigate its indexing instead of downloading again.")
        db.execute("UPDATE jobs SET status='queued',attempts=0,next_attempt=0,error='',updated=? WHERE id=?", (time.time(), video_id))
        db.execute("UPDATE requests SET notification='' WHERE EXISTS (SELECT 1 FROM json_each(requests.ids) WHERE value=?)", (video_id,))
    subprocess.run(["systemctl", "start", "--no-block", "youtube-queue-worker.service"], capture_output=True, check=False)
    return {"id": video_id, "status": "queued"}


def show_status(scope=None):
    with database() as db:
        if scope:
            rows = db.execute("""SELECT id,title,status,height,attempts,error,path,item_id FROM jobs
                WHERE id IN (SELECT value FROM requests,json_each(requests.ids) WHERE requests.scope=?)
                ORDER BY created DESC LIMIT 100""", (scope,)).fetchall()
        else:
            rows = db.execute("SELECT id,title,status,height,attempts,error,path,item_id FROM jobs ORDER BY created DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]


def watch():
    notifications = []
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        for request in db.execute("SELECT * FROM requests ORDER BY created"):
            jobs = [db.execute("SELECT * FROM jobs WHERE id=?", (video_id,)).fetchone() for video_id in json.loads(request["ids"])]
            counts = {status: sum(job["status"] == status for job in jobs) for status in (
                "ready", "failed", "paused", "queued", "downloading", "awaiting-index")}
            finished = all(job["status"] in TERMINAL for job in jobs)
            problems = [job for job in jobs if job["status"] in ("failed", "paused") or
                        (job["status"] == "awaiting-index" and time.time() - job["created"] > 900)]
            if not finished and not problems:
                continue
            problem_ids = {job["id"] for job in problems}
            signature = hashlib.sha256(json.dumps([
                (job["id"], job["status"], job["error"]) for job in jobs
                if job["status"] in TERMINAL or job["id"] in problem_ids
            ]).encode()).hexdigest()
            if signature == request["notification"]:
                continue
            if request["playlist"]:
                message = (f"YouTube playlist: {request['title']} — {counts['ready']} ready in Jellyfin, "
                           f"{request['skipped']} skipped, {counts['failed']} failed")
                if not finished:
                    message += f", {len(jobs) - counts['ready'] - counts['failed']} pending"
                message += "."
                for job in problems:
                    message += f"\n{job['title']} [{job['id']}]: {job['error'] or job['status']}"
                message += "\n" + settings()["publicJellyfinUrl"]
            elif len(jobs) == 1 and jobs[0]["status"] == "ready":
                job = jobs[0]
                message = f"Ready in Jellyfin: {job['title']}\n" + settings()["publicJellyfinUrl"] + "/web/#/details?id=" + job["item_id"]
            else:
                message = "YouTube request: " + request["title"]
                for job in jobs:
                    message += f"\n{job['title']} [{job['id']}]: {job['error'] or job['status']}"
                    if job["status"] == "failed":
                        message += f". Send Hermes a new message: Retry YouTube {job['id']}"
            notifications.append((request["id"], signature, message))
        if notifications:
            print("\n\n".join(message for _, _, message in notifications), flush=True)
            db.executemany("UPDATE requests SET notification=? WHERE id=?", [(signature, identifier) for identifier, signature, _ in notifications])


def main():
    os.umask(0o077)
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default=scope_default(), help="Stable current conversation ID")
    sub = parser.add_subparsers(dest="command", required=True)
    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    preview_parser = sub.add_parser("preview")
    preview_parser.add_argument("url")
    queue_parser = sub.add_parser("queue")
    source = queue_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--snapshot")
    queue_parser.add_argument("--index", type=int, nargs="+")
    queue_parser.add_argument("--confirm-playlist", action="store_true")
    queue_parser.add_argument("--height", choices=(720, 1080, 1440, 2160, 4320), type=int, default=1080)
    sub.add_parser("status")
    retry_parser = sub.add_parser("retry")
    retry_parser.add_argument("video_id")
    for command in ("health", "setup", "worker", "watch"):
        sub.add_parser(command)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise QueueError("Run this command through sudo youtube-queue.")
        STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
        if args.command == "search":
            result = search(args.query, require_scope(args.scope))
        elif args.command == "preview":
            result = preview(args.url, require_scope(args.scope))
        elif args.command == "queue":
            result = queue(args.scope, args.url, args.snapshot, args.index, args.confirm_playlist, args.height)
        elif args.command == "status":
            result = show_status(args.scope)
        elif args.command == "retry":
            result = retry(args.video_id)
        elif args.command == "setup":
            result = setup_library()
        elif args.command == "health":
            version = ytdlp(["--version"], timeout=30).strip()
            libraries = jellyfin("GET", "/Library/VirtualFolders")
            if not any(str(LIBRARY) in item.get("Locations", []) for item in libraries):
                raise QueueError("YouTube library has not been configured; run youtube-queue setup.")
            result = {"downloader": version, "jellyfin": "available", "library": str(LIBRARY)}
        elif args.command == "worker":
            worker()
            return
        else:
            watch()
            return
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (QueueError, OSError, subprocess.SubprocessError) as error:
        print(f"YouTube queue: {clean(error)}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
