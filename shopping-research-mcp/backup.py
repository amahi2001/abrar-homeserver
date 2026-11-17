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


def private_parent(directory):
    # Migrate earlier app-owned backup parents in place. The parent of this
    # directory is already controlled by the backup administrator.
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(fd, os.geteuid(), -1)
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def retire_orphan(run):
    # The daemon can retain a worker after the invoking client has exited.
    row = run(["docker", "ps", "-a", "--no-trunc", "--filter", f"name=^/{WORKER}$", "--format",
               '{{.ID}} {{.State}} {{.Label "' + OWNER_LABEL + '"}}'],
              check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if not row:
        return
    match = re.fullmatch(r"([a-f0-9]{64}) (created|exited|dead) 1", row)
    if not match:
        raise RuntimeError("An active or unowned backup worker still exists; retry after it exits")
    # Address the observed ID; a simultaneous start makes non-forced removal fail.
    run(["docker", "rm", match[1]], check=True, timeout=30)


def clean_abandoned(root, requested):
    legacy_count = 0
    for kind in ("daily", "pre-upgrade", "weekly"):
        directory = root / kind
        if directory.is_symlink():
            if kind == requested or (requested == "daily" and kind == "weekly"):
                raise RuntimeError(f"Refusing linked {kind} backup directory")
            continue
        private_parent(directory)
        for entry in directory.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if STAGING.fullmatch(entry.name):
                shutil.rmtree(entry)
            elif STAMP.fullmatch(entry.name) and not (entry / "manifest.json").is_file():
                legacy_count += 1
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
        private_parent(root)
        clean_abandoned(root, kind)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Snapshot destination already exists")
        staging = destination.parent / f".{name}.incomplete"
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
        snapshot.rename(destination)
        staging.rmdir()
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
              f"{time.monotonic() - started:.1f}s, {shutil.disk_usage(root).free} bytes free)")
        return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("daily", "pre-upgrade"))
    args = parser.parse_args()
    backup(args.kind)
