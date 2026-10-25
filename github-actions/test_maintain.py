import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import maintain


class MaintenanceTests(unittest.TestCase):
    def test_only_stable_release_is_accepted(self):
        self.assertEqual(maintain.stable_version({"tag_name": "v2.338.1", "draft": False, "prerelease": False}), "2.338.1")
        for tag, draft, prerelease in [("v2.339.0-rc1", False, False), ("v2.339.0", True, False), ("v2.339.0", False, True), ("../../bad", False, False)]:
            with self.assertRaises(RuntimeError):
                maintain.stable_version({"tag_name": tag, "draft": draft, "prerelease": prerelease})

    def test_missing_duplicate_or_truncated_registration_is_rejected(self):
        runner = {"name": "buildfleet-server-test", "busy": False, "status": "online"}
        for runners, count in [([], 0), ([runner, runner], 2), ([runner], 101)]:
            with patch.object(maintain, "api", return_value={"runners": runners, "total_count": count}):
                with self.assertRaises(RuntimeError):
                    maintain.registered("test")

    def test_busy_runner_never_receives_a_signal(self):
        with patch.object(maintain, "registered", return_value={"busy": True, "status": "online"}), patch.object(maintain.signal, "pidfd_send_signal") as send:
            with maintain.idle_listener("test") as descriptor:
                self.assertIsNone(descriptor)
            send.assert_not_called()

    def test_existing_worker_is_not_interrupted(self):
        with patch.object(maintain, "registered", return_value={"busy": False, "status": "online"}), patch.object(maintain, "processes", return_value=[(123, "Runner.Listener"), (124, "Runner.Worker")]), patch.object(maintain.signal, "pidfd_send_signal") as send:
            with maintain.idle_listener("test") as descriptor:
                self.assertIsNone(descriptor)
            send.assert_not_called()

    def frozen_process(self, second_state, raises=False):
        # Exercise real pidfd STOP/CONT so a failed check cannot strand dispatch.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        table = [(child.pid, "Runner.Listener")]
        try:
            with patch.object(maintain, "registered", side_effect=[{"busy": False, "status": "online"}, second_state]), patch.object(maintain, "processes", return_value=table), patch.object(maintain.os, "readlink", return_value="/home/runner/bin/Runner.Listener"):
                if raises:
                    with self.assertRaisesRegex(RuntimeError, "check failed"):
                        with maintain.idle_listener("test"):
                            raise RuntimeError("check failed")
                else:
                    with maintain.idle_listener("test") as descriptor:
                        self.assertIsNone(descriptor)
            for _ in range(100):
                state = Path(f"/proc/{child.pid}/status").read_text().split("State:", 1)[1].split()[0]
                if state not in {"T", "t"}:
                    break
                time.sleep(.01)
            self.assertNotIn(state, {"T", "t"})
        finally:
            os.kill(child.pid, signal.SIGCONT)
            child.terminate()
            child.wait(timeout=5)

    def test_job_assigned_during_check_defers_and_resumes_listener(self):
        self.frozen_process({"busy": True, "status": "online"})

    def test_failed_update_check_resumes_listener(self):
        self.frozen_process({"busy": False, "status": "online"}, raises=True)

    def test_failed_replacement_restores_old_image_and_registration(self):
        @maintain.contextlib.contextmanager
        def idle(*args, **kwargs):
            yield 99
        with patch.object(maintain, "container", side_effect=[{"Image": "old"}, {"Image": "new", "State": {"Running": True}}]), patch.object(maintain, "idle_listener", idle), patch.object(maintain, "stop_idle") as stop, patch.object(maintain, "run") as run, patch.object(maintain, "wait_online", side_effect=[RuntimeError("offline"), None]) as health:
            with self.assertRaisesRegex(RuntimeError, "previous image restored online"):
                maintain.replace("test", "prefix", "new")
            self.assertEqual(stop.call_count, 2)
            run.assert_any_call("docker", "image", "tag", "old", "prefix-test")
            health.assert_any_call("test", "old")

    def test_no_repeat_replacement_when_container_is_current(self):
        with patch.object(maintain, "container", return_value={"Image": "new"}), patch.object(maintain, "registered", return_value={"status": "online"}), patch.object(maintain, "idle_listener") as idle, patch.object(maintain, "run") as run:
            self.assertTrue(maintain.replace("test", "prefix", "new"))
            idle.assert_not_called()
            run.assert_called_once_with("docker", "image", "tag", "new", "prefix-test")

    def test_current_image_does_not_repair_startup_tag_until_online(self):
        with patch.object(maintain, "container", return_value={"Image": "new"}), patch.object(maintain, "registered", return_value={"status": "offline"}), patch.object(maintain, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "not registered online"):
                maintain.replace("test", "prefix", "new")
            run.assert_not_called()

    def test_interrupt_after_replacement_restores_old_image_before_exiting(self):
        @maintain.contextlib.contextmanager
        def idle(*args, **kwargs):
            yield 99
        with patch.object(maintain, "container", side_effect=[{"Image": "old"}, {"Image": "new", "State": {"Running": True}}]), patch.object(maintain, "idle_listener", idle), patch.object(maintain, "stop_idle") as stop, patch.object(maintain, "run") as run, patch.object(maintain, "wait_online", side_effect=[KeyboardInterrupt(), None]) as health:
            with self.assertRaises(KeyboardInterrupt):
                maintain.replace("test", "prefix", "new")
            self.assertEqual(stop.call_count, 2)
            run.assert_any_call("docker", "image", "tag", "old", "prefix-test")
            health.assert_any_call("test", "old")

    def test_rollback_does_not_interrupt_a_newly_accepted_job(self):
        @maintain.contextlib.contextmanager
        def idle(*args, **kwargs):
            yield None if kwargs.get("allow_offline") else 99
        with patch.object(maintain, "container", side_effect=[{"Image": "old"}, {"Image": "new", "State": {"Running": True}}]), patch.object(maintain, "idle_listener", idle), patch.object(maintain, "stop_idle") as stop, patch.object(maintain, "run") as run, patch.object(maintain, "wait_online", side_effect=RuntimeError("API unavailable")):
            with self.assertRaisesRegex(RuntimeError, "active/unknown job prevents restart"):
                maintain.replace("test", "prefix", "new")
            self.assertEqual(stop.call_count, 1)
            run.assert_any_call("docker", "image", "tag", "old", "prefix-test")

    def test_retry_after_deferred_rollback_repairs_startup_tag_without_interrupting_job(self):
        @maintain.contextlib.contextmanager
        def idle(*args, **kwargs):
            yield None if kwargs.get("allow_offline") else 99
        running = {"Image": "old", "State": {"Running": True}}
        tags = {"prefix-test": "old"}
        def commands(*args, **kwargs):
            if args[:3] == ("docker", "image", "tag"):
                tags[args[4]] = args[3]
            elif args == ("systemctl", "start", "github-actions-test.service"):
                running["Image"] = tags["prefix-test"]
            return ""
        with patch.object(maintain, "container", side_effect=lambda repository: running), patch.object(maintain, "idle_listener", idle), patch.object(maintain, "stop_idle") as stop, patch.object(maintain, "run", side_effect=commands) as run, patch.object(maintain, "wait_online", side_effect=RuntimeError("API unavailable")), patch.object(maintain, "registered", return_value={"status": "online", "busy": True}), patch.object(maintain.signal, "pidfd_send_signal") as send:
            with self.assertRaisesRegex(RuntimeError, "active/unknown job prevents restart"):
                maintain.replace("test", "prefix", "new")
            self.assertEqual(running["Image"], "new")
            self.assertEqual(tags["prefix-test"], "old")
            run.reset_mock()
            self.assertTrue(maintain.replace("test", "prefix", "new"))
            self.assertEqual(tags["prefix-test"], "new")
            self.assertEqual(running["Image"], "new")
            run.assert_called_once_with("docker", "image", "tag", "new", "prefix-test")
            self.assertEqual(stop.call_count, 1)
            send.assert_not_called()

    def test_candidate_is_cached_for_a_week_but_package_refresh_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "candidate.json"
            state.write_text(json.dumps({"version": "2.338.1", "source": "prefix", "image": "sha256:cached", "built_at": time.time()}))
            with patch.object(maintain, "run") as run:
                self.assertEqual(maintain.candidate_image(Path(directory), "prefix", state, "2.338.1"), "sha256:cached")
                run.assert_called_once_with("docker", "image", "inspect", "sha256:cached")
            old = json.loads(state.read_text())
            old["built_at"] -= 8 * 86400
            state.write_text(json.dumps(old))
            digest = "ghcr.io/actions/actions-runner@sha256:" + "a" * 64
            with patch.object(maintain, "run", side_effect=["", json.dumps([digest]), "", "", "sha256:new"]) as run:
                self.assertEqual(maintain.candidate_image(Path(directory), "prefix", state, "2.338.1"), "sha256:new")
                build = run.call_args_list[2].args
                self.assertIn("--no-cache", build)
                self.assertIn(f"RUNNER_BASE={digest}", build)


if __name__ == "__main__":
    unittest.main()
