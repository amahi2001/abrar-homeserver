#!/usr/bin/env python3
"""Build updates from the deployed source, then verify or restore the host."""

import argparse
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import stat
import subprocess
import tempfile
import time


SERVICES = (
    "sshd", "tailscaled", "nginx", "docker", "docker-adguardhome",
    "docker-gluetun", "docker-transmission-vpn", "vaultwarden", "jellyfin",
    "prowlarr", "flaresolverr", "postgresql", "phpfpm-nextcloud",
    "redis-nextcloud", "samba-smbd", "samba-nmbd", "samba-wsdd",
    "hermes-agent", "hermes-dashboard",
)
HOST = "cloud.amahi.dev"
CONFIG = "nixosConfigurations.buildfleet-server.config"
APPLICATION_UNITS = tuple(name + ".service" for name in (
    "hermes-agent", "hermes-dashboard", "vaultwarden", "jellyfin", "prowlarr",
    "sonarr", "radarr", "postgresql", "redis-nextcloud", "phpfpm-nextcloud",
    "nextcloud-setup", "nextcloud-update-db",
))
DOCKER_UNITS = ("docker.service", "docker.socket")


def units_fingerprint(system, units):
    root = system / "etc/systemd/system"
    if not root.is_dir():
        return None
    result = {}
    try:
        for unit in units:
            path = root / unit
            result[unit] = path.read_bytes() if path.exists() or path.is_symlink() else None
            for dropin in sorted((root / (unit + ".d")).glob("*.conf")):
                result[f"{unit}.d/{dropin.name}"] = dropin.read_bytes()
    except OSError:
        return None
    return result


def units_changed(previous, target, units):
    before, after = (units_fingerprint(system, units) for system in (previous, target))
    return before is None or after is None or before != after


def application_units_changed(previous, target):
    return units_changed(previous, target, APPLICATION_UNITS)


def docker_units_changed(previous, target):
    units = set(DOCKER_UNITS)
    for system in (previous, target):
        units.update(path.name for path in (system / "etc/systemd/system").glob("github-actions-*.service")
                     if path.name != "github-actions-maintenance.service")
    return units_changed(previous, target, sorted(units))


def fleet_module():
    path = os.environ.get("FLEET_MAINTENANCE_MODULE")
    if not path:
        raise RuntimeError("Docker changes require the configured fleet maintenance module")
    spec = importlib.util.spec_from_file_location("fleet_maintenance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recover_fleet(module, directory):
    path = directory / "fleet.json"
    if not path.exists():
        return
    manifest = json.loads(path.read_text())
    if not manifest.get("resumed"):
        module.resume(manifest, module.runners())
        manifest["resumed"] = True
        path.write_text(json.dumps(manifest, indent=2) + "\n")


@contextmanager
def recovery_signals():
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def prune_successful(state_dir, current, log, *, now=None):
    now = time.time() if now is None else now
    histories = []
    for directory in state_dir.iterdir():
        if (directory.is_symlink() or not directory.is_dir()
                or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-(all|codex|hermes)-[a-z0-9_]{8}", directory.name)):
            continue
        metadata = directory / "result.json"
        try:
            if metadata.is_symlink():
                continue
            result = json.loads(metadata.read_text())
            if not isinstance(result, dict) or result.get("status") not in ("succeeded", "unchanged"):
                continue
            stamp = datetime.strptime(directory.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
            histories.append((stamp, directory))
        except (OSError, ValueError):
            continue
    for stamp, directory in sorted(histories, reverse=True)[3:]:
        if directory != current and now - stamp > 30 * 86400:
            try:
                shutil.rmtree(directory)
            except OSError as error:
                log(f"WARNING: could not prune {directory}: {error}")


def mark_reboot_required(state_dir, target, log, *, booted=Path("/run/booted-system")):
    marker = state_dir / "reboot-required"
    try:
        running_kernel = (booted / "kernel").resolve(strict=True)
        desired_kernel = (target / "kernel").resolve(strict=True)
    except (OSError, RuntimeError):
        log("WARNING: could not compare booted and deployed kernels; check reboot status manually")
        return
    if running_kernel == desired_kernel:
        marker.unlink(missing_ok=True)
    else:
        temporary = state_dir / "reboot-required.tmp"
        temporary.write_text(json.dumps({"booted_kernel": str(running_kernel),
                                          "deployed_kernel": str(desired_kernel)}) + "\n")
        temporary.replace(marker)
        log(f"Kernel update requires a controlled reboot; recorded in {marker}")


@contextmanager
def host_deployment_lock(path):
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def command(args, log):
    log("$ " + " ".join(map(str, args)))
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=4 * 60 * 60)
    if result.stdout:
        log(result.stdout.rstrip(), console=False)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}\n"
                           + result.stdout[-8000:])
    return result.stdout


