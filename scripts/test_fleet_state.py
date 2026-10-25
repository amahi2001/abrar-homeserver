import contextlib
import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("fleet_state", Path(__file__).with_name("fleet-state.py"))
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)
spec = importlib.util.spec_from_file_location("system_update", Path(__file__).with_name("system-update.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class FleetTests(unittest.TestCase):
    def setUp(self):
        # Nix's build sandbox has no host account database.
        self.owner = fleet.pwd.struct_passwd(("buildfleet", "x", 1042, 2042, "", "/home/buildfleet", "/bin/sh"))
        lookup = patch.object(fleet.pwd, "getpwnam", return_value=self.owner)
        self.owner_lookup = lookup.start()
        self.addCleanup(lookup.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.control = root / "system.control"
        self.canonical = root / "system"
        self.runtime = root / "runtime-system"
        self.store = root / "nix/store"
        self.pause = root / "nixos-fleet-maintenance/paused"
        for path in (self.control, self.canonical, self.runtime, self.store):
            path.mkdir(parents=True)
        for name, value in (("SYSTEM_CONTROL", self.control),
                            ("SYSTEM_UNITS", (self.canonical, self.runtime)), ("NIX_STORE", self.store),
                            ("PAUSE_MARKER", self.pause)):
            replace = patch.object(fleet, name, value)
            replace.start()
            self.addCleanup(replace.stop)

    def unit_target(self, unit="github-actions-app.service"):
        target = self.store / ("a" * 32 + "-unit-" + unit) / unit
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[Service]\nExecStart=/test/runner\n")
        return target

    def test_missing_legacy_runner_unit_is_restored_before_start(self):
        unit = "github-actions-app.service"
        target = self.unit_target(unit)
        runner = Mock()
        runner.container.return_value = {"Image": "current-image"}
        def commands(*args, **kwargs):
            return "not-found" if "--property=LoadState" in args else ""
        with patch.object(fleet, "run", side_effect=commands) as run:
            fleet.resume({"dev": {}, "runners": ["app"], "runner_units": {unit: str(target)}}, runner)
            self.assertEqual((self.control / unit).readlink(), target)
            calls = [call.args for call in run.call_args_list]
            self.assertLess(calls.index(("systemctl", "daemon-reload")),
                            calls.index(("systemctl", "start", unit)))
            runner.wait_online.assert_called_once_with("app", "current-image")

    def test_existing_units_always_win_over_saved_legacy_links(self):
        unit = "github-actions-app.service"
        target = self.unit_target(unit)
        for present in ("loaded", "canonical", "runtime", "control"):
            with self.subTest(present=present):
                runner = Mock()
                runner.container.return_value = {"Image": "current-image"}
                path = {"canonical": self.canonical, "runtime": self.runtime,
                        "control": self.control}.get(present)
                if path is not None:
                    (path / unit).write_text("new unit")
                def commands(*args, **kwargs):
                    return ("loaded" if present == "loaded" else "not-found") if "--property=LoadState" in args else ""
                with patch.object(fleet, "run", side_effect=commands) as run:
                    fleet.resume({"dev": {}, "runners": ["app"], "runner_units": {unit: str(target)}}, runner)
                    run.assert_any_call("systemctl", "start", unit)
                    self.assertNotIn(("systemctl", "daemon-reload"), [call.args for call in run.call_args_list])
                if path is not None:
                    self.assertEqual((path / unit).read_text(), "new unit")
                    (path / unit).unlink()
                self.assertFalse((self.control / unit).exists())

    def test_invalid_saved_runner_target_cannot_create_a_control_unit(self):
        unit = "github-actions-app.service"
        target = self.unit_target(unit)
        escaped = self.store / ("b" * 32 + "-unit-" + unit) / unit
        escaped.parent.mkdir()
        escaped.symlink_to(target)
        missing = self.store / ("c" * 32 + "-unit-" + unit) / unit
        for value in (str(missing), str(escaped),
                      str(self.canonical / unit), "relative/" + unit, None):
            with self.subTest(value=value), patch.object(fleet, "run", return_value="not-found") as run:
                runner = Mock()
                with self.assertRaisesRegex(RuntimeError, "Invalid saved runner unit target"):
                    fleet.resume({"dev": {}, "runners": ["app"], "runner_units": {unit: value}}, runner)
                self.assertFalse((self.control / unit).exists())
                self.assertNotIn(("systemctl", "start", unit), [call.args for call in run.call_args_list])
                runner.wait_online.assert_not_called()

    def test_recovery_starts_only_recorded_containers_and_runners(self):
        runner = Mock()
        runner.container.return_value = {"Image": "current-image"}
        healthy = json.dumps([{"State": {"Running": True, "Health": {"Status": "healthy"}}}])
        def commands(*args, **kwargs):
            return healthy if args[:2] == ("docker", "inspect") else ""
        with patch.object(fleet, "run", side_effect=commands) as run:
            fleet.resume({"dev": {"app": "prior-running-id"}, "runners": ["app"]}, runner)
            run.assert_any_call("docker", "start", "prior-running-id")
            run.assert_any_call("systemctl", "start", "github-actions-app.service")
            self.assertEqual(sum(call.args[:2] == ("docker", "start") for call in run.call_args_list), 1)
            runner.wait_online.assert_called_once_with("app", "current-image")

    def test_runner_creation_can_lag_behind_simple_unit_start(self):
        runner = Mock()
        runner.container.side_effect = [RuntimeError("docker inspect failed (1)"),
                                        RuntimeError("docker inspect failed (1)"),
                                        {"Image": "created-image"}]
        with patch.object(fleet, "run") as run, patch.object(fleet.time, "sleep") as sleep:
            fleet.resume({"dev": {}, "runners": ["app"]}, runner)
            run.assert_called_once_with("systemctl", "start", "github-actions-app.service")
            self.assertEqual(runner.container.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            runner.wait_online.assert_called_once_with("app", "created-image")

    def test_missing_runner_container_has_a_bounded_wait(self):
        runner = Mock()
        runner.container.side_effect = RuntimeError("docker inspect failed (1)")
        with patch.object(fleet, "run"), patch.object(fleet.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "container was not created"):
                fleet.resume({"dev": {}, "runners": ["app"]}, runner)
            self.assertEqual(runner.container.call_count, 40)
            runner.wait_online.assert_not_called()

    def test_declarative_control_link_resolves_only_to_an_existing_immutable_unit(self):
        @contextlib.contextmanager
        def idle(*args):
            yield 123
        unit = "github-actions-app.service"
        immutable = self.unit_target(unit)
        static = self.control.parent / "static/systemd/system.control" / unit
        static.parent.mkdir(parents=True)
        static.symlink_to(immutable)
        outside = self.canonical / unit
        outside.write_text("mutable unit")
        for kind, target in (("declarative", static), ("outside", outside),
                             ("dangling", self.store / "missing-unit" / unit)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                control = self.control / unit
                control.symlink_to(target)
                self.addCleanup(lambda path=control: path.unlink(missing_ok=True))
                credentials = Path(directory) / "credentials"
                credentials.mkdir()
                (credentials / "github-token").write_text("test-credential")
                runner = Mock()
                runner.idle_listener = idle
                with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": '["app"]', "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "runners", return_value=runner), patch.object(fleet, "dev_containers", return_value={}), patch.object(fleet, "run", return_value="active"), patch.object(fleet, "resume"):
                    if kind == "declarative":
                        with fleet.guard(Path(directory) / "fleet"):
                            manifest = json.loads((Path(directory) / "fleet/fleet.json").read_text())
                            self.assertEqual(manifest["runner_units"], {unit: str(immutable)})
                    else:
                        with self.assertRaisesRegex(RuntimeError, "Invalid saved runner unit target"):
                            with fleet.guard(Path(directory) / "fleet"):
                                self.fail("a mutable or dangling target must not deploy")
                        runner.stop_idle.assert_not_called()
                control.unlink()

    def test_one_recovery_failure_does_not_skip_other_services(self):
        runner = Mock()
        runner.container.return_value = {"Image": "current-image"}
        with patch.object(fleet, "run", side_effect=[RuntimeError("container failed"), ""]) as run:
            with self.assertRaisesRegex(RuntimeError, "Fleet recovery needs attention"):
                fleet.resume({"dev": {"app": "id"}, "runners": ["app"]}, runner)
            run.assert_any_call("systemctl", "start", "github-actions-app.service")

    def test_direct_core_recovery_cannot_bypass_an_existing_pause_marker(self):
        self.pause.parent.mkdir(mode=0o700)
        for marker in ("file", "dangling-symlink"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                if marker == "file":
                    self.pause.write_text("other maintenance")
                else:
                    self.pause.symlink_to(self.pause.with_name("missing"))
                manifest = {"dev": {"app": "dev-id"}, "sidecars": ["db-id"],
                            "runners": ["app"], "resumed": False}
                manifest_path = Path(directory) / "fleet.json"
                manifest_path.write_text(json.dumps(manifest))
                runner = Mock()
                with patch.object(fleet, "runners", return_value=runner), patch.object(fleet, "run") as run:
                    with self.assertRaisesRegex(RuntimeError, "pause marker is still present"):
                        core.recover_fleet(fleet, Path(directory))
                    run.assert_not_called()
                    runner.container.assert_not_called()
                    runner.wait_online.assert_not_called()
                self.assertEqual(json.loads(manifest_path.read_text()), manifest)
                self.assertTrue(self.pause.exists() or self.pause.is_symlink())
                self.pause.unlink()

    def test_only_running_compose_siblings_are_saved_and_resumed_before_dev(self):
        with patch.object(fleet, "run", side_effect=["dev-app", "dev-id\ndb-running-id"]) as run:
            sidecars = fleet.compose_sidecars({"app": "dev-id"})
            self.assertEqual(sidecars, ["db-running-id"])
            run.assert_any_call("docker", "ps", "--filter", "label=com.docker.compose.project=dev-app", "--format", "{{.ID}}")
        healthy = json.dumps([{"State": {"Running": True, "Health": {"Status": "healthy"}}}])
        with patch.object(fleet, "run", side_effect=lambda *args, **kwargs: healthy if args[:2] == ("docker", "inspect") else "") as run:
            fleet.resume({"dev": {"app": "dev-id"}, "sidecars": sidecars, "runners": []}, Mock())
            starts = [call.args for call in run.call_args_list if call.args[:2] == ("docker", "start")]
            self.assertEqual(starts, [("docker", "start", "db-running-id"), ("docker", "start", "dev-id")])

    def test_busy_runner_aborts_before_stopping_anything(self):
        @contextlib.contextmanager
        def busy(*args):
            yield None
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir()
            (credentials / "github-token").write_text("test-credential")
            runner = Mock()
            runner.idle_listener = busy
            with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": '["app"]', "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "runners", return_value=runner), patch.object(fleet, "dev_containers", return_value={}), patch.object(fleet, "run", return_value="active") as run:
                with self.assertRaisesRegex(RuntimeError, "active or unknown work"):
                    with fleet.guard(Path(directory) / "fleet"):
                        self.fail("busy fleet must not deploy")
                runner.stop_idle.assert_not_called()
                self.assertFalse(any("stop" in call.args for call in run.call_args_list))

    def test_failed_switch_still_recovers_prior_running_fleet(self):
        @contextlib.contextmanager
        def idle(*args):
            yield 123
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir()
            (credentials / "github-token").write_text("test-credential")
            runner = Mock()
            runner.idle_listener = idle
            unit = "github-actions-app.service"
            target = self.unit_target(unit)
            (self.control / unit).symlink_to(target)
            def check_snapshot(*args):
                manifest = json.loads((Path(directory) / "fleet/fleet.json").read_text())
                self.assertEqual(manifest["runner_units"], {unit: str(target)})
                self.assertTrue(self.pause.is_file())
            runner.stop_idle.side_effect = check_snapshot
            with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": '["app"]', "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "runners", return_value=runner), patch.object(fleet, "dev_containers", return_value={}), patch.object(fleet, "run", return_value="active"), patch.object(fleet, "resume") as resume:
                with self.assertRaisesRegex(RuntimeError, "switch failed"):
                    with fleet.guard(Path(directory) / "fleet"):
                        self.assertTrue(self.pause.is_file())
                        raise RuntimeError("switch failed")
                runner.stop_idle.assert_called_once_with("app", 123)
                resume.assert_called_once()
                self.assertTrue(json.loads((Path(directory) / "fleet/fleet.json").read_text())["resumed"])
                self.assertFalse(self.pause.exists())

    def test_pause_marker_is_removed_before_resume_even_when_stop_fails(self):
        @contextlib.contextmanager
        def idle(*args):
            yield 123
        for fail_stop in (False, True):
            with self.subTest(fail_stop=fail_stop), tempfile.TemporaryDirectory() as directory:
                credentials = Path(directory) / "credentials"
                credentials.mkdir()
                (credentials / "github-token").write_text("test-credential")
                runner = Mock()
                runner.idle_listener = idle
                def stop(*args):
                    self.assertTrue(self.pause.is_file())
                    self.assertEqual(self.pause.parent.stat().st_mode & 0o7777, 0o700)
                    if fail_stop:
                        raise RuntimeError("stop failed")
                runner.stop_idle.side_effect = stop
                def resume(*args):
                    self.assertFalse(self.pause.exists())
                with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": '["app"]', "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "runners", return_value=runner), patch.object(fleet, "dev_containers", return_value={}), patch.object(fleet, "run", return_value="active"), patch.object(fleet, "resume", side_effect=resume) as recovery:
                    if fail_stop:
                        with self.assertRaisesRegex(RuntimeError, "stop failed"):
                            with fleet.guard(Path(directory) / "fleet"):
                                self.fail("a failed stop must not reach activation")
                    else:
                        with fleet.guard(Path(directory) / "fleet"):
                            self.assertTrue(self.pause.is_file())
                    recovery.assert_called_once()

    def test_unknown_pause_marker_is_retained_without_stopping_or_resuming(self):
        @contextlib.contextmanager
        def idle(*args):
            yield 123
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir()
            (credentials / "github-token").write_text("test-credential")
            self.pause.parent.mkdir(mode=0o700)
            self.pause.write_text("other maintenance")
            runner = Mock()
            runner.idle_listener = idle
            with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": '["app"]', "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "runners", return_value=runner), patch.object(fleet, "dev_containers", return_value={}), patch.object(fleet, "run", return_value="active"), patch.object(fleet, "resume") as resume:
                with self.assertRaisesRegex(RuntimeError, "pause marker already exists"):
                    with fleet.guard(Path(directory) / "fleet"):
                        self.fail("a stale pause marker must block activation")
                runner.stop_idle.assert_not_called()
                resume.assert_not_called()
                self.assertEqual(self.pause.read_text(), "other maintenance")

    def test_replaced_pause_marker_is_not_deleted_or_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir()
            (credentials / "github-token").write_text("test-credential")
            with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": "[]", "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "runners", return_value=Mock()), patch.object(fleet, "dev_containers", return_value={}), patch.object(fleet, "run", return_value=""), patch.object(fleet, "resume") as resume:
                with self.assertRaisesRegex(RuntimeError, "pause marker was replaced"):
                    with fleet.guard(Path(directory) / "fleet"):
                        self.pause.rename(self.pause.with_suffix(".old"))
                        self.pause.write_text("other maintenance")
                self.assertEqual(self.pause.read_text(), "other maintenance")
                resume.assert_not_called()

    def test_symlinked_pause_directory_cannot_redirect_the_shared_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir()
            (credentials / "github-token").write_text("test-credential")
            self.pause.parent.symlink_to(Path(directory), target_is_directory=True)
            with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": "[]", "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "runners", return_value=Mock()), patch.object(fleet, "dev_containers", return_value={}), patch.object(fleet, "run", return_value=""), patch.object(fleet, "resume") as resume:
                with self.assertRaisesRegex(RuntimeError, "pause directory"):
                    with fleet.guard(Path(directory) / "fleet"):
                        self.fail("a symlinked pause directory must block activation")
                self.assertFalse((Path(directory) / "paused").exists())
                resume.assert_not_called()

    def test_dev_startup_lock_is_held_through_switch_and_released_after_recovery(self):
        @contextlib.contextmanager
        def agent(*args):
            yield
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects/app"
            project.mkdir(parents=True)
            (project / "startup.lock").touch()
            credentials = root / "credentials"
            credentials.mkdir()
            (credentials / "github-token").write_text("test-credential")
            with patch.dict(fleet.os.environ, {"RUNNER_REPOSITORIES": "[]", "CREDENTIALS_DIRECTORY": str(credentials)}), patch.object(fleet, "PROJECTS", root / "projects"), patch.object(fleet, "runners", return_value=Mock()), patch.object(fleet, "agent_lock", side_effect=agent) as agent_lock, patch.object(fleet, "dev_containers", return_value={"app": "running-id"}), patch.object(fleet, "compose_sidecars", return_value=[]), patch.object(fleet, "run", return_value="") as run, patch.object(fleet, "resume") as resume:
                with fleet.guard(root / "fleet"):
                    with (project / "startup.lock").open("a") as other:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                run.assert_any_call("docker", "stop", "--time", "30", "running-id")
                self.assertEqual(resume.call_args.args[0]["dev"], {"app": "running-id"})
                self.owner_lookup.assert_called_once_with("buildfleet")
                agent_lock.assert_called_once_with("running-id", self.owner.pw_uid, self.owner.pw_gid)
            with (project / "startup.lock").open("a") as released:
                fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
