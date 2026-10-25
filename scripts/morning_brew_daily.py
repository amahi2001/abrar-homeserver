#!/usr/bin/env python3
"""Queue Morning Brew Daily's newest episode and expire only this job's downloads after 48h."""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PLAYLIST_ID = "PL5sjYkDs2Q-dEGxZMpzCV_lmiGUgJxL7N"
SCOPE = "telegram:8080066097:morning-brew-daily-cron"
QUEUE = "/run/current-system/sw/bin/youtube-queue"
LIBRARY = Path("/srv/data/media/library/youtube").resolve()
STATE = Path("/var/lib/youtube-queue/morning-brew-daily.json")
MAX_AGE = dt.timedelta(hours=48)
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Command failed").strip()[-2000:])
    return json.loads(result.stdout) if result.stdout.strip() else None


def queue_command(*args):
    return command(QUEUE, *args)


def feed_entries():
    url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={PLAYLIST_ID}"
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-MorningBrew/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        root = ET.fromstring(response.read())
    rows = []
    for node in root.findall("a:entry", NS):
        video = node.findtext("yt:videoId", namespaces=NS)
        title = node.findtext("a:title", namespaces=NS)
        published = node.findtext("a:published", namespaces=NS)
        if not video or not VIDEO_ID.fullmatch(video) or not title or not published:
            continue
        timestamp = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        rows.append({"id": video, "title": title, "published": timestamp.astimezone(dt.timezone.utc).isoformat()})
    if not rows:
        raise RuntimeError("Playlist feed returned no valid episodes; no downloads or deletions attempted.")
    return rows


def load_state():
    if not STATE.exists():
        return {}
    data = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("State file has invalid format; stopping safely.")
    return data


def save_state(data):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".morning-brew-", suffix=".tmp", dir=STATE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, STATE)
        os.chmod(STATE, 0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def jellyfin(method, path, params=None):
    config = json.loads(Path("/etc/youtube-queue.json").read_text(encoding="utf-8"))
    key = Path(config["jellyfinKeyFile"]).read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("Jellyfin API key is empty; refusing expiry operation.")
    url = config["jellyfinUrl"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method=method, headers={"X-Emby-Token": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"Jellyfin API returned HTTP {error.code}; download retained.") from error


def managed_path(job, video_id):
    path = Path(job.get("path") or "").resolve()
    if path.parent.parent != LIBRARY or path.name != "video.mkv" or not path.parent.name.endswith(f" [{video_id}]"):
        raise RuntimeError(f"Refusing unexpected library path for {video_id}.")
    return path


def expire(video_id, record, job, dry_run):
    if not job or job.get("status") != "ready" or not job.get("item_id") or not job.get("path"):
        return False, f"expired {video_id} but queue item is not ready; will retry"
    path = managed_path(job, video_id)
    if not path.is_file():
        return False, f"expired {video_id} but expected video file is missing; retained state for review"
    item_id = job["item_id"]
    result = jellyfin("GET", "/Items", {"Ids": item_id, "Fields": "Path"})
    items = (result or {}).get("Items", [])
    if len(items) != 1 or items[0].get("Id") != item_id or Path(items[0].get("Path", "")).resolve() != path:
        raise RuntimeError(f"Jellyfin path does not match queued file for {video_id}; refusing deletion.")
    if dry_run:
        return True, f"would delete expired: {record.get('title', video_id)}"
    jellyfin("DELETE", f"/Items/{urllib.parse.quote(item_id, safe='')}")
    if path.exists():
        raise RuntimeError(f"Jellyfin delete did not remove {path}; state retained for retry.")
    return True, f"deleted expired: {record.get('title', video_id)}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            raise RuntimeError("sudo not available; cannot access protected YouTube queue state.")
        os.execvp(sudo, [sudo, "-n", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])

    now = dt.datetime.now(dt.timezone.utc)
    entries = feed_entries()
    latest = entries[0]
    recent = {item["id"]: item for item in entries}
    latest_age = now - dt.datetime.fromisoformat(latest["published"])
    if latest_age < dt.timedelta(0):
        raise RuntimeError("Latest episode publication time is in the future; stopping safely.")

    # Confirm playlist membership/channel through the supported queue preview before queueing.
    video_url = f"https://www.youtube.com/watch?v={latest['id']}"
    check = queue_command("--scope", SCOPE, "preview", video_url)
    detail = (check.get("entries") or [{}])[0]
    if detail.get("id") != latest["id"] or detail.get("channel") != "Morning Brew Daily":
        raise RuntimeError("Latest RSS entry did not match the Morning Brew Daily video preview.")

    state = load_state()
    scoped_jobs = queue_command("--scope", SCOPE, "status") or []
    global_jobs = queue_command("status") or []
    scoped_by_id = {job["id"]: job for job in scoped_jobs}
    global_by_id = {job["id"]: job for job in global_jobs}

    # Recover state after interruption: this cron's scope plus recent RSS dates prove ownership/date.
    for video_id, job in scoped_by_id.items():
        if video_id in recent and video_id not in state:
            state[video_id] = {"published": recent[video_id]["published"], "title": recent[video_id]["title"]}

    messages = []
    latest_date = dt.datetime.fromisoformat(latest["published"])
    if latest_age <= MAX_AGE:
        if latest["id"] in scoped_by_id:
            state.setdefault(latest["id"], {"published": latest["published"], "title": latest["title"]})
        elif latest["id"] not in global_by_id and not list(LIBRARY.glob(f"* [{latest['id']}]")):
            if args.dry_run:
                messages.append(f"would queue: {latest['title']}")
            else:
                result = queue_command("--scope", SCOPE, "queue", "--url", video_url)
                job = next((x for x in result.get("jobs", []) if x.get("id") == latest["id"]), None)
                if job and job.get("status") in {"queued", "downloading", "awaiting-index"}:
                    state[latest["id"]] = {"published": latest["published"], "title": latest["title"]}
                    messages.append(f"queued: {latest['title']}")
        # Existing downloads outside this cron's scope are never added to expiry state.

    for video_id, record in list(state.items()):
        try:
            published = dt.datetime.fromisoformat(record["published"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(f"Invalid publication timestamp for tracked video {video_id}; stopping safely.")
        if now - published <= MAX_AGE:
            continue
        removed, message = expire(video_id, record, scoped_by_id.get(video_id), args.dry_run)
        if removed:
            state.pop(video_id, None)
        messages.append(message)

    if not args.dry_run:
        save_state(state)
    if messages:
        print("Morning Brew Daily: " + "; ".join(messages))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Morning Brew Daily automation failed safely: {error}", file=sys.stderr)
        sys.exit(1)
