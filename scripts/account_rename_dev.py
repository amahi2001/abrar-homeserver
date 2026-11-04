"""Preserve this host's development fleet during the account/home rename.

The privileged controller owns quiescing, moving the home, and rollback order.
This module never starts an agent, switches a Git branch, or rebuilds an image.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time


PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")


def run_command(args, *, timeout=120):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # Resolved Compose files and tool output can contain account secrets.
        raise RuntimeError(f"Development migration command failed: {Path(args[0]).name}")
    return result.stdout.strip()


def compose_identity(project, source):
    return f"dev-{project}-{hashlib.sha256(str(source).encode()).hexdigest()[:8]}"


def rebase(value, old_home, new_home, old_project=None, new_project=None):
    if isinstance(value, dict):
        return {key: rebase(item, old_home, new_home, old_project, new_project)
                for key, item in value.items()}
    if isinstance(value, list):
        return [rebase(item, old_home, new_home, old_project, new_project) for item in value]
    if isinstance(value, str):
        old, new = str(old_home), str(new_home)
        value = new if value == old else value.replace(old + "/", new + "/")
        if old_project:
            value = value.replace(old_project, new_project)
    return value


def port_mappings(container):
    result = []
    for target, bindings in container.get("NetworkSettings", {}).get("Ports", {}).items():
        number, protocol = target.split("/")
        for binding in bindings or []:
            result.append([binding["HostIp"], int(binding["HostPort"]), int(number), protocol])
    return sorted(result)


def mount_mappings(container):
    if any(mount["Type"] != "bind" for mount in container.get("Mounts", [])):
        raise RuntimeError("Development migration requires exclusively bind-mounted data")
    return sorted([[mount["Source"], mount["Destination"], mount["RW"]]
                   for mount in container.get("Mounts", [])])


def _git(run, source, *args):
    return run(["git", "-c", f"safe.directory={source}", "-C", str(source), *args])


def _snapshot(home, path, backup, snapshots):
    relative = str(path.relative_to(home))
    if relative in snapshots:
        return
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlinked migration file: {relative}")
    if not path.exists():
        snapshots[relative] = {"existed": False}
        return
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"Not a regular migration file: {relative}")
    destination = backup / "files" / str(len(snapshots))
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(path, destination)
    destination.chmod(0o600)
    snapshots[relative] = {"existed": True, "backup": str(destination),
                           "mode": stat.S_IMODE(info.st_mode),
                           "uid": info.st_uid, "gid": info.st_gid}


def _write(path, content, *, uid, gid, mode=0o600):
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlinked migration destination: {path.name}")
    created = []
    parent = path.parent
    while not parent.exists():
        created.append(parent)
        parent = parent.parent
    if parent.resolve() != parent:
        raise RuntimeError("Refusing a symlinked migration parent directory")
    for directory in reversed(created):
        directory.mkdir(mode=0o700)
        os.chown(directory, uid, gid)
    descriptor, temporary = tempfile.mkstemp(prefix=".account-rename-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content if isinstance(content, bytes) else content.encode())
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            shutil.copystat(path, temporary, follow_symlinks=False)
        os.chown(temporary, uid, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_write(path, content, uid, gid):
    _write(path, json.dumps(content, indent=2) + "\n", uid=uid, gid=gid)


def capture(old_home, backup_directory, *, run=run_command):
    """Capture minimal container state and private rollback copies, not environments."""
    home, backup = Path(old_home), Path(backup_directory)
    if home.resolve() != home or home.is_symlink():
        raise RuntimeError("The original home must be its canonical directory")
    if backup.exists() and any(backup.iterdir()):
        raise RuntimeError("Development backup directory must be empty")
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup.chmod(0o700)
    owner = home.stat()
    manifest = {"old_home": str(home), "backup_directory": str(backup),
                "uid": owner.st_uid, "gid": owner.st_gid,
                "projects": [], "files": {}, "credentials": {}}
    root = home / ".local/state/dev/projects"
    for metadata_path in sorted(root.glob("*/project.json")):
        project = metadata_path.parent.name
        if not PROJECT.fullmatch(project):
            raise RuntimeError("Invalid development project name")
        metadata = json.loads(metadata_path.read_text())
        source = home / "projects" / project
        if (source.is_symlink() or metadata_path.parent.is_symlink()
                or metadata["source"] != str(source)
                or metadata["composeProject"] != compose_identity(project, source)):
            raise RuntimeError(f"Unexpected project identity: {project}")
        compose = json.loads((metadata_path.parent / "compose.json").read_text())
        if set(compose["services"]) != {"dev"}:
            raise RuntimeError(f"Unplanned development sidecars: {project}")
        ids = run(["docker", "ps", "-aq", "--filter",
                   "label=com.docker.compose.project=" + metadata["composeProject"],
                   "--filter", "label=com.docker.compose.service=dev"]).split()
        if len(ids) != 1:
            raise RuntimeError(f"Expected one original development container: {project}")
        container = json.loads(run(["docker", "inspect", ids[0]]))[0]
        git_directory = Path(_git(run, source, "rev-parse", "--absolute-git-dir"))
        git_directory.relative_to(home)
        status = _git(run, source, "status", "--porcelain")
        if status:
            raise RuntimeError(f"Development source has pending changes: {project}")
        item = {"name": project, "metadata": metadata, "container": ids[0],
                "image_id": container["Image"], "running": container["State"]["Running"],
                "ports": port_mappings(container), "mounts": mount_mappings(container),
                "head": _git(run, source, "rev-parse", "HEAD"),
                "branch": _git(run, source, "symbolic-ref", "--short", "HEAD"),
                "git_directory": str(git_directory.relative_to(home)),
                "tracked_migrations": [],
                "native_archive": str((metadata_path.parent / "devcontainers.account-rename-backup").relative_to(home))}
        if (home / item["native_archive"]).exists():
            raise RuntimeError(f"Existing native-state migration archive: {project}")
        for path in [metadata_path, metadata_path.parent / "compose.json",
                     metadata_path.parent / "devcontainer.json",
                     source / ".dev/compose.yaml", git_directory / "info/exclude"]:
            _snapshot(home, path, backup, manifest["files"])
        if project == "token-eye":
            # The host recovery wrapper selects its existing box by Compose ID.
            for relative in ["scripts/token-eye-dogfood-server", "scripts/token-eye-dogfood-server.test.py"]:
                path = source / relative
                if not path.is_file():
                    raise RuntimeError(f"Required TokenEye host recovery file missing: {relative}")
                text = path.read_text()
                if metadata["composeProject"] in text or str(home) + "/" in text:
                    _snapshot(home, path, backup, manifest["files"])
                    item["tracked_migrations"].append(relative)
        manifest["projects"].append(item)
    if not manifest["projects"]:
        raise RuntimeError("No development projects found")
    for relative in [".local/state/dev/accounts/codex/auth.json",
                     ".local/state/dev/accounts/claude/.credentials.json",
                     ".local/state/dev/credentials/github-token"]:
        path = home / relative
        if path.is_file():
            manifest["credentials"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _ports_override(text, ports):
    """Replace only dev.ports, retaining the existing build/environment blocks."""
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if re.fullmatch(r"  dev:\s*\n?", line)]
    if len(starts) != 1:
        raise RuntimeError("Expected exactly one dev service in the local Compose override")
    first = starts[0] + 1
    end = next((i for i in range(first, len(lines))
                if lines[i].strip() and not lines[i].lstrip().startswith("#")
                and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    existing = [i for i in range(first, end) if re.match(r"    ports:\s", lines[i])]
    if len(existing) > 1:
        raise RuntimeError("Duplicate dev port blocks")
    start = existing[0] if existing else end
    finish = start
    if existing:
        finish = next((i for i in range(start + 1, end)
                       if lines[i].strip() and not lines[i].lstrip().startswith("#")
                       and len(lines[i]) - len(lines[i].lstrip()) <= 4), end)
    comments = [line for line in lines[start:finish] if line.lstrip().startswith("#")]
    block = ["    ports: !override\n", *comments]
    for ip, published, target, protocol in ports:
        if ":" in ip:
            raise RuntimeError("An IPv6 published port needs explicit migration review")
        block.append(f'      - "{ip}:{published}:{target}/{protocol}"\n')
    return "".join(lines[:start] + block + lines[finish:])


def migrate(manifest, new_home, *, run=run_command):
    """Run after quiescing/moving home; retries preserve the original rollback copies."""
    old_home, home = manifest["old_home"], Path(new_home)
    if home.resolve() != home or home.is_symlink():
        raise RuntimeError("The new home must be canonical, not a compatibility symlink")
    uid, gid = manifest["uid"], manifest["gid"]
    for item in manifest["projects"]:
        project = item["name"]
        source = home / "projects" / project
        state = home / ".local/state/dev/projects" / project
        original = item["metadata"]
        identity = compose_identity(project, source)
        image = original["baseImage"] + "-" + identity
        run(["docker", "image", "tag", item["image_id"], image])
        for filename in ["compose.json", "devcontainer.json"]:
            relative = str((state / filename).relative_to(home))
            snapshot = manifest["files"][relative]
            content = json.loads(Path(snapshot["backup"]).read_text())
            content = rebase(content, old_home, home, original["composeProject"], identity)
            if filename == "compose.json":
                content["name"] = identity
                dev = content["services"]["dev"]
                dev["image"] = image
                dev["pull_policy"] = "never"
                dev.pop("build", None)
                if item["running"]:
                    dev["ports"] = [{"host_ip": ip, "published": str(published),
                                     "target": target, "protocol": protocol, "mode": "ingress"}
                                    for ip, published, target, protocol in item["ports"]]
            _json_write(state / filename, content, uid, gid)
        metadata = dict(original, source=str(source), composeProject=identity, image=image)
        metadata.pop("configHash", None)
        _json_write(state / "project.json", metadata, uid, gid)
        native, archive = state / "devcontainers", home / item["native_archive"]
        if native.exists() and not archive.exists():
            native.rename(archive)
        native.mkdir(mode=0o700, exist_ok=True)
        os.chown(native, uid, gid)
        for relative in item["tracked_migrations"]:
            path = source / relative
            snapshot = manifest["files"][str(path.relative_to(home))]
            text = Path(snapshot["backup"]).read_text()
            text = rebase(text, old_home, home, original["composeProject"], identity)
            _write(path, text, uid=uid, gid=gid, mode=snapshot["mode"])
        if item["running"]:
            override = source / ".dev/compose.yaml"
            snapshot = manifest["files"][str(override.relative_to(home))]
            text = Path(snapshot["backup"]).read_text() if snapshot["existed"] else "services:\n  dev:\n"
            text = rebase(text, old_home, home)
            _write(override, _ports_override(text, item["ports"]), uid=uid, gid=gid,
                   mode=snapshot.get("mode", 0o600))
            if not snapshot["existed"]:
                exclude = home / item["git_directory"] / "info/exclude"
                saved = manifest["files"][str(exclude.relative_to(home))]
                content = Path(saved["backup"]).read_text() if saved["existed"] else ""
                if "/.dev/compose.yaml" not in content.splitlines():
                    content = content.rstrip("\n") + "\n/.dev/compose.yaml\n"
                _write(exclude, content, uid=uid, gid=gid, mode=saved.get("mode", 0o600))


def start(manifest, new_home, *, user="amahi", run=run_command, health_timeout=180):
    """Use the native CLI, never dev start (which may change a main branch)."""
    home = Path(new_home)
    for item in sorted(manifest["projects"], key=lambda value: value["name"] != "token-eye"):
        if not item["running"]:
            continue
        project = item["name"]
        state, source = home / ".local/state/dev/projects" / project, home / "projects" / project
        identity = compose_identity(project, source)
        args = [str(home / ".local/share/devcontainers/bin/devcontainer"), "up",
                "--workspace-folder", str(source), "--config", str(state / "devcontainer.json"),
                "--user-data-folder", str(state / "devcontainers"),
                "--id-label", f"com.docker.compose.project={identity}",
                "--id-label", "com.docker.compose.service=dev"]
        run(["runuser", "-u", user, "--", "env", f"HOME={home}", f"USER={user}",
             f"LOGNAME={user}", *args], timeout=180)
        deadline = time.monotonic() + health_timeout
        while True:
            ids = _new_containers(item, home, run)
            if len(ids) == 1:
                container = json.loads(run(["docker", "inspect", ids[0]]))[0]
                if (container["State"]["Running"]
                        and container["State"].get("Health", {}).get("Status") == "healthy"):
                    break
                if not container["State"]["Running"] or container["State"].get("Health", {}).get("Status") == "unhealthy":
                    raise RuntimeError(f"Migrated development container failed: {project}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Migrated development container did not become healthy: {project}")
            time.sleep(0.5)


def _new_containers(item, home, run):
    identity = compose_identity(item["name"], home / "projects" / item["name"])
    return run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={identity}",
                "--filter", "label=com.docker.compose.service=dev"]).split()


def verify(manifest, new_home, *, run=run_command):
    home = Path(new_home)
    ports, mounts, configured_mounts, running = 0, 0, 0, 0
    for item in manifest["projects"]:
        project, original = item["name"], item["metadata"]
        source = home / "projects" / project
        metadata = json.loads((home / ".local/state/dev/projects" / project / "project.json").read_text())
        if (metadata["source"] != str(source) or metadata["composeProject"] != compose_identity(project, source)
                or metadata["repo"] != original["repo"]
                or metadata["privateDefault"] != original["privateDefault"]
                or metadata["baseImage"] != original["baseImage"]
                or metadata["image"] != original["baseImage"] + "-" + compose_identity(project, source)):
            raise RuntimeError(f"Project identity changed unexpectedly: {project}")
        compose = json.loads((home / ".local/state/dev/projects" / project / "compose.json").read_text())
        service = compose["services"]["dev"]
        planned_mounts = sorted([[mount["source"], mount["target"], not mount.get("read_only", False)]
                                 for mount in service["volumes"]])
        expected_mounts = sorted(rebase(item["mounts"], manifest["old_home"], home))
        if (compose["name"] != metadata["composeProject"] or service["image"] != metadata["image"]
                or service.get("pull_policy") != "never" or "build" in service
                or planned_mounts != expected_mounts):
            raise RuntimeError(f"Migrated Compose configuration differs: {project}")
        configured_mounts += len(planned_mounts)
        if (_git(run, source, "rev-parse", "HEAD") != item["head"]
                or _git(run, source, "symbolic-ref", "--short", "HEAD") != item["branch"]):
            raise RuntimeError(f"Git head or branch changed: {project}")
        expected_changes = set(item["tracked_migrations"])
        if project == "token-eye":
            expected_changes.add(".dev/compose.yaml")
        if item.get("source_committed"):
            expected_changes.clear()
        actual_changes = set()
        for line in _git(run, source, "status", "--porcelain").splitlines():
            status, _, relative = line.lstrip().partition(" ")
            if status != "M":
                raise RuntimeError(f"Unexpected source changes after migration: {project}")
            actual_changes.add(relative.lstrip())
        if actual_changes != expected_changes:
            raise RuntimeError(f"Unexpected source changes after migration: {project}")
        for relative in item["tracked_migrations"]:
            path = source / relative
            snapshot = manifest["files"][str(path.relative_to(home))]
            expected = rebase(Path(snapshot["backup"]).read_text(), manifest["old_home"], home,
                              original["composeProject"], metadata["composeProject"])
            if path.read_text() != expected:
                raise RuntimeError(f"Unexpected edits to migrated host recovery file: {project}")
        old = json.loads(run(["docker", "inspect", item["container"]]))[0]
        if old["State"]["Running"]:
            raise RuntimeError(f"Original development container is still running: {project}")
        ids = _new_containers(item, home, run)
        if not item["running"]:
            if ids:
                raise RuntimeError(f"Stopped project unexpectedly recreated: {project}")
            continue
        if len(ids) != 1:
            raise RuntimeError(f"Expected one migrated container: {project}")
        current = json.loads(run(["docker", "inspect", ids[0]]))[0]
        if (current["Image"] != item["image_id"] or mount_mappings(current) != sorted(expected_mounts)
                or port_mappings(current) != item["ports"]
                or not current["State"]["Running"]
                or current["State"].get("Health", {}).get("Status") != "healthy"):
            raise RuntimeError(f"Image, mount, port, or health invariant failed: {project}")
        running += 1
        ports += len(item["ports"])
        mounts += len(item["mounts"])
    for relative, digest in manifest["credentials"].items():
        if hashlib.sha256((home / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Development credentials changed during the migration")
    return {"projects": len(manifest["projects"]), "running": running,
            "ports": ports, "running_bind_mounts": mounts, "configured_bind_mounts": configured_mounts,
            "credentials_unchanged": True}


def rollback_files(manifest, home):
    """Restore after stopping new containers; controller later resumes old IDs."""
    home = Path(home)
    for relative, snapshot in manifest["files"].items():
        path = home / relative
        if snapshot["existed"]:
            _write(path, Path(snapshot["backup"]).read_bytes(), uid=snapshot["uid"],
                   gid=snapshot["gid"], mode=snapshot["mode"])
        elif path.exists():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Refusing to remove unexpected rollback destination: {relative}")
            path.unlink()
    for item in manifest["projects"]:
        archive = home / item["native_archive"]
        native = home / ".local/state/dev/projects" / item["name"] / "devcontainers"
        if archive.exists():
            if archive.is_symlink() or native.is_symlink():
                raise RuntimeError("Refusing symlinked native-state rollback")
            if native.exists():
                shutil.rmtree(native)
            archive.rename(native)
