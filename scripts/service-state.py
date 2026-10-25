#!/usr/bin/env python3
"""Quiesce and preserve application state around a guarded NixOS upgrade."""

import argparse
import json
import os
from pathlib import Path
import subprocess


TIMERS = ("nextcloud-cron.timer", "media-organizer.timer", "youtube-queue-worker.timer")
SERVICES = (
    "youtube-queue-worker.service", "media-organizer.service", "nextcloud-cron.service",
    "hermes-dashboard.service", "hermes-agent.service", "phpfpm-nextcloud.service",
    "vaultwarden.service", "jellyfin.service", "prowlarr.service", "sonarr.service", "radarr.service",
)
UNITS = TIMERS + SERVICES
PATHS = {
    "nextcloud-config": Path("/srv/data/nextcloud/config"),
    "vaultwarden": Path("/var/lib/vaultwarden"),
    "jellyfin": Path("/var/lib/jellyfin"),
    "prowlarr": Path("/var/lib/prowlarr"),
    "sonarr": Path("/var/lib/sonarr"),
    "radarr": Path("/var/lib/radarr"),
    "hermes": Path("/var/lib/hermes/.hermes"),
}
HERMES_EXCLUDES = (
    "**/.venv/", "**/node_modules/", "**/.git/", "/google-env/", "/cache/",
    "/.cache/", "/runtime/", "/bin/", "/lsp/", "/logs/", "**/ms-playwright/",
    "**/browsers/", "**/.next/", "*.sock", "*.pid", "*.lock",
)
PG_DUMP = "/run/current-system/sw/bin/pg_dump"
PG_RESTORE = "/run/current-system/sw/bin/pg_restore"
OCC = "/run/current-system/sw/bin/nextcloud-occ"


def command(args, *, output=None, input_file=None):
    # Dump/restore streams are opened by root; postgres cannot traverse the backup directory.
    with open(output, "wb") if output else open(os.devnull, "wb") as destination:
        with open(input_file, "rb") if input_file else open(os.devnull, "rb") as source:
            result = subprocess.run(args, stdin=source, stdout=destination if output else subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=1800)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): "
                           + result.stderr.decode(errors="replace")[-4000:])
    return result.stdout.decode(errors="replace") if result.stdout else ""


def active(unit, run):
    return run(["systemctl", "show", "--property=ActiveState", "--value", unit]).strip() in (
        "active", "activating", "reloading",
    )


def save(directory, manifest):
    temporary = directory / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(directory / "manifest.json")


def target(label, paths):
    path = paths[label]
    resolved = path.resolve()
    allowed = {path.absolute()}
    if label == "prowlarr":
        allowed.add(path.parent / "private" / path.name)
    if resolved not in allowed:
        raise RuntimeError(f"Unexpected state directory symlink: {path}")
    return resolved


def load(directory, paths=PATHS, *, complete=False):
    if directory.is_symlink():
        raise RuntimeError("Refusing a symlinked backup directory")
    manifest = json.loads((directory / "manifest.json").read_text())
    if (manifest.get("version") != 1 or not isinstance(manifest.get("active_units"), list)
            or not set(manifest["active_units"]).issubset(UNITS)
            or not isinstance(manifest.get("maintenance_before"), bool)
            or not isinstance(manifest.get("paths"), dict)):
        raise RuntimeError("Invalid application-state backup manifest")
    for label, recorded in manifest["paths"].items():
        if label not in paths or str(target(label, paths)) != recorded:
            raise RuntimeError(f"Refusing an unrecognized restore destination: {label}")
        snapshot = directory / label
        if complete and (snapshot.is_symlink() or not snapshot.is_dir()):
            raise RuntimeError(f"Missing state snapshot: {label}")
    dump = directory / "nextcloud.pgdump"
    if complete and (manifest.get("complete") is not True or dump.is_symlink()
                     or not dump.is_file() or dump.stat().st_size == 0):
        raise RuntimeError("Application-state backup is incomplete")
    return manifest