def curl(run, url, *options):
    return run(["curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                *options, url])


def hermes_status(run):
    return json.loads(curl(run, "http://127.0.0.1:9119/api/status"))


def health_check(run, expected_hermes=None):
    # On-demand Sonarr/Radarr and external Internet access are not required.
    for name in SERVICES:
        run(["systemctl", "is-active", "--quiet", name + ".service"])
    curl(run, "http://127.0.0.1:8080/")
    dns = run(["dig", "+time=5", "+tries=1", "@127.0.0.1", "localhost", "A"])
    if "status: NOERROR" not in dns or not re.search(r"\b127\.0\.0\.1\b", dns):
        raise RuntimeError("Local AdGuard DNS lookup failed")
    nextcloud = json.loads(curl(run, f"https://{HOST}/status.php",
                               "--resolve", f"{HOST}:443:127.0.0.1"))
    if (nextcloud.get("installed") is not True
            or nextcloud.get("maintenance") is not False
            or nextcloud.get("needsDbUpgrade") is not False):
        raise RuntimeError("Nextcloud is not ready: " + json.dumps(nextcloud))
    curl(run, "http://127.0.0.1:8222/alive")
    if curl(run, "http://127.0.0.1:8096/health").strip() != "Healthy":
        raise RuntimeError("Jellyfin health check failed")
    status = hermes_status(run)
    if status.get("gateway_running") is not True or status.get("overall") not in (None, "ok"):
        raise RuntimeError("Hermes gateway is not healthy")
    if expected_hermes is not None and status.get("version") != expected_hermes:
        raise RuntimeError(f"Hermes reports {status.get('version')}; expected {expected_hermes}")
    endpoint_health = json.loads(curl(
        run, "https://hermes.amahi.dev/health?deep=1", "--noproxy", "*",
        "--resolve", "hermes.amahi.dev:443:100.91.234.17"))
    if endpoint_health.get("status") != "ok":
        raise RuntimeError("Hermes WebUI HTTPS endpoint is not healthy")
    endpoint_auth = json.loads(curl(
        run, "https://hermes.amahi.dev/api/auth/status", "--noproxy", "*",
        "--resolve", "hermes.amahi.dev:443:100.91.234.17"))
    if (endpoint_auth.get("auth_enabled") is not True
            or endpoint_auth.get("password_auth_enabled") is not True
            or endpoint_auth.get("logged_in") is not False):
        raise RuntimeError("Hermes WebUI HTTPS endpoint does not require password login")
    for name, host, path in (
        ("Hermes WebUI", "hermes.amahi.dev", "/api/sessions"),
        ("Transmission", "buildfleet-server.tailcb7cdb.ts.net", "/transmission/rpc"),
    ):
        code = run([
            "curl", "--silent", "--show-error", "--max-time", "10", "--noproxy", "*",
            "--resolve", f"{host}:443:100.91.234.17",
            "--output", "/dev/null", "--write-out", "%{http_code}", f"https://{host}{path}",
        ]).strip()
        if code != "401":
            raise RuntimeError(f"{name} HTTPS endpoint did not require login: HTTP {code}")


def recover_services(run, *, maintenance_lock=Path("/run/lock/nixos-flake-update.lock"),
                     deployment_lock=Path("/run/lock/host-deployment.lock")):
    with ExitStack() as locks:
        for path in (maintenance_lock, deployment_lock):
            lock = locks.enter_context(path.open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Maintenance is running; skipping service recovery until the next timer")
                return False
        for name in SERVICES:
            unit = name + ".service"
            state = run(["systemctl", "show", "--property=ActiveState", "--value", unit]).strip()
            if state in ("inactive", "failed"):
                try:
                    run(["systemctl", "start", unit])
                except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                    print(f"Could not start {unit}: {error}")
        health_check(run)
        print("All critical services are running and healthy")
        return True


def wait_healthy(run, expected_hermes=None, *, attempts=18, sleep=time.sleep):
    for attempt in range(attempts):
        try:
            health_check(run, expected_hermes)
            return
        except (RuntimeError, ValueError):
            if attempt == attempts - 1:
                raise
            sleep(5)


def running_hermes_package(run):
    pid = int(run(["systemctl", "show", "--property=MainPID", "--value",
                   "hermes-agent.service"]).strip())
    if pid <= 0:
        raise RuntimeError("Hermes gateway has no running process")
    args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    for arg in args:
        if re.fullmatch(r"/nix/store/[^/]+/bin/hermes", arg):
            return arg.removesuffix("/bin/hermes")
    raise RuntimeError("Cannot identify the managed Hermes gateway package")


def require_hermes_idle(run):
    status = hermes_status(run)
    # Hermes' NAS lifecycle contract counts in-flight work; active_sessions is five-minute history.
    if not (status.get("gateway_running") is True
            and status.get("gateway_busy") is False
            and status.get("gateway_drainable") is True
            and status.get("active_agents") == 0):
        raise RuntimeError("Hermes has active or unknown work; retry the update when idle")


def refresh_hermes(run, expected):
    if running_hermes_package(run) != expected:
        require_hermes_idle(run)
        run(["systemctl", "restart", "hermes-agent.service"])
        if running_hermes_package(run) != expected:
            raise RuntimeError("Restarted Hermes gateway still uses the previous package")


def update_hermes_pin(candidate, run):
    release = json.loads(curl(run, "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest",
                              "--retry", "3", "-H", "Accept: application/vnd.github+json"))
    tag = release.get("tag_name", "")
    # Upstream has used both v2026.9.24 and v0.22.2 stable release tags.
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise RuntimeError(f"Unexpected stable Hermes release: {tag!r}")
    flake = candidate / "flake.nix"
    replacement = f'    hermes-agent.url = "github:NousResearch/hermes-agent/{tag}";'
    content, count = re.subn(r'^    hermes-agent\.url = ".*";$', replacement,
                             flake.read_text(), flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError("Cannot identify one Hermes input in flake.nix")
    flake.write_text(content)


def update_service_pins(candidate, run):
    """Refresh exact, tested source pins that nix flake update cannot advance."""
    releases = json.loads(curl(run, "https://nodejs.org/dist/index.json"))
    versions = [item["version"] for item in releases
                if re.fullmatch(r"v24\.[0-9]+\.[0-9]+", item.get("version", ""))]
    if not versions:
        raise RuntimeError("Node index contains no stable Node 24 release")
    node = max(versions, key=lambda value: tuple(map(int, value[1:].split("."))))[1:]
    image = f"node:{node}-bookworm-slim"
    run(["docker", "pull", image])
    digests = json.loads(run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image]))
    digest = next((value.split("@", 1)[1] for value in digests
                   if re.fullmatch(r"(?:docker.io/library/)?node@sha256:[a-f0-9]{64}", value)), None)
    if digest is None:
        raise RuntimeError("Cannot identify the official Node image digest")
    release = json.loads(curl(run, "https://api.github.com/repos/oven-sh/bun/releases/latest"))
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"bun-v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise RuntimeError(f"Unexpected stable Bun release: {tag!r}")
    dockerfile = candidate / "development/Dockerfile"
    content, count = re.subn(r"^FROM node:24[^\n]*$", f"FROM {image}@{digest}", dockerfile.read_text(), flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError("Cannot identify the Node 24 development image pin")
    content, count = re.subn(r"^ARG BUN_VERSION=.+$", f"ARG BUN_VERSION={tag[5:]}", content, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError("Cannot identify the development Bun pin")
    # Advance manager-owned tool pins with the image, rather than modifying a
    # running agent's executable in place. User plugin/skill locks stay separate.
    for name, url in (
        ("CODEX_VERSION", "https://registry.npmjs.org/@openai/codex/latest"),
        ("CLAUDE_VERSION", "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"),
        ("UV_VERSION", "https://pypi.org/pypi/uv/json"),
    ):
        release = json.loads(curl(run, url))
        version = release.get("version") if name != "UV_VERSION" else release.get("info", {}).get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise RuntimeError(f"Unexpected stable development tool release: {name}")
        pattern = rf"^ARG {name}=([0-9]+\.[0-9]+\.[0-9]+)$"
        previous = re.findall(pattern, content, flags=re.MULTILINE)
        if len(previous) != 1:
            raise RuntimeError(f"Cannot identify the development tool pin: {name}")
        if tuple(map(int, version.split("."))) > tuple(map(int, previous[0].split("."))):
            content = re.sub(pattern, f"ARG {name}={version}", content, flags=re.MULTILINE)
    dockerfile.write_text(content)
    release = json.loads(curl(run, "https://api.github.com/repos/JuliusBrussee/caveman/releases/latest"))
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise RuntimeError(f"Unexpected stable Caveman release: {tag!r}")
    flake = candidate / "flake.nix"
    content, count = re.subn(r'url = "github:JuliusBrussee/caveman/[^"\n]+";',
                             f'url = "github:JuliusBrussee/caveman/{tag}";', flake.read_text())
    if count != 1:
        raise RuntimeError("Cannot identify the managed Caveman input")
    flake.write_text(content)


def gateway_package_from_system(system, *, read_script=None):
    """Follow the built unit's wrapper chain, including Nix dependency grafts."""
    root = system / "etc/systemd/system"
    unit = "hermes-agent.service"
    starts = []
    for file in [root / unit, *sorted((root / (unit + ".d")).glob("*.conf"))]:
        section = ""
        for line in file.read_text().splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Service]" and line.startswith("ExecStart="):
                value = line.removeprefix("ExecStart=").strip()
                if value:
                    starts.append(shlex.split(value))
                else:
                    starts.clear()
    if (len(starts) != 1 or len(starts[0]) != 1
            or not re.fullmatch(r"/nix/store/[^/\s]+-hermes-gateway", starts[0][0])):
        raise RuntimeError("Cannot identify the built Hermes gateway ExecStart wrapper")
    read_script = read_script or (lambda path: Path(path).read_text())
    wrapper = read_script(starts[0][0])
    launches = [shlex.split(line.strip()) for line in wrapper.splitlines()
                if line.strip().startswith("exec ")]
    if (len(launches) != 1 or len(launches[0]) != 3 or launches[0][2] != "gateway"
            or not re.fullmatch(r"/nix/store/[^/\s]+/bin/hermes", launches[0][1])):
        raise RuntimeError("Cannot identify the built Hermes gateway package")
    entry = launches[0][1]
    for _ in range(4):
        script = read_script(entry)
        if re.match(r"^#!\s*/nix/store/[^/\s]+/bin/python[0-9.]*\s*(?:\n|$)", script):
            return entry.removesuffix("/bin/hermes")
        launches = [shlex.split(line.strip()) for line in script.splitlines()
                    if line.strip().startswith("exec ")]
        if (len(launches) != 1 or len(launches[0]) != 3 or launches[0][2] != "$@"
                or not re.fullmatch(r"/nix/store/[^/\s]+/bin/hermes", launches[0][1])):
            break
        entry = launches[0][1]
    raise RuntimeError("Cannot identify the final Hermes Python entry point")


def expected_hermes(flake, run, system):
    expected = json.loads(run(["nix", "eval", "--json", "--no-write-lock-file",
                               f"{flake}#{CONFIG}.services.hermes-agent.package", "--apply",
                               "p: { path = p.outPath; version = p.version; }"]))
    expected["path"] = gateway_package_from_system(system)
    return expected


def sync_pins(candidate, source, canonical, log):
    for name in ("flake.nix", "flake.lock", "development/Dockerfile"):
        current = canonical / name
        wanted = (candidate / name).read_bytes()
        baseline = (source / name).read_bytes()
        if wanted == baseline:
            continue
        if not current.is_file() or current.is_symlink() or current.read_bytes() != baseline:
            log(f"WARNING: left administrator edits in {current}; reconcile its pins before a manual rebuild")
            continue
        original = current.stat()
        with tempfile.NamedTemporaryFile(dir=current.parent, prefix=f".{current.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(wanted)
        try:
            temporary.chmod(stat.S_IMODE(original.st_mode))
            if os.geteuid() == 0:
                os.chown(temporary, original.st_uid, original.st_gid)
            # Recheck after preparing the replacement so a concurrent edit wins.
            if current.stat() != original or current.read_bytes() != baseline:
                log(f"WARNING: {current} changed during maintenance; its pins were not synchronized")
                continue
            temporary.replace(current)
            log(f"Synchronized validated pins to {current}")
        finally:
            temporary.unlink(missing_ok=True)


def update(task, *, source=Path("/etc/nixos-deployed"),
           state_dir=Path("/var/lib/nixos-updates"), canonical=Path("/etc/nixos"),
           backup_hook=None, current_system=Path("/run/current-system"),
           deployment_lock=Path("/run/lock/host-deployment.lock"),
           run=None, verify=wait_healthy, fleet_factory=None):
    source = source.resolve(strict=True)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{task}-"
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=state_dir))
    candidate = directory / "candidate"
    result = {"task": task, "source": str(source), "directory": str(directory), "status": "preparing"}

    def log(message, *, console=True):
        with (directory / "update.log").open("a") as handle:
            handle.write(message + "\n")
        if console:
            print(message, flush=True)

    def save(status, **details):
        result.update(status=status, **details)
        content = json.dumps(result, indent=2) + "\n"
        (directory / "result.json").write_text(content)
        temporary = state_dir / "latest.json.tmp"
        temporary.write_text(content)
        temporary.replace(state_dir / "latest.json")

    run = run or (lambda args: command(args, log))
    log(f"Preparing {task} update from {source}; retained state: {directory}")
    try:
        shutil.copytree(source, candidate, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "result"))
        for path in (candidate, *candidate.rglob("*")):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        flake = f"path:{candidate}"
        if task == "all":
            update_service_pins(candidate, run)
        if task in ("all", "hermes"):
            update_hermes_pin(candidate, run)
        inputs = [] if task == "all" else ["codex-cli" if task == "codex" else "hermes-agent"]
        run(["nix", "flake", "update", *inputs, "--flake", flake])
        if all((candidate / name).read_bytes() == (source / name).read_bytes()
               for name in ("flake.nix", "flake.lock", "development/Dockerfile")):
            with host_deployment_lock(deployment_lock):
                expected = expected_hermes(flake, run, current_system.resolve(strict=True))
                refresh_hermes(run, expected["path"])
                verify(run, expected["version"])
            save("unchanged")
            mark_reboot_required(state_dir, current_system, log)
            prune_successful(state_dir, directory, log)
            log("Pins are unchanged and services are healthy")
            return directory

        save("building")
        run(["nix", "flake", "check", "--no-write-lock-file", flake])
        run(["nix", "build", "--no-write-lock-file", "--out-link", str(directory / "system"),
             f"{flake}#{CONFIG}.system.build.toplevel"])
        target = (directory / "system").resolve()
        expected = expected_hermes(flake, run, target)
        with host_deployment_lock(deployment_lock), ExitStack() as recovery:
            previous = current_system.resolve(strict=True)
            old_hermes = running_hermes_package(run)
            apps_changed = application_units_changed(previous, target)
            if apps_changed or old_hermes != expected["path"]:
                require_hermes_idle(run)
            if apps_changed and not backup_hook:
                raise RuntimeError("A service-state backup/restore hook is required before deployment")
            backup = directory / "backup"
            fleet_directory = directory / "fleet"
            fleet = None
            guard = nullcontext()
            docker_changed = docker_units_changed(previous, target)
            if docker_changed:
                if fleet_factory is None:
                    fleet = fleet_module()
                    fleet_factory = fleet.guard
                guard = fleet_factory(fleet_directory)
            backed_up = deployment_started = rolled_back = False
            rollback_errors = []

            def rollback(error):
                nonlocal rolled_back
                rolled_back = True
                recovery.enter_context(recovery_signals())
                log(f"Deployment failed; restoring {previous}: {error}")
                save("rolling-back", error=str(error))
                try:
                    if apps_changed or old_hermes != expected["path"]:
                        state = run(["systemctl", "show", "--property=ActiveState", "--value",
                                     "hermes-agent.service"]).strip()
                        if state not in ("inactive", "failed"):
                            require_hermes_idle(run)
                except BaseException as rollback_error:
                    rollback_errors.append(
                        "Application rollback could not safely complete; active or unknown Hermes work "
                        "was left intact: " + str(rollback_error))
                    return
                commands = []
                if backed_up:
                    commands.append([str(backup_hook), "restore", str(backup)])
                commands.extend([
                    ["nix-env", "--profile", "/nix/var/nix/profiles/system", "--set", str(previous)],
                    [str(previous / "bin/switch-to-configuration"), "switch"],
                ])
                if backed_up:
                    commands.append([str(backup_hook), "resume", str(backup)])
                for args in commands:
                    try:
                        run(args)
                    except BaseException as rollback_error:
                        # A failed service activation must not skip database recovery.
                        rollback_errors.append(str(rollback_error))
                        log(f"Rollback step failed: {rollback_error}")
                        if backed_up and args[:2] == [str(backup_hook), "restore"]:
                            # Do not activate old code over unrestored state, or bypass
                            # the restore hook's last check for newly accepted work.
                            return
                try:
                    refresh_hermes(run, old_hermes)
                    verify(run)
                except BaseException as rollback_error:
                    rollback_errors.append(str(rollback_error))

            try:
                with guard:
                    try:
                        if apps_changed:
                            save("backing-up", previous_system=str(previous))
                            try:
                                run([str(backup_hook), "backup", str(backup)])
                                backed_up = True
                            except BaseException:
                                run([str(backup_hook), "resume", str(backup)])
                                raise
                        deployment_started = True
                        save("deploying", previous_system=str(previous))
                        run(["nixos-rebuild", "switch", "--flake", flake, "--no-write-lock-file"])
                        if backed_up:
                            run([str(backup_hook), "resume", str(backup)])
                        refresh_hermes(run, expected["path"])
                        verify(run, expected["version"])
                    except BaseException as error:
                        if deployment_started:
                            # Keep the fleet stopped until the old Docker is active.
                            rollback(error)
                        raise
            except BaseException as error:
                if deployment_started and not rolled_back:
                    if docker_changed:
                        # Partial resume can already have accepted new work.
                        # Re-gate dispatch before replacing Docker again.
                        try:
                            with fleet_factory(directory / "fleet-rollback"):
                                rollback(error)
                        except BaseException as fleet_error:
                            rollback_errors.append(
                                "Docker rollback could not safely complete; active work was left intact: "
                                + str(fleet_error))
                    else:
                        rollback(error)
                if fleet is not None:
                    try:
                        recover_fleet(fleet, fleet_directory)
                    except BaseException as fleet_error:
                        rollback_errors.append(str(fleet_error))
                if not deployment_started:
                    raise
                if rollback_errors:
                    save("rollback-failed", error=str(error), rollback_errors=rollback_errors)
                    raise RuntimeError(f"Deployment failed: {error}; rollback needs attention: "
                                       f"{'; '.join(rollback_errors)}; inspect {directory}") from error
                save("rolled-back", error=str(error))
                raise RuntimeError(f"Deployment failed and prior system/state were restored; inspect {directory}") from error
        sync_pins(candidate, source, canonical, log)
        save("succeeded", system=str((directory / "system").resolve()))
        mark_reboot_required(state_dir, target, log)
        prune_successful(state_dir, directory, log)
        log("Update deployed and service checks passed")
        return directory
    except BaseException as error:
        if result["status"] not in ("rolled-back", "rollback-failed"):
            save("failed", error=str(error))
        log(str(error))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=("all", "codex", "hermes"), nargs="?", default="all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check service health without changing the host")
    mode.add_argument("--recover", action="store_true", help="Start stopped critical services and check health")
    parser.add_argument("--source", type=Path, default=Path("/etc/nixos-deployed"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/nixos-updates"))
    parser.add_argument("--backup-hook", type=Path)
    args = parser.parse_args()
    os.umask(0o077)

    def interrupted(signum, frame):
        raise RuntimeError(f"Maintenance interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if args.check or args.recover:
            def check_log(message, *, console=True):
                if console:
                    print(message)
            run = lambda command_args: command(command_args, check_log)
            if args.recover:
                recover_services(run)
            else:
                health_check(run)
                print("All managed service health checks passed")
            return 0
        with Path("/run/lock/nixos-flake-update.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Another system update is running; retrying on the next timer")
                return 0
            update(args.task, source=args.source, state_dir=args.state_dir, backup_hook=args.backup_hook)
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"System maintenance failed: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
