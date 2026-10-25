"""Exercise container maintenance without contacting Docker or stopping services."""

import importlib.util
import fcntl
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("container_update", Path(__file__).with_name("container-update.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class FakeCommands:
    def __init__(self, root, kind):
        self.root = root
        self.kind = kind
        self.calls = []
        self.names = ["adguardhome"] if kind == "adguard" else ["gluetun", "transmission-vpn"]
        self.images = (["adguard/adguardhome:latest"] if kind == "adguard" else
                       ["qmcgaw/gluetun:latest", "lscr.io/linuxserver/transmission:latest"])
        self.tags = {image: f"old-{name}" for name, image in zip(self.names, self.images)}
        self.running = {name: self.tags[image] for name, image in zip(self.names, self.images)}
        self.pull_fail = None
        self.start_fail = False
        self.health_fail = False
        self.stale_namespace = False
        self.dns_failure = False
        self.interrupt_start = False
        self.backup_failure = False
        self.changed = True
        self.config = root / "config"
        self.configs = [self.config]
        if kind == "vpn":
            self.configs.append(root / "gluetun")
        for config in self.configs:
            config.mkdir()
            (config / "settings").write_text("original")

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:3] == ("docker", "image", "tag"):
            self.tags[args[4]] = args[3]
        elif args[:3] == ("docker", "image", "rm"):
            del self.tags[args[3]]
        elif args[:2] == ("docker", "pull"):
            if args[2] == self.pull_fail:
                raise subprocess.CalledProcessError(1, args)
            if self.changed:
                self.tags[args[2]] = "new-" + args[2]
        elif args[:3] == ("docker", "image", "inspect"):
            return self.tags[args[-1]]
        elif args[:2] == ("docker", "inspect"):
            if args[3] == "{{.Image}}":
                return self.running[args[-1]]
            if args[3] == "{{.Id}}":
                return "stale-id" if args[-1] == "stale" else "gluetun-id"
            if args[3] == "{{.HostConfig.NetworkMode}}":
                return "container:stale" if self.stale_namespace else "container:gluetun-id"
            if args[3] == "{{.State.Health.Status}}":
                return "healthy"
        elif args[:2] == ("systemctl", "start"):
            self.running = {name: self.tags[image] for name, image in zip(self.names, self.images)}
            if any(image.startswith("new-") for image in self.running.values()):
                for config in self.configs:
                    (config / "settings").write_text("migrated")
                    (config / "added-by-new-version").write_text("new")
                if self.interrupt_start:
                    updater.interrupted(15, None)
                if self.start_fail:
                    raise subprocess.CalledProcessError(1, args)
        elif args[0] == "cp":
            if self.backup_failure:
                raise subprocess.CalledProcessError(1, args)
            shutil.copytree(args[-2], args[-1])
        elif args[0] == "curl" or args[:2] == ("docker", "exec"):
            if self.health_fail and any(image.startswith("new-") for image in self.running.values()):
                raise subprocess.CalledProcessError(1, args)
        elif args[0] == "dig":
            if self.dns_failure and any(image.startswith("new-") for image in self.running.values()):
                return "status: SERVFAIL"
            return "status: NOERROR"
        return ""


class ContainerUpdateTests(unittest.TestCase):
    def test_main_serializes_with_host_deployments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_path = Path

            def mapped(value):
                if str(value) == "/var/lib/container-updates":
                    return root
                if str(value) == "/run/lock/host-deployment.lock":
                    return root / "deployment.lock"
                return real_path(value)

            def update(kind, state_dir):
                with (root / "deployment.lock").open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            with patch.object(updater, "Path", mapped), patch.object(updater, "update", side_effect=update) as call, patch.object(updater.sys, "argv", ["container-update.py", "vpn"]), patch.object(updater.signal, "signal"):
                updater.main()
                call.assert_called_once_with("vpn", root)

    def test_vpn_post_start_health_probe_never_takes_deployment_lock(self):
        for mode, kind in (("vpn-ready", "vpn-ready"), ("transmission-ready", "vpn")):
            with self.subTest(mode=mode), patch.object(updater, "Path") as path, patch.object(updater, "wait_healthy") as health, patch.object(updater.sys, "argv", ["container-update.py", mode]):
                updater.main()
                path.assert_not_called()
                health.assert_called_once_with(kind)

    def test_transmission_start_waits_for_authenticated_rpc(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = FakeCommands(Path(directory), "vpn")
            attempts = 0

            def delayed(*args, **kwargs):
                nonlocal attempts
                if "--authenv --session-info" in args[-1]:
                    attempts += 1
                    if attempts < 3:
                        raise subprocess.CalledProcessError(1, args)
                return commands(*args, **kwargs)

            with patch.object(updater, "run", side_effect=delayed), patch.object(updater.time, "sleep") as sleep, patch.object(updater.sys, "argv", ["container-update.py", "transmission-ready"]):
                updater.main()
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertFalse(any(call[0] == "systemctl" for call in commands.calls))

    def test_transmission_start_fails_closed_after_bounded_rpc_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = FakeCommands(Path(directory), "vpn")
            attempts = 0

            def unavailable(*args, **kwargs):
                nonlocal attempts
                if "--authenv --session-info" in args[-1]:
                    attempts += 1
                    raise subprocess.CalledProcessError(1, args)
                return commands(*args, **kwargs)

            with patch.object(updater, "run", side_effect=unavailable), patch.object(updater.time, "sleep"), self.assertRaisesRegex(RuntimeError, "health check failed"):
                updater.wait_healthy("vpn")
            self.assertEqual(attempts, 60)

    def exercise(self, kind, **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        commands = FakeCommands(root, kind)
        for key, value in options.items():
            setattr(commands, key, value)
        real_path = Path

        def mapped_path(value):
            if str(value) in ("/var/lib/adguardhome/conf", "/var/lib/transmission-vpn"):
                return commands.config
            if str(value) == "/var/lib/gluetun":
                return commands.configs[1]
            return real_path(value)

        with patch.object(updater, "run", commands), patch.object(updater, "Path", mapped_path), \
                patch.object(updater.time, "sleep"), patch.object(updater.signal, "signal"):
            try:
                updater.update(kind, root)
                error = None
            except (RuntimeError, subprocess.SubprocessError) as caught:
                error = caught
        return root, commands, error

    def assert_restored(self, commands):
        for config in commands.configs:
            self.assertEqual((config / "settings").read_text(), "original")
            self.assertFalse((config / "added-by-new-version").exists())
        self.assertTrue(all(commands.tags[image].startswith("old-") for image in commands.images))
        self.assertTrue(all(image.startswith("old-") for image in commands.running.values()))

    def test_success_preserves_new_config_and_only_removes_rollback_tags(self):
        for kind in ("adguard", "vpn"):
            with self.subTest(kind=kind):
                root, commands, error = self.exercise(kind)
                self.assertIsNone(error)
                self.assertEqual((commands.config / "settings").read_text(), "migrated")
                self.assertTrue(all(commands.tags[image].startswith("new-") for image in commands.images))
                removals = [call for call in commands.calls if call[:3] == ("docker", "image", "rm")]
                self.assertEqual(len(removals), len(commands.images))
                self.assertTrue(all(call[3].startswith("buildfleet-container-rollback:") for call in removals))
                self.assertEqual(set(root.iterdir()), set(commands.configs))

    def test_pull_failure_never_stops_services_and_restores_partial_pull(self):
        _, commands, error = self.exercise("vpn", pull_fail="lscr.io/linuxserver/transmission:latest")
        self.assertIsNotNone(error)
        self.assert_restored(commands)
        self.assertFalse(any(call[0] == "systemctl" for call in commands.calls))

    def test_health_failure_restores_images_and_config_for_both_stacks(self):
        for kind in ("adguard", "vpn"):
            with self.subTest(kind=kind):
                root, commands, error = self.exercise(kind, health_fail=True)
                self.assertIsInstance(error, RuntimeError)
                self.assert_restored(commands)
                self.assertTrue(any(item.name.startswith(kind + "-") for item in root.iterdir()))
                self.assertFalse(any(call[:3] == ("docker", "image", "rm") for call in commands.calls))

    def test_start_failure_also_restores_config(self):
        _, commands, error = self.exercise("vpn", start_fail=True)
        self.assertIsNotNone(error)
        self.assert_restored(commands)

    def test_interruption_during_start_also_restores_images_and_config(self):
        _, commands, error = self.exercise("adguard", interrupt_start=True)
        self.assertIsNotNone(error)
        self.assert_restored(commands)

    def test_failed_backup_restarts_old_image_without_changing_live_config(self):
        _, commands, error = self.exercise("vpn", backup_failure=True)
        self.assertIsNotNone(error)
        self.assert_restored(commands)

    def test_dns_servfail_triggers_rollback_even_when_http_succeeds(self):
        _, commands, error = self.exercise("adguard", dns_failure=True)
        self.assertIsNotNone(error)
        self.assert_restored(commands)

    def test_unchanged_images_do_not_stop_services_or_create_backup(self):
        root, commands, error = self.exercise("vpn", changed=False)
        self.assertIsNone(error)
        self.assertFalse(any(call[0] in ("cp", "systemctl") for call in commands.calls))
        self.assertEqual(set(root.iterdir()), set(commands.configs))

    def test_stale_network_namespace_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = FakeCommands(Path(directory), "vpn")
            commands.stale_namespace = True
            with patch.object(updater, "run", commands), self.assertRaisesRegex(RuntimeError, "stale"):
                updater.healthy("vpn")

    def test_health_checks_authenticate_without_printing_password(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = FakeCommands(Path(directory), "vpn")
            with patch.object(updater, "run", commands):
                updater.healthy("vpn")
            self.assertTrue(any('--authenv --session-info' in call[-1] for call in commands.calls))
            self.assertTrue(any("'-P OUTPUT DROP'" in call[-1] for call in commands.calls))


if __name__ == "__main__":
    unittest.main()
