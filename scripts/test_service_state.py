"""Regression checks for service-state backup and recovery without live services."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("service_state", Path(__file__).with_name("service-state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "backup"
        self.paths = {"nextcloud-config": self.root / "config", "vaultwarden": self.root / "vault",
                      "hermes": self.root / "hermes"}
        for path in self.paths.values():
            path.mkdir()
            (path / "state.db").write_text("old state")
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        (self.uploads / "user-file").write_text("keep uploads")
        self.original_active = {"hermes-agent.service", "hermes-dashboard.service", "phpfpm-nextcloud.service",
                                "vaultwarden.service", "nextcloud-cron.timer", "media-organizer.timer"}
        self.active = self.original_active.copy()
        self.calls = []
        self.fail_dump = False
        self.busy = False
        self.maintenance = False
        self.fail_maintenance_off = False

    def run_command(self, args, *, output=None, input_file=None):
        self.calls.append(args)
        if args[:2] == ["systemctl", "show"]:
            return "active" if args[-1] in self.active else "inactive"
        if args[:2] == ["systemctl", "stop"]:
            self.active.discard(args[-1])
        if args[:2] == ["systemctl", "start"]:
            self.active.add(args[-1])
        if args[:2] == [state.OCC, "status"]:
            return json.dumps({"maintenance": self.maintenance})
        if args[:2] == [state.OCC, "maintenance:mode"]:
            if args[-1] == "--off" and self.fail_maintenance_off:
                raise RuntimeError("injected maintenance mode failure")
            self.maintenance = args[-1] == "--on"
        if args[0] == "curl":
            return json.dumps({"gateway_running": True, "gateway_busy": self.busy,
                               "gateway_drainable": True, "active_agents": int(self.busy), "active_sessions": 1})
        if state.PG_DUMP in args:
            if self.fail_dump:
                raise RuntimeError("injected dump failure")
            output.write_bytes(b"PGDMP snapshot")
        if state.PG_RESTORE in args:
            self.assertEqual(input_file.read_bytes(), b"PGDMP snapshot")
        if args[0] == "rsync":
            source, destination = Path(args[-2]), Path(args[-1])
            if "--delete" in args:
                for path in destination.iterdir():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        return ""

    def backup(self):
        state.backup(self.directory, self.run_command, paths=self.paths)

    def resume(self):
        state.resume(self.directory, self.run_command, paths=self.paths)

    def test_idle_lifecycle_contract_ignores_history_and_requires_every_field(self):
        idle = {"gateway_running": True, "gateway_busy": False, "gateway_drainable": True,
                "active_agents": 0, "active_sessions": 1}

        def check(status):
            def commands(args):
                return json.dumps(status) if args[0] == "curl" else self.run_command(args)
            state.require_hermes_idle(commands)

        check(idle)
        for field in ("gateway_running", "gateway_busy", "gateway_drainable", "active_agents"):
            with self.subTest(missing=field):
                with self.assertRaisesRegex(RuntimeError, "Hermes is busy"):
                    check({key: value for key, value in idle.items() if key != field})

    def test_backup_quiesces_then_resume_restores_only_original_active_units(self):
        self.backup()
        self.assertFalse(self.active)
        self.assertTrue(self.maintenance)
        self.assertTrue(json.loads((self.directory / "manifest.json").read_text())["complete"])
        first_copy = next(i for i, args in enumerate(self.calls) if args[0] == "rsync")
        stops = [i for i, args in enumerate(self.calls) if args[:2] == ["systemctl", "stop"]]
        self.assertLess(max(stops), first_copy)
        self.resume()
        self.assertEqual(self.active, self.original_active)
        self.assertFalse(self.maintenance)
        self.assertNotIn("sonarr.service", self.active)
        self.assertNotIn("radarr.service", self.active)

    def test_failed_dump_resumes_original_services_and_leaves_incomplete_backup(self):
        self.fail_dump = True
        with self.assertRaisesRegex(RuntimeError, "injected dump"):
            self.backup()
        self.assertEqual(self.active, self.original_active)
        self.assertFalse(self.maintenance)
        self.assertFalse(json.loads((self.directory / "manifest.json").read_text())["complete"])
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            state.restore(self.directory, self.run_command, paths=self.paths)

    def test_failed_maintenance_toggle_still_restarts_all_original_services(self):
        self.fail_dump = True
        self.fail_maintenance_off = True
        with self.assertRaisesRegex(RuntimeError, "services could not fully resume"):
            self.backup()
        self.assertEqual(self.active, self.original_active)

    def test_existing_maintenance_mode_defers_backup_without_changing_services(self):
        self.maintenance = True
        with self.assertRaisesRegex(RuntimeError, "already in maintenance mode"):
            self.backup()
        self.assertTrue(self.maintenance)
        self.assertEqual(self.active, self.original_active)
        self.assertFalse(any(args[:2] in (["systemctl", "stop"], ["systemctl", "start"],
                                         [state.OCC, "maintenance:mode"])
                             or args[0] == "rsync" or state.PG_DUMP in args for args in self.calls))
        self.assertFalse((self.directory / "manifest.json").exists())

    def test_restore_clears_temporary_maintenance_after_database_before_activation(self):
        self.backup()
        self.assertTrue(self.maintenance)
        before = len(self.calls)
        state.restore(self.directory, self.run_command, paths=self.paths)
        self.assertFalse(self.maintenance)
        self.assertFalse(self.active)
        restored = self.calls[before:]
        copy_end = max(i for i, args in enumerate(restored) if args[0] == "rsync")
        database = next(i for i, args in enumerate(restored) if state.PG_RESTORE in args)
        toggle = restored.index([state.OCC, "maintenance:mode", "--off"])
        self.assertLess(copy_end, database)
        self.assertLess(database, toggle)
        self.assertEqual(toggle, len(restored) - 1)
        self.assertFalse(any(args[:2] == ["systemctl", "start"] for args in restored))

    def test_restore_preserves_legacy_original_maintenance_mode(self):
        self.backup()
        manifest_path = self.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["maintenance_before"] = True
        manifest_path.write_text(json.dumps(manifest))
        self.maintenance = False
        state.restore(self.directory, self.run_command, paths=self.paths)
        self.assertTrue(self.maintenance)
        self.assertFalse(self.active)
        self.resume()
        self.assertTrue(self.maintenance)
        self.assertEqual(self.active, self.original_active)

    def test_restore_replaces_application_state_without_touching_uploads(self):
        self.backup()
        self.resume()
        for path in self.paths.values():
            (path / "state.db").write_text("migrated state")
            (path / "new-schema.db").write_text("remove on rollback")
        state.restore(self.directory, self.run_command, paths=self.paths)
        self.assertFalse(self.active)
        for path in self.paths.values():
            self.assertEqual((path / "state.db").read_text(), "old state")
            self.assertFalse((path / "new-schema.db").exists())
        self.assertEqual((self.uploads / "user-file").read_text(), "keep uploads")
        self.assertTrue(any(state.PG_RESTORE in args and "--single-transaction" in args for args in self.calls))
        self.resume()
        self.assertEqual(self.active, self.original_active)

    def test_rollback_recovers_a_failed_gateway_without_its_status_endpoint(self):
        self.backup()
        self.resume()
        self.active.discard("hermes-agent.service")
        self.busy = True
        before = len(self.calls)
        state.restore(self.directory, self.run_command, paths=self.paths)
        self.assertFalse(self.active)
        self.assertFalse(any(args[0] == "curl" for args in self.calls[before:]))

    def test_rollback_preserves_new_work_accepted_after_resume(self):
        self.backup()
        self.resume()
        self.busy = True
        live = self.paths["hermes"] / "state.db"
        live.write_text("new conversation")
        before = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, "Hermes is busy"):
            state.restore(self.directory, self.run_command, paths=self.paths)
        self.assertEqual(self.active, self.original_active)
        self.assertEqual(live.read_text(), "new conversation")
        self.assertFalse(any(args[:2] == ["systemctl", "stop"] or args[0] == "rsync"
                             or state.PG_RESTORE in args for args in self.calls[before:]))
        self.assertTrue(json.loads((self.directory / "manifest.json").read_text())["resumed"])

    def test_active_gateway_with_unknown_status_blocks_restore(self):
        self.backup()
        self.resume()
        before = len(self.calls)
        def unavailable(args, **kwargs):
            if args[0] == "curl":
                raise RuntimeError("status endpoint unavailable")
            return self.run_command(args, **kwargs)
        with self.assertRaisesRegex(RuntimeError, "status endpoint unavailable"):
            state.restore(self.directory, unavailable, paths=self.paths)
        self.assertEqual(self.active, self.original_active)
        self.assertFalse(any(args[:2] == ["systemctl", "stop"] or args[0] == "rsync"
                             or state.PG_RESTORE in args for args in self.calls[before:]))

    def test_manifest_cannot_add_arbitrary_destinations_or_services(self):
        self.backup()
        self.resume()
        manifest_path = self.directory / "manifest.json"
        original = json.loads(manifest_path.read_text())
        for change in ({"paths": {"../../../uploads": str(self.uploads)}},
                       {"active_units": ["unrelated.service"]}):
            with self.subTest(change=change):
                manifest_path.write_text(json.dumps(original | change))
                calls = len(self.calls)
                with self.assertRaises(RuntimeError):
                    state.restore(self.directory, self.run_command, paths=self.paths)
                self.assertEqual(len(self.calls), calls)

    def test_changed_top_level_symlink_is_rejected_before_any_live_action(self):
        self.backup()
        self.resume()
        vault = self.paths["vaultwarden"]
        shutil.rmtree(vault)
        vault.symlink_to(self.uploads)
        calls = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, "Unexpected state directory symlink"):
            state.restore(self.directory, self.run_command, paths=self.paths)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((self.uploads / "user-file").read_text(), "keep uploads")

    def test_busy_hermes_does_not_get_stopped(self):
        self.busy = True
        with self.assertRaisesRegex(RuntimeError, "Hermes is busy"):
            self.backup()
        self.assertEqual(self.active, self.original_active)
        self.assertFalse(self.maintenance)
        self.assertFalse(any(args[:2] == ["systemctl", "stop"] for args in self.calls))

    def test_rsync_preserves_ownership_acls_and_excludes_runtime_binaries(self):
        self.backup()
        copies = [args for args in self.calls if args[0] == "rsync"]
        self.assertTrue(all("-aHAX" in args and "--numeric-ids" in args for args in copies))
        hermes = next(args for args in copies if args[-2] == str(self.paths["hermes"]) + "/")
        self.assertIn("**/.venv/", hermes)
        self.assertIn("**/node_modules/", hermes)

    @unittest.skipUnless(shutil.which("rsync"), "rsync is needed for the filesystem integration check")
    def test_real_rsync_keeps_excluded_dependencies_and_never_follows_symlinks(self):
        source = self.paths["hermes"]
        snapshot = self.root / "hermes-snapshot"
        snapshot.mkdir()
        for name in (".venv", "cache", "node_modules"):
            (source / name).mkdir()
            (source / name / "artifact").write_text("dependency")
        (source / "uploads-link").symlink_to(self.uploads)

        def run(args):
            subprocess.run(args, check=True, capture_output=True)

        state.sync(source, snapshot, "hermes", run)
        self.assertTrue((snapshot / "uploads-link").is_symlink())
        self.assertFalse((snapshot / ".venv").exists())
        self.assertFalse((snapshot / "cache").exists())
        self.assertFalse((snapshot / "node_modules").exists())
        (source / "state.db").write_text("migrated state")
        (source / "new-schema.db").touch()
        state.sync(snapshot, source, "hermes", run, restore=True)
        self.assertEqual((source / "state.db").read_text(), "old state")
        self.assertFalse((source / "new-schema.db").exists())
        self.assertEqual((source / ".venv/artifact").read_text(), "dependency")
        self.assertEqual((self.uploads / "user-file").read_text(), "keep uploads")


if __name__ == "__main__":
    unittest.main()
