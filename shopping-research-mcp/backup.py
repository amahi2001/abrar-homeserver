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
OWNER_LABEL = "dev.amahi.shopping-backup"
STAGING = re.compile(r"\.\d{8}T\d{6}Z\.incomplete")
STAMP = re.compile(r"\d{8}T\d{6}Z")


def marked(path):
    return (path / "manifest.json").is_file() and (path.parent.name != "weekly" or (path / "week").is_file())


def prune(directory, keep):
    snapshots = sorted(
        path for path in directory.iterdir()
        if path.is_dir() and not path.is_symlink() and STAMP.fullmatch(path.name)
        and marked(path)
    )
    for snapshot in snapshots[:-keep]:
        shutil.rmtree(snapshot)


@contextmanager
def locked(path):
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def managed_directory(directory, *, gid, mode):
    # Migrate earlier app-owned backup parents in place. The parent of this
    # directory is already controlled by the backup administrator.
    directory.mkdir(mode=mode, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(fd, os.geteuid(), gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def retire_orphan(run):
    # The daemon can retain a worker after the invoking client has exited.
    query = ["docker", "ps", "-a", "--no-trunc", "--filter", f"name=^/{WORKER}$", "--format",
             '{{.ID}} {{.State}} {{.Label "' + OWNER_LABEL + '"}}']
    def observed():
        return run(query, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    row = observed()
    if not row:
        return
    match = re.fullmatch(r"([a-f0-9]{64}) (created|exited|dead) 1", row)
    if not match:
        raise RuntimeError("An active or unowned backup worker still exists; retry after it exits")
    # Address the observed ID; a simultaneous start makes non-forced removal fail.
    try:
        run(["docker", "rm", match[1]], check=True, timeout=30)
    except subprocess.CalledProcessError:
        if observed():  # --rm may have already finished.
            raise


def prepare_directories(root, requested, gid):
    legacy_count = 0
    required = {requested, "weekly"} if requested == "daily" else {requested}
    for kind in ("daily", "pre-upgrade", "weekly"):
        directory = root / kind
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            if kind in required:
                raise RuntimeError(f"Refusing invalid {kind} backup directory")
            print(f"Skipping invalid {kind} backup directory.", file=sys.stderr)
            continue
        if not directory.exists():
            continue
        managed_directory(directory, gid=gid, mode=0o750)
        work = directory / ".worker-staging"
        if kind != "weekly" and work.exists() and not work.is_symlink() and work.is_dir():
            managed_directory(work, gid=os.getegid(), mode=0o700)
            for entry in work.iterdir():
                if STAGING.fullmatch(entry.name) and not entry.is_symlink() and entry.is_dir():
                    shutil.rmtree(entry)
        for entry in directory.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if kind == "weekly" and STAGING.fullmatch(entry.name):
                shutil.rmtree(entry)
            elif STAMP.fullmatch(entry.name) and not marked(entry):
                legacy_count += 1
    # Reclaim failed writes across all existing tiers before allocating a missing one.
    for kind in required:
        if not (root / kind).exists():
            managed_directory(root / kind, gid=gid, mode=0o750)
    if legacy_count:
        print(f"Retained {legacy_count} unmarked backup directories for inspection.", file=sys.stderr)


def backup(kind, *, state=STATE, now=None, run=subprocess.run):
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    name = now.strftime("%Y%m%dT%H%M%SZ")
    root = state / "backups"
    with locked(state / "backup.lock"):
        destination = root / kind / name
        owner = (state / "data").stat()
        image = run([
            "docker", "compose", "--project-directory", str(state / "current"),
            "-f", str(state / "current/compose.yaml"), "images", "--quiet", "app",
        ], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{12,64}", image):
            raise RuntimeError("Could not identify exactly one local application image")
        retire_orphan(run)
        # Older helpers mount the whole root at /backups. Preserve group
        # traversal so the previous worker still runs during upgrades/rollback.
        managed_directory(root, gid=owner.st_gid, mode=0o750)
        prepare_directories(root, kind, owner.st_gid)
        work = destination.parent / ".worker-staging"
        managed_directory(work, gid=os.getegid(), mode=0o700)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Snapshot destination already exists")
        staging = work / f".{name}.incomplete"
        staging.mkdir(mode=0o700)
        os.chown(staging, owner.st_uid, owner.st_gid, follow_symlinks=False)
        run([
            "docker", "run", "--name", WORKER, "--label", OWNER_LABEL + "=1", "--rm", "--pull=never", "--network=none",
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
        # Only the two standalone databases and the manifest belong in a
        # published snapshot. SQLite may leave empty WAL and SHM sidecars.
        for db in files[1:]:
            wal = db.with_name(db.name + "-wal")
            if wal.is_symlink() or (wal.exists() and wal.stat().st_size != 0):
                raise RuntimeError("Backup is not a standalone SQLite database")
        finished = staging / "complete"
        finished.mkdir(mode=0o700)
        for path in files:
            path.rename(finished / path.name)
        finished.rename(destination)
        shutil.rmtree(staging)
        prune(destination.parent, 7 if kind == "daily" else 3)
        if kind == "daily":
            weekly = root / "weekly"
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
              f"{time.monotonic() - started:.1f}s, {shutil.disk_usage(destination.parent).free} bytes free)")
        return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("daily", "pre-upgrade"))
    args = parser.parse_args()
    backup(args.kind)
