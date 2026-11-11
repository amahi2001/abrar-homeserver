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


STATE = Path("/var/lib/shopping-research-mcp")
STAMP = re.compile(r"\d{8}T\d{6}Z")


def prune(directory, keep):
    snapshots = sorted(
        path for path in directory.iterdir()
        if path.is_dir() and not path.is_symlink() and STAMP.fullmatch(path.name)
        and (path / "manifest.json").is_file()
    )
    for snapshot in snapshots[:-keep]:
        shutil.rmtree(snapshot)


@contextmanager
def locked(path):
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def backup(kind, *, state=STATE, now=None, run=subprocess.run):
    now = now or datetime.now(timezone.utc)
    name = now.strftime("%Y%m%dT%H%M%SZ")
    root = state / "backups"
    with locked(state / "backup.lock"):
        destination = root / kind / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        owner = (state / "data").stat()
        os.chown(destination.parent, owner.st_uid, owner.st_gid)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Snapshot destination already exists")
        relative = destination.relative_to(root)
        image = run([
            "docker", "compose", "--project-directory", str(state / "current"),
            "-f", str(state / "current/compose.yaml"), "images", "--quiet", "app",
        ], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{12,64}", image):
            raise RuntimeError("Could not identify exactly one local application image")
        run([
            "docker", "run", "--rm", "--pull=never", "--network=none",
            "--memory=2g", "--pids-limit=64", "--user", f"{owner.st_uid}:{owner.st_gid}",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--mount", f"type=bind,src={state / 'data'},dst=/data,readonly",
            "--mount", f"type=bind,src={root},dst=/backups",
            "--entrypoint=timeout", image,
            "--signal=TERM", "--kill-after=30s", "900s",
            "bun", "dist/cli.js", "backup", "/backups/" + str(relative),
        ], check=True, timeout=960)
        if not (destination / "manifest.json").is_file():
            raise RuntimeError("Backup CLI did not finish a manifest")
        prune(destination.parent, 7 if kind == "daily" else 3)
        if kind == "daily":
            weekly = root / "weekly"
            weekly.mkdir(exist_ok=True, mode=0o750)
            week = now.strftime("%G-W%V")
            if not any((entry / "week").is_file() and (entry / "week").read_text() == week
                       for entry in weekly.iterdir() if entry.is_dir() and not entry.is_symlink()):
                target = weekly / name
                shutil.copytree(destination, target)
                (target / "week").write_text(week)
                prune(weekly, 4)
        print(f"Consistent {kind} snapshot retained: {destination}")
        return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("daily", "pre-upgrade"))
    args = parser.parse_args()
    backup(args.kind)
