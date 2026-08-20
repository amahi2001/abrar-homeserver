#!/usr/bin/env python3
"""Safely search Prowlarr and queue selected results in VPN-isolated Transmission."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

PROWLARR_CONFIG = Path("/var/lib/prowlarr/config.xml")
PROWLARR_API = "http://127.0.0.1:9696/api/v1"
PROWLARR_DOCKER_GATEWAY = "172.17.0.1:9696"
TRANSMISSION_DOWNLOAD_DIR = "/downloads/library/manual"
TEMP_TORRENT = "/tmp/media-queue.torrent"
WATCH_STATE = Path("/var/lib/media-queue/watch-state.json")
SEARCH_CACHE = Path("/var/lib/media-queue/search-cache.json")
ORGANIZER_EVENT_DIR = Path("/var/lib/media-queue/events")
TRANSMISSION = ["docker", "exec", "transmission-vpn", "transmission-remote", "127.0.0.1:9091"]


class QueueError(RuntimeError):
    pass


def command(args, *, quiet=False):
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        raise QueueError("A protected media pipeline command failed.")
    return completed.stdout.strip() if quiet else completed.stdout


def require_root():
    if os.geteuid() != 0:
        raise QueueError("Run this command as: sudo media-queue …")


def prowlarr_key():
    try:
        contents = PROWLARR_CONFIG.read_text(encoding="utf-8")
    except OSError as error:
        raise QueueError("Cannot read the local Prowlarr configuration.") from error
    match = re.search(r"<ApiKey>([^<]+)</ApiKey>", contents)
    if not match:
        raise QueueError("Prowlarr API key is unavailable.")
    return match.group(1)


def api_get(path, query=None):
    url = f"{PROWLARR_API}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    request = Request(url, headers={
        "X-Api-Key": prowlarr_key(),
        # Make returned download URLs reachable from Transmission's Docker
        # namespace; this is private Docker bridge traffic, not a LAN service.
        "Host": PROWLARR_DOCKER_GATEWAY,
    })
    try:
        with urlopen(request, timeout=45) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise QueueError("Prowlarr search is unavailable.") from error


def network_guardrails():
    health = command(["docker", "inspect", "-f", "{{.State.Health.Status}}", "gluetun"], quiet=True)
    if health != "healthy":
        raise QueueError("VPN guardrail failed: Gluetun is not healthy.")
    network_mode = command(["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", "transmission-vpn"], quiet=True)
    if not network_mode.startswith("container:"):
        raise QueueError("VPN guardrail failed: Transmission is not sharing Gluetun's network namespace.")
    command([
        "docker", "exec", "gluetun", "sh", "-ceu",
        "test -d /sys/class/net/tun0 && iptables -S OUTPUT | grep -qx -- '-P OUTPUT DROP'",
    ])


def search_results(query, limit):
    data = api_get("/search", {
        "query": query,
        "type": "search",
        "limit": max(limit, 1),
        "offset": 0,
    })
    if not isinstance(data, list):
        raise QueueError("Prowlarr returned an unexpected search response.")
    # Some indexers, notably Nyaa.si, expose a magnetUrl rather than a
    # Prowlarr downloadUrl. Keep both forms; queue_results validates and adds
    # either one inside Transmission's Gluetun namespace.
    return [
        item for item in data
        if item.get("protocol") == "torrent"
        and (item.get("downloadUrl") or item.get("magnetUrl"))
    ]


def readable_size(value):
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "unknown"


def print_results(results):
    if not results:
        print("No torrent results found.")
        return
    for number, result in enumerate(results, start=1):
        print(
            f"{number}. {result.get('title', 'Untitled')}\n"
            f"   source={result.get('indexer', 'unknown')} "
            f"size={readable_size(result.get('size'))} "
            f"seeders={result.get('seeders', '?')} "
            f"leechers={result.get('leechers', '?')}"
        )


def save_private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def cache_search_results(query, results):
    # Download URLs are intentionally retained only in this root-owned cache so
    # result numbers remain stable between the search and the user's selection.
    save_private_json(SEARCH_CACHE, {
        "query": query,
        "created_at": time.time(),
        "results": results,
    })


def selected_results(query, indices):
    try:
        cached = json.loads(SEARCH_CACHE.read_text(encoding="utf-8"))
        created_at = float(cached["created_at"])
        results = cached["results"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise QueueError("Search first, then choose the displayed result numbers.") from error
    if cached.get("query") != query or time.time() - created_at > 30 * 60 or not isinstance(results, list):
        raise QueueError("Those search results have expired. Search again and choose displayed result numbers.")
    invalid = [str(index) for index in indices if index < 1 or index > len(results)]
    if invalid:
        raise QueueError(
            "These result numbers are no longer available: " + ", ".join(invalid) + ". Search again and choose displayed numbers."
        )
    # Preserve the user's order while avoiding an accidental duplicate queue.
    seen = set()
    return [results[index - 1] for index in indices if not (index in seen or seen.add(index))]


def validate_download_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.netloc != PROWLARR_DOCKER_GATEWAY:
        raise QueueError("Prowlarr did not return a private Docker-bridge download URL.")


def normalize_prowlarr_download_url(value):
    # Prowlarr may return localhost in magnetUrl; Transmission reaches it
    # through the Docker bridge gateway inside the protected namespace.
    if value.startswith("http://127.0.0.1:9696"):
        value = "http://" + PROWLARR_DOCKER_GATEWAY + value[len("http://127.0.0.1:9696"): ]
    validate_download_url(value)
    return value


def validate_magnet_uri(value):
    parsed = urlparse(value)
    if parsed.scheme != "magnet" or not re.search(r"(?:^|&)xt=urn%3Abtih%3A|(?:^|&)xt=urn:btih:", parsed.query, re.IGNORECASE):
        raise QueueError("Prowlarr returned an invalid magnet link.")


def magnet_from_download_redirect(download_url):
    """Resolve a Prowlarr magnet redirect from inside Gluetun.

    BusyBox wget follows redirects automatically and cannot directly add a
    magnet URI. Its response headers still contain the Location value, which
    we capture without ever exposing it to the caller or logs.
    """
    completed = subprocess.run([
        "docker", "exec", "-e", f"MEDIA_QUEUE_DOWNLOAD_URL={download_url}",
        "transmission-vpn", "sh", "-ceu",
        'wget -S --timeout=30 --tries=1 -O /dev/null "$MEDIA_QUEUE_DOWNLOAD_URL"',
    ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    match = re.search(r"(?mi)^\s*Location:\s*(magnet:\?\S+)\s*$", completed.stderr)
    if not match:
        return None
    magnet = match.group(1)
    validate_magnet_uri(magnet)
    return magnet


def transmission_torrents():
    raw = command(TRANSMISSION + ["--json", "--list"], quiet=True)
    try:
        torrents = json.loads(raw)["result"]["torrents"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise QueueError("Transmission returned an unexpected queue response.") from error
    if not isinstance(torrents, list):
        raise QueueError("Transmission returned an unexpected queue response.")
    return torrents


def load_watch_state():
    try:
        state = json.loads(WATCH_STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"watched": {}, "pipeline": "healthy"}
    except (OSError, json.JSONDecodeError):
        return {"watched": {}, "pipeline": "healthy"}
    if not isinstance(state, dict):
        return {"watched": {}, "pipeline": "healthy"}
    state.setdefault("watched", {})
    state.setdefault("pipeline", "healthy")
    if not isinstance(state["watched"], dict):
        state["watched"] = {}
    return state


def save_watch_state(state):
    save_private_json(WATCH_STATE, state)


def record_watched_torrents(before_ids, results):
    after = transmission_torrents()
    new_torrents = [torrent for torrent in after if torrent.get("id") not in before_ids]
    if not new_torrents:
        return 0
    state = load_watch_state()
    for torrent in new_torrents:
        torrent_id = str(torrent["id"])
        state["watched"][torrent_id] = {
            "name": torrent.get("name", "queued media"),
            "state": None,
        }
    save_watch_state(state)
    return len(new_torrents)


def queue_results(results, dry_run):
    network_guardrails()
    for result in results:
        if result.get("downloadUrl"):
            validate_download_url(result["downloadUrl"])
        elif result.get("magnetUrl"):
            if result["magnetUrl"].startswith("magnet:"):
                validate_magnet_uri(result["magnetUrl"])
            else:
                normalize_prowlarr_download_url(result["magnetUrl"])
        else:
            raise QueueError("The selected indexer did not provide a usable torrent or magnet link.")
    if dry_run:
        for result in results:
            print(f"Guardrails passed. Would queue: {result.get('title', 'selected result')}")
        return
    before_ids = {torrent.get("id") for torrent in transmission_torrents()}
    try:
        for result in results:
            if result.get("magnetUrl") and not result.get("downloadUrl"):
                download_url = normalize_prowlarr_download_url(result["magnetUrl"])
            else:
                download_url = result["downloadUrl"]
            descriptor = subprocess.run([
                "docker", "exec", "-e", f"MEDIA_QUEUE_DOWNLOAD_URL={download_url}",
                "transmission-vpn", "sh", "-ceu",
                f"umask 077; rm -f {TEMP_TORRENT}; "
                f"wget -q --timeout=30 --tries=1 -O {TEMP_TORRENT} \"$MEDIA_QUEUE_DOWNLOAD_URL\"; "
                f"test -s {TEMP_TORRENT}",
            ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if descriptor.returncode == 0:
                add_source = TEMP_TORRENT
            else:
                # Some indexers return a Prowlarr 30x response whose Location
                # is a magnet URI rather than a torrent descriptor. Resolve it
                # in the same VPN-isolated namespace, then give it directly to
                # Transmission in that namespace.
                add_source = magnet_from_download_redirect(download_url)
                if add_source is None:
                    raise QueueError("The selected indexer did not provide a usable torrent or magnet link.")
            command(TRANSMISSION + [
                "--add", add_source, "--download-dir", TRANSMISSION_DOWNLOAD_DIR,
                "--no-start-paused",
            ])
    finally:
        subprocess.run(["docker", "exec", "transmission-vpn", "rm", "-f", TEMP_TORRENT],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    watched = record_watched_torrents(before_ids, results)
    for result in results:
        print(f"Queued through VPN-isolated Transmission: {result.get('title', 'selected result')}")
    print("Final location: /srv/data/media/library/manual")
    print("After completion, the automatic organizer will publish recognized media to Jellyfin.")
    if watched:
        print(f"Watching {watched} newly queued item(s) for paused, no-peer, failed, and completed states.")


def torrent_state(torrent):
    if torrent.get("error") or torrent.get("error_string"):
        return "failed"
    status = torrent.get("status")
    left = torrent.get("left_until_done", 1)
    if left == 0 and status in (5, 6):
        return "completed"
    if status == 0:
        if left and not torrent.get("peers_sending_to_us"):
            return "paused-no-peers"
        return "paused"
    if status == 3 or (status == 4 and not torrent.get("rate_download") and not torrent.get("peers_sending_to_us")):
        return "waiting-for-peers"
    if status == 4:
        return "downloading"
    if status in (1, 2):
        return "checking"
    return "queued"


def watch_message(name, state, torrent):
    if state == "completed":
        return f"Media download finished: {name}"
    if state == "paused":
        return f"Media download paused before completion: {name}"
    if state == "paused-no-peers":
        return f"Media download paused before completion with no connected peers: {name}"
    if state == "waiting-for-peers":
        return f"Media download is waiting for peers (no active download peers): {name}"
    if state == "failed":
        detail = torrent.get("error_string") or "Transmission reported an error"
        return f"Media download failed: {name} — {detail}"
    return f"Media download resumed: {name}"


def drain_organizer_events():
    messages = []
    try:
        events = sorted(ORGANIZER_EVENT_DIR.glob("*.json"))
    except OSError:
        return messages
    for event in events:
        try:
            data = json.loads(event.read_text(encoding="utf-8"))
            message = data.get("message")
            if isinstance(message, str) and message:
                messages.append(message)
        except (OSError, json.JSONDecodeError):
            pass
        try:
            event.unlink()
        except OSError:
            pass
    return messages


def watch_downloads():
    state = load_watch_state()
    messages = drain_organizer_events()
    try:
        network_guardrails()
    except QueueError as error:
        if state.get("pipeline") != "failed":
            messages.append(f"Media download watcher stopped by a VPN guardrail: {error}")
        state["pipeline"] = "failed"
        save_watch_state(state)
        print("\n".join(messages))
        return
    recovered = state.get("pipeline") == "failed"
    state["pipeline"] = "healthy"
    torrents = {str(torrent.get("id")): torrent for torrent in transmission_torrents()}
    if recovered:
        messages.append("Media download watcher restored its protected connection.")
    now = time.time()
    for torrent_id, torrent in torrents.items():
        if torrent_id in state["watched"]:
            continue
        current = torrent_state(torrent)
        recently_added = now - float(torrent.get("added_date", 0)) < 30 * 60
        state["watched"][torrent_id] = {
            "name": torrent.get("name", "queued media"),
            # Notify when a manually added torrent completes between polls, but
            # silently baseline older torrents that predate this watcher.
            "state": None if recently_added else current,
        }
    alert_states = {"completed", "paused", "paused-no-peers", "waiting-for-peers", "failed"}
    for torrent_id, watched in list(state["watched"].items()):
        torrent = torrents.get(torrent_id)
        previous = watched.get("state")
        name = watched.get("name", "queued media")
        if torrent is None:
            if previous not in ("completed", "removed"):
                messages.append(f"Media download was removed before completion: {name}")
            watched["state"] = "removed"
            continue
        current = torrent_state(torrent)
        if previous is None:
            if current in alert_states:
                messages.append(watch_message(name, current, torrent))
        elif current != previous:
            if current in alert_states:
                messages.append(watch_message(name, current, torrent))
            elif previous in {"paused", "paused-no-peers", "waiting-for-peers", "failed"} and current in {"downloading", "checking", "queued"}:
                messages.append(watch_message(name, "resumed", torrent))
        watched["state"] = current
    save_watch_state(state)
    print("\n".join(messages))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="verify VPN and Transmission isolation")
    subparsers.add_parser("status", help="show the Transmission queue")
    search = subparsers.add_parser("search", help="search configured Prowlarr indexers")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    queue = subparsers.add_parser("queue", help="queue selected current search results")
    queue.add_argument("--query", required=True)
    queue.add_argument("--index", type=int, nargs="+", required=True)
    queue.add_argument("--confirm", action="store_true", help="required after the user selects displayed result numbers")
    queue.add_argument("--dry-run", action="store_true", help="validate without queueing")
    subparsers.add_parser(
        "watch",
        help="emit changed states for all Transmission items and organizer events",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    require_root()
    if args.command == "health":
        network_guardrails()
        print("VPN guardrails passed: Gluetun healthy, kill switch active, Transmission isolated.")
    elif args.command == "status":
        network_guardrails()
        print(command(TRANSMISSION + ["--list"]), end="")
    elif args.command == "search":
        results = search_results(args.query, args.limit)
        cache_search_results(args.query, results)
        print_results(results)
    elif args.command == "queue":
        if not args.confirm:
            raise QueueError("Queueing requires --confirm after the user selects displayed result numbers.")
        queue_results(selected_results(args.query, args.index), args.dry_run)
    elif args.command == "watch":
        watch_downloads()


if __name__ == "__main__":
    try:
        main()
    except QueueError as error:
        print(f"media-queue: {error}", file=sys.stderr)
        raise SystemExit(2)
