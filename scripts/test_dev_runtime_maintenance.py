import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import dev_runtime_maintenance as maintenance


class DevMaintenanceTests(unittest.TestCase):
    def test_only_named_managed_running_projects_are_accepted(self):
        with patch.object(maintenance, "run", return_value="abc123\tapp\ndef456\tother"):
            self.assertEqual(maintenance.running_projects(), {"app": "abc123", "other": "def456"})
        for rows in ["abc\t../escape", "abc\tapp\ndef\tapp"]:
            with patch.object(maintenance, "run", return_value=rows):
                with self.assertRaises(RuntimeError):
                    maintenance.running_projects()

    def test_weekly_candidate_is_reused_without_reinstalling_or_building(self):
        candidate = {"dev": "/store/dev", "built_at": time.time(), "image": "base", "id": "sha256:cached"}
        with patch.object(maintenance, "run", return_value="sha256:cached") as run:
            self.assertEqual(maintenance.candidate("/store/dev", {"candidate": candidate}), candidate)
            run.assert_called_once_with("docker", "image", "inspect", "--format", "{{.Id}}", "base")

    def test_weekly_build_refreshes_bundled_node_and_smokes_before_promotion(self):
        image = "buildfleet-dev:" + "a" * 16
        with patch.object(maintenance, "run", side_effect=["", f"build output\n{image}", "sha256:new"]) as run, patch.object(maintenance, "smoke") as smoke:
            candidate = maintenance.candidate("/store/dev", {})
            run.assert_any_call("/store/dev", "setup", "--refresh-runtime", timeout=600)
            run.assert_any_call("/store/dev", "build", timeout=3600)
            smoke.assert_called_once_with(image)
            self.assertEqual(candidate["id"], "sha256:new")

    def test_busy_or_stopped_projects_are_left_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            for running, idle in [({}, True), ({"app": "old"}, False)]:
                with patch.object(maintenance, "running_projects", return_value=running), patch.object(maintenance, "idle", return_value=idle), patch.object(maintenance, "run") as run:
                    self.assertIsNone(maintenance.refresh("dev", "app", "old", Path(directory), {}))
                    run.assert_not_called()

    def test_existing_startup_lock_defers_without_touching_the_box(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / "startup.lock").open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with patch.object(maintenance, "run") as run:
                    self.assertIsNone(maintenance.refresh("dev", "app", "old", Path(directory), {}))
                    run.assert_not_called()

    def test_failed_or_interrupted_rebuild_restores_image_and_configuration_without_source_or_volume_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            source = state / "source"
            source.mkdir()
            (source / "work.txt").write_text("uncommitted work")
            home = state / "home"
            home.mkdir()
            agent_lock = home / ".agent-session.lock"
            metadata = {"composeProject": "dev-app", "image": "previous-tag", "source": str(source)}
            compose = {"services": {"dev": {"volumes": [{"type": "bind", "source": str(home), "target": "/home/node"}]}}}
            originals = {"project.json": json.dumps(metadata), "compose.json": json.dumps(compose), "devcontainer.json": "original native config"}
            for name, data in originals.items():
                (state / name).write_text(data)

            failure = RuntimeError("unhealthy replacement")
            def commands(*args, **kwargs):
                if args[:2] == ("dev", "rebuild"):
                    self.assertEqual(kwargs["env"]["DEV_HOST_LOCK_HELD"], "app")
                    self.assertEqual(subprocess.run(["flock", "-n", str(agent_lock), "true"]).returncode, 0)
                    for name in originals:
                        (state / name).write_text("failed replacement")
                    raise failure
                if args[:4] == ("docker", "image", "tag", "sha256:previous") and args[-1] == "previous-tag" or args[:2] == ("docker", "compose"):
                    self.assertNotEqual(subprocess.run(["flock", "-n", str(agent_lock), "true"]).returncode, 0)
                return ""

            prior = {"Image": "sha256:previous", "Config": {"Labels": {"com.docker.compose.project": "dev-app"}},
                     "Mounts": [{"Type": "bind", "Source": str(home), "Destination": "/home/node"}]}
            with patch.object(maintenance, "running_projects", side_effect=[{"app": "old"}, {"app": "new"}]), patch.object(maintenance, "idle", return_value=True), patch.object(maintenance, "inspect", return_value=prior), patch.object(maintenance, "run", side_effect=commands) as run:
                with self.assertRaisesRegex(RuntimeError, "previous image and project configuration restored"):
                    maintenance.refresh("dev", "app", "old", state, {"id": "sha256:new"})
                run.assert_any_call("docker", "image", "tag", "sha256:previous", "previous-tag")
                self.assertTrue(any(call.args[:2] == ("docker", "compose") and "--no-build" in call.args and "--no-deps" in call.args for call in run.call_args_list))
                self.assertFalse(any("rm" in call.args or "down" in call.args for call in run.call_args_list))
            for name, data in originals.items():
                self.assertEqual((state / name).read_text(), data)
            self.assertEqual(subprocess.run(["flock", "-n", str(agent_lock), "true"]).returncode, 0)
            self.assertEqual((source / "work.txt").read_text(), "uncommitted work")
            failure = KeyboardInterrupt("interrupted")
            with patch.object(maintenance, "running_projects", side_effect=[{"app": "old"}, {"app": "new"}]), patch.object(maintenance, "idle", return_value=True), patch.object(maintenance, "inspect", return_value=prior), patch.object(maintenance, "run", side_effect=commands) as run:
                with self.assertRaisesRegex(KeyboardInterrupt, "interrupted"):
                    maintenance.refresh("dev", "app", "old", state, {"id": "sha256:new"})
                run.assert_any_call("docker", "image", "tag", "sha256:previous", "previous-tag")
            for name, data in originals.items():
                self.assertEqual((state / name).read_text(), data)

    def test_rollback_admission_gate_preserves_new_work_and_unknown_mounts(self):
        for reason in ("busy", "unknown-mount"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                home = state / "home"
                home.mkdir()
                metadata = {"composeProject": "dev-app", "image": "previous-tag", "source": str(state / "source")}
                compose = {"services": {"dev": {"volumes": [{"type": "bind", "source": str(home), "target": "/home/node"}]}}}
                originals = {"project.json": json.dumps(metadata), "compose.json": json.dumps(compose), "devcontainer.json": "original"}
                for name, data in originals.items():
                    (state / name).write_text(data)
                prior = {"Image": "old", "Config": {"Labels": {"com.docker.compose.project": "dev-app"}},
                         "Mounts": [{"Type": "bind", "Source": str(home), "Destination": "/home/node"}]}
                current = {**prior, "Image": "new"}
                if reason == "unknown-mount":
                    current["Mounts"] = [{"Type": "bind", "Source": "/unknown/home", "Destination": "/home/node"}]
                with (home / ".agent-session.lock").open("a") as admitted:
                    def commands(*args, **kwargs):
                        if args[:2] == ("dev", "rebuild"):
                            for name in originals:
                                (state / name).write_text("new state and active work")
                            if reason == "busy":
                                fcntl.flock(admitted, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            raise RuntimeError("partial resume failed")
                        return ""
                    with patch.object(maintenance, "running_projects", side_effect=[{"app": "old"}, {"app": "new"}]), patch.object(maintenance, "idle", return_value=True), patch.object(maintenance, "inspect", side_effect=[prior, current]), patch.object(maintenance, "run", side_effect=commands) as run:
                        with self.assertRaisesRegex(RuntimeError, "Rollback needs attention"):
                            maintenance.refresh("dev", "app", "old", state, {})
                        self.assertFalse(any(call.args[:2] == ("docker", "compose") for call in run.call_args_list))
                        self.assertFalse(any(call.args[-1:] == ("previous-tag",) for call in run.call_args_list))
                    for name in originals:
                        self.assertEqual((state / name).read_text(), "new state and active work")

    def test_rollback_dispatch_lock_releases_after_recovery_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            home = state / "home"
            home.mkdir()
            snapshots = {"compose.json": json.dumps({"services": {"dev": {"volumes": [{"type": "bind", "source": str(home), "target": "/home/node"}]}}}).encode()}
            runtime = {"Mounts": [{"Type": "bind", "Source": str(home), "Destination": "/home/node"}]}
            path = home / ".agent-session.lock"
            with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                with maintenance.rollback_dispatch(state, snapshots, runtime, runtime):
                    self.assertNotEqual(subprocess.run(["flock", "-n", str(path), "true"]).returncode, 0)
                    raise RuntimeError("recovery failed")
            self.assertEqual(subprocess.run(["flock", "-n", str(path), "true"]).returncode, 0)
            path.unlink()
            path.symlink_to(home / "missing")
            with self.assertRaises(OSError):
                with maintenance.rollback_dispatch(state, snapshots, runtime, runtime):
                    self.fail("Symlinked lock must not admit rollback")


if __name__ == "__main__":
    unittest.main()
