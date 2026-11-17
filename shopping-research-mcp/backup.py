#!/usr/bin/env python3
"""Keep consistent online database snapshots, never copies of live WAL files."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


STATE = Path("/var/lib/shopping-research-mcp")
WORKER = "shopping-research-mcp-backup-worker"
STAGING = re.compile(r"\.\d{8}T\d{6}Z\.incomplete")
STAMP = re.compile(r"\d{8}T\d{6}Z")


def prune(directory, keep):
    snapshots = sorted(
        path for path in directory.iterdir()
        if path.is_dir() and not path.is_symlink() and STAMP.fullmatch(path.name)
        and (path / "manifest.json").is_file()
        and (directory.name != "weekly" or (path / "week").is_file())
    )
    for snapshot in snapshots[:-keep]:
        shutil.rmtree(snapshot)


@contextmanager
def locked(path):
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def clean_abandoned(root, run):
    abandoned = []
    legacy_count = 0
    for kind in ("daily", "pre-upgrade", "weekly"):
        directory = root / kind
        if directory.is_symlink():
            raise RuntimeError(f"Refusing linked {kind} backup directory")
        if not directory.exists():
            continue
        for entry in directory.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if STAGING.fullmatch(entry.name):
                abandoned.append(entry)
            elif STAMP.fullmatch(entry.name) and not (entry / "manifest.json").is_file():
                legacy_count += 1
    if legacy_count:
        print(f"Retained {legacy_count} unmarked backup directories for inspection.", file=sys.stderr)
    if abandoned:
        # Docker can outlive an interrupted client that released the host lock.
        worker = run(["docker", "ps", "-aq", "--filter", f"name=^/{WORKER}$"],
                     check=True, capture_output=True, text=True, timeout=30)
        if worker.stdout.strip():
            raise RuntimeError("Backup worker still exists; retry after it exits")
        for entry in abandoned:
            shutil.rmtree(entry)


def backup(kind, *, state=STATE, now=None, run=subprocess.run):
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    name = now.strftime("%Y%m%dT%H%M%SZ")
    root = state / "backups"
    with locked(state / "backup.lock"):
        clean_abandoned(root, run)
        destination = root / kind / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        owner = (state / "data").stat()
        os.chown(destination.parent, owner.st_uid, owner.st_gid)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Snapshot destination already exists")
        image = run([
            "docker", "compose", "--project-directory", str(state / "current"),
            "-f", str(state / "current/compose.yaml"), "images", "--quiet", "app",
        ], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{12,64}", image):
            raise RuntimeError("Could not identify exactly one local application image")
        staging = destination.parent / f".{name}.incomplete"
        staging.mkdir(mode=0o700)
        os.chown(staging, owner.st_uid, owner.st_gid)
        run([
            "docker", "run", "--name", WORKER, "--rm", "--pull=never", "--network=none",
            "--memory=2g", "--pids-limit=64", "--user", f"{owner.st_uid}:{owner.st_gid}",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--mount", f"type=bind,src={state / 'data'},dst=/data,readonly",
            "--mount", f"type=bind,src={staging},dst=/backups",
            "--entrypoint=timeout", image,
            "--signal=TERM", "--kill-after=30s", "900s",
            "bun", "dist/cli.js", "backup", "/backups/snapshot",
        ], check=True, timeout=960)
        snapshot = staging / "snapshot"
        files = [snapshot / name for name in ("manifest.json", "shopping.sqlite", "auth.sqlite")]
        if snapshot.is_symlink() or any(path.is_symlink() or not path.is_file() for path in files):
            raise RuntimeError("Backup CLI did not finish both databases and manifest")
        size = sum(path.stat().st_size for path in files)
        snapshot.rename(destination)
        staging.rmdir()
        prune(destination.parent, 7 if kind == "daily" else 3)
        if kind == "daily":
            weekly = root / "weekly"
            weekly.mkdir(exist_ok=True, mode=0o750)
            week = now.strftime("%G-W%V")
            if not any((entry / "week").is_file() and (entry / "week").read_text() == week
                       for entry in weekly.iterdir() if entry.is_dir() and not entry.is_symlink()
                       and STAMP.fullmatch(entry.name) and (entry / "manifest.json").is_file()):
                target = weekly / name
                # Publish the whole weekly snapshot atomically, including its markers.
                staging = weekly / f".{name}.incomplete"
                shutil.copytree(destination, staging)
                (staging / "week").write_text(week)
                staging.rename(target)
                prune(weekly, 4)
        print(f"Consistent {kind} snapshot retained: {destination} ({size} bytes, "
              f"{time.monotonic() - started:.1f}s, {shutil.disk_usage(root).free} bytes free)")
        return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("daily", "pre-upgrade"))
    args = parser.parse_args()
    backup(args.kind)