def require_hermes_idle(run):
    if not active("hermes-agent.service", run):
        return
    status = json.loads(run(["curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                             "http://127.0.0.1:9119/api/status"]))
    # Hermes' NAS lifecycle contract counts in-flight work; active_sessions is five-minute history.
    if not (status.get("gateway_running") is True and status.get("gateway_busy") is False
            and status.get("gateway_drainable") is True and status.get("active_agents") == 0):
        raise RuntimeError("Hermes is busy; preserving its running conversations")


def pause(run, *, require_idle=True):
    if require_idle:
        require_hermes_idle(run)
    # Stop triggers first so none can restart a writer while its state is copied.
    for unit in UNITS:
        if active(unit, run):
            run(["systemctl", "stop", unit])


def sync(source, destination, label, run, *, restore=False):
    args = ["rsync", "-aHAX", "--numeric-ids"]
    if restore:
        args.append("--delete")
    if label == "hermes":
        for pattern in HERMES_EXCLUDES:
            args.extend(["--exclude", pattern])
    run([*args, "--", str(source) + "/", str(destination) + "/"])


def resume(directory, run=command, *, paths=PATHS):
    if not (directory / "manifest.json").exists():
        return
    manifest = load(directory, paths)
    if manifest.get("resumed") is True:
        return
    errors = []

    def attempt(args):
        try:
            run(args)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            errors.append(str(error))

    # Keep services that were intentionally offline (notably Sonarr/Radarr) offline.
    for unit in UNITS:
        if unit not in manifest["active_units"] and active(unit, run):
            attempt(["systemctl", "stop", unit])
    attempt([OCC, "maintenance:mode", "--on" if manifest["maintenance_before"] else "--off"])
    for unit in SERVICES + TIMERS:
        if unit in manifest["active_units"]:
            attempt(["systemctl", "start", unit])
    if errors:
        raise RuntimeError("Resuming application services needs attention: " + "; ".join(errors))
    manifest["resumed"] = True
    save(directory, manifest)


def backup(directory, run=command, *, paths=PATHS):
    if directory.exists():
        raise RuntimeError("Refusing to overwrite an existing application-state backup")
    directory.mkdir(parents=True, mode=0o700)
    status = json.loads(run([OCC, "status", "--output=json"]))
    manifest = {
        "version": 1, "complete": False, "resumed": False,
        "active_units": [unit for unit in UNITS if active(unit, run)],
        "maintenance_before": status.get("maintenance", False),
        "paths": {},
    }
    if not isinstance(manifest["maintenance_before"], bool):
        raise RuntimeError("Cannot determine Nextcloud maintenance state")
    if manifest["maintenance_before"]:
        raise RuntimeError("Nextcloud is already in maintenance mode; defer updates until maintenance finishes")
    save(directory, manifest)
    try:
        require_hermes_idle(run)
        if not manifest["maintenance_before"]:
            run([OCC, "maintenance:mode", "--on"])
        pause(run)
        for label, path in paths.items():
            if path.exists():
                resolved = target(label, paths)
                destination = directory / label
                destination.mkdir(mode=0o700)
                sync(resolved, destination, label, run)
                manifest["paths"][label] = str(resolved)
                save(directory, manifest)
        run(["runuser", "--user", "postgres", "--", PG_DUMP,
             "--format=custom", "--dbname=nextcloud"], output=directory / "nextcloud.pgdump")
        manifest["complete"] = True
        save(directory, manifest)
        print(f"Application state is quiesced and backed up in {directory}", flush=True)
    except BaseException as error:
        try:
            resume(directory, run, paths=paths)
        except BaseException as resume_error:
            raise RuntimeError(f"Backup failed: {error}; services could not fully resume: {resume_error}") from error
        raise


def restore(directory, run=command, *, paths=PATHS):
    manifest = load(directory, paths, complete=True)
    # Recheck all destinations before stopping anything or deleting new application files.
    # A resumed gateway may already have accepted new work. Failed/inactive
    # gateways need no status endpoint, but an active gateway must prove idle.
    require_hermes_idle(run)
    manifest["resumed"] = False
    save(directory, manifest)
    pause(run)
    for label in manifest["paths"]:
        destination = target(label, paths)
        destination.mkdir(parents=True, exist_ok=True)
        sync(directory / label, destination, label, run, restore=True)
    run(["runuser", "--user", "postgres", "--", PG_RESTORE, "--clean", "--if-exists",
         "--exit-on-error", "--single-transaction", "--dbname=nextcloud"],
        input_file=directory / "nextcloud.pgdump")
    # The snapshot includes our temporary flag; restore it before old-generation setup runs.
    run([OCC, "maintenance:mode", "--on" if manifest["maintenance_before"] else "--off"])
    print(f"Restored application state from {directory}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("backup", "resume", "restore"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        {"backup": backup, "resume": resume, "restore": restore}[args.phase](args.directory)
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Application-state {args.phase} failed: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
