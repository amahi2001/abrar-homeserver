import fcntl
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import token_eye_update as update


OLD_COMMIT, NEW_COMMIT = "a" * 40, "b" * 40
ROUTES = [{"requested": True, "configured": True, "enabled": True, "profiles": 2},
          {"requested": True, "configured": True, "enabled": True, "profiles": 2, "forwardReady": True}]


class TokenEyeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.updater = update.Updater(self.temporary.name)
        self.updater.checkout.mkdir(parents=True)
        self.updater.binary.parent.mkdir(parents=True)
        self.updater.state.mkdir()
        self.updater.binary.write_bytes(b"previous executable")
        self.previous = {"commit": OLD_COMMIT, "sha256": update.digest(self.updater.binary)}
        update.save(self.updater.receipt, self.previous)
        update.save(self.updater.runtime / "deployment.json", {"schema": 1, "kind": "token-eye-server-runtime"})
        self.candidate = Path(self.temporary.name) / "candidate"
        self.candidate.write_bytes(b"tested candidate")
        self.updater.git = Mock(side_effect=self.git)
        self.updater.box_ready = Mock()
        self.updater.service_active = Mock(return_value=True)
        self.updater.routes = Mock(return_value=ROUTES)
        self.updater.restart = Mock()
        self.updater.wait_ready = Mock()
        self.updater.build = Mock(return_value=self.candidate)
        self.ancestor = 0
        self.revision = NEW_COMMIT

    def git(self, *args, **kwargs):
        output, code = b"", 0
        if args[:2] == ("remote", "get-url"):
            output = b"https://github.com/amahi2001/token-eye.git\n"
        elif args[0] == "rev-parse":
            output = self.revision.encode()
        elif args[0] == "merge-base":
            code = self.ancestor
        return subprocess.CompletedProcess(args, code, output)

    def assert_unchanged(self):
        self.assertEqual(self.updater.binary.read_bytes(), b"previous executable")
        self.assertEqual(json.loads(self.updater.receipt.read_text()), self.previous)

    def test_main_behind_deployed_revision_waits_without_build_or_restart(self):
        self.ancestor = 1
        self.assertEqual(self.updater.update(), "waiting_for_main")
        self.updater.box_ready.assert_not_called()
        self.updater.build.assert_not_called()
        self.updater.restart.assert_not_called()
        self.assert_unchanged()

    def test_same_revision_is_noop_and_ancestry_errors_are_not_treated_as_old_main(self):
        self.revision = OLD_COMMIT
        self.assertEqual(self.updater.update(), "current")
        self.revision, self.ancestor = NEW_COMMIT, 128
        with self.assertRaisesRegex(RuntimeError, "ancestry_check_failed"):
            self.updater.update()
        self.updater.build.assert_not_called()

    def test_tested_candidate_replaces_binary_atomically_after_build_and_updates_receipt(self):
        unrelated = self.updater.checkout / "dirty.txt"
        unrelated.write_text("unfinished work")
        self.assertEqual(self.updater.update(), "updated")
        self.assertEqual(self.updater.binary.read_bytes(), self.candidate.read_bytes())
        self.assertEqual(self.updater.binary.stat().st_mode & 0o777, 0o755)
        self.assertEqual(json.loads(self.updater.receipt.read_text()),
                         {"commit": NEW_COMMIT, "sha256": update.digest(self.candidate)})
        self.assertEqual(self.updater.previous.read_bytes(), b"previous executable")
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual(unrelated.read_text(), "unfinished work")
        self.updater.restart.assert_called_once()

    def test_build_or_smoke_failure_keeps_live_binary_and_service_untouched(self):
        self.updater.build.side_effect = RuntimeError("failed fixture")
        with self.assertRaisesRegex(RuntimeError, "failed fixture"):
            self.updater.update()
        self.assert_unchanged()
        self.updater.restart.assert_not_called()
        self.assertFalse(self.updater.pending.exists())

    def test_failed_readiness_rolls_back_and_verifies_old_routes(self):
        self.updater.wait_ready.side_effect = [RuntimeError("readiness_failed"), None]
        self.assertEqual(self.updater.update(), "rolled_back")
        self.assert_unchanged()
        self.assertEqual(self.updater.restart.call_count, 2)
        self.assertEqual(self.updater.wait_ready.call_count, 2)
        self.assertFalse(self.updater.pending.exists())

    def test_interruption_after_swap_also_rolls_back(self):
        self.updater.restart.side_effect = [InterruptedError("shutdown"), None]
        self.assertEqual(self.updater.update(), "rolled_back")
        self.assert_unchanged()

    def pending(self):
        updated = {"commit": NEW_COMMIT, "sha256": update.digest(self.candidate)}
        update.atomic(self.updater.previous, self.updater.binary.read_bytes())
        update.save(self.updater.pending, {"previous": self.previous, "candidate": updated, "routes": ROUTES})
        return updated

    def test_crash_recovery_runs_before_fetch_and_handles_committed_receipt(self):
        candidate = self.pending()
        self.updater.binary.write_bytes(self.candidate.read_bytes())
        update.save(self.updater.receipt, candidate)
        self.assertEqual(self.updater.update(), "rolled_back")
        self.assert_unchanged()
        self.updater.git.assert_not_called()

    def test_recovery_preserves_unknown_binary_or_receipt_changes(self):
        self.pending()
        self.updater.binary.write_bytes(b"external change")
        with self.assertRaisesRegex(RuntimeError, "installed_binary_changed"):
            self.updater.update()
        self.assertEqual(self.updater.binary.read_bytes(), b"external change")
        self.assertTrue(self.updater.pending.exists())
        self.updater.restart.assert_not_called()

    def test_unverified_live_binary_and_marker_fail_before_fetch(self):
        self.updater.binary.write_bytes(b"external change")
        with self.assertRaisesRegex(RuntimeError, "installed_binary_changed"):
            self.updater.update()
        self.updater.git.assert_not_called()
        self.updater.binary.write_bytes(b"previous executable")
        update.save(self.updater.runtime / "deployment.json", {"schema": 2})
        with self.assertRaisesRegex(RuntimeError, "unknown_deployment"):
            self.updater.update()

    def test_busy_native_lock_defers_without_running_commands(self):
        path = Path(self.temporary.name) / "native.lock"
        with path.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(update.Deferred, "busy"):
                with update.locked(path):
                    self.fail("busy lock entered")
        link = path.with_suffix(".symlink")
        link.symlink_to(path)
        with self.assertRaises(OSError):
            with update.locked(link):
                self.fail("symlink lock entered")

    def test_intentional_routing_off_is_preserved_and_route_regressions_fail(self):
        off = [{"requested": False, "configured": False, "enabled": False, "profiles": 0},
               {"requested": False, "configured": False, "enabled": False, "profiles": 0, "forwardReady": False}]
        self.assertTrue(update.Updater.healthy(off, off))
        self.assertFalse(update.Updater.healthy(ROUTES, off))
        self.assertFalse(update.Updater.healthy(off, ROUTES))
        fewer = [{**ROUTES[0], "profiles": 1}, ROUTES[1]]
        self.assertFalse(update.Updater.healthy(fewer, ROUTES))
        self.updater.routes.return_value = off
        self.assertEqual(self.updater.update(), "updated")
        self.updater.wait_ready.assert_called_once_with(off)

    def test_route_toggle_or_inactive_service_during_build_defers_without_install(self):
        self.updater.routes.side_effect = [ROUTES, [{**ROUTES[0], "requested": False}, ROUTES[1]]]
        with self.assertRaisesRegex(update.Deferred, "deployment_changed"):
            self.updater.update()
        self.assert_unchanged()
        self.updater.restart.assert_not_called()
        self.updater.routes.side_effect = None
        self.updater.service_active.return_value = False
        with self.assertRaisesRegex(update.Deferred, "service_inactive"):
            self.updater.update()

    def test_state_symlink_and_receipt_symlink_are_refused_without_overwriting_targets(self):
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        self.updater.state.rmdir()
        self.updater.state.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "unknown_runtime_path"):
            self.updater.run()
        self.assertEqual(list(other.iterdir()), [])
        self.updater.state.unlink()
        self.updater.state.mkdir()
        original = self.updater.receipt.read_bytes()
        target = other / "receipt.json"
        target.write_bytes(original)
        self.updater.receipt.unlink()
        self.updater.receipt.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "unowned_deployment_file"):
            self.updater.update()
        self.assertEqual(target.read_bytes(), original)
        self.updater.git.assert_not_called()

    def test_status_records_fixed_phase_and_revisions_without_exception_or_cli_contents(self):
        self.updater.phase = "build_and_test"
        self.updater.installed_commit, self.updater.main_commit = OLD_COMMIT, NEW_COMMIT
        self.updater.update = Mock(side_effect=RuntimeError("SECRET ACCOUNT AND RAW CLI OUTPUT"))
        with patch.object(update, "locked", return_value=nullcontext()):
            self.assertEqual(self.updater.run(), 1)
        latest = (self.updater.state / "latest.json").read_text()
        self.assertNotIn("SECRET", latest)
        report = json.loads(latest)
        self.assertEqual(report["phase"], "build_and_test")
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["installedCommit"], OLD_COMMIT)
        self.assertEqual(report["mainCommit"], NEW_COMMIT)

    def test_readiness_deadline_stops_retrying_a_failed_service(self):
        self.updater.service_active.return_value = False
        with patch.object(update.time, "monotonic", side_effect=[0, 0, 61, 61]), patch.object(update.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "readiness_failed"):
                update.Updater.wait_ready(self.updater, ROUTES)
        self.updater.service_active.assert_called_once()
        self.updater.routes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
