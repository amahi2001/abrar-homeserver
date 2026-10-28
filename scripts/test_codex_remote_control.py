"""Remote Control checks never replace a server or disclose native responses."""

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, call, patch


spec = importlib.util.spec_from_file_location(
    "remote_control", Path(__file__).with_name("codex-remote-control.py"),
)
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)

SECRET = "fixture-private-device-and-account-data"


def response(state):
    return {"status": state, "serverName": SECRET, "installationId": SECRET, "environmentId": SECRET}


class RemoteControlTests(unittest.TestCase):
    def setUp(self):
        self.socket_path = Path("/unused/control.sock")
        self.rpc = Mock()
        self.rpc.call.return_value = response("connected")
        self.patches = [
            patch.object(remote.refresh, "Rpc", return_value=self.rpc),
            patch.object(remote.subprocess, "run"),
            patch.object(remote.time, "sleep"),
        ]
        self.connect, self.run, self.sleep = [p.start() for p in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def ensure(self, check_only=False):
        return remote.ensure_remote_control("/test/codex", self.socket_path, check_only)

    def test_existing_unmanaged_connection_needs_no_start_or_enable(self):
        self.assertEqual(self.ensure(), "connected")
        self.run.assert_not_called()
        self.rpc.call.assert_called_once_with("remoteControl/status/read", None)
        self.rpc.close.assert_called_once()
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_unmanaged_disabled_server_is_enabled_in_place(self):
        self.rpc.call.side_effect = [response("disabled"), response("connecting"), response("connected")]
        self.assertEqual(self.ensure(), "connected")
        self.run.assert_not_called()
        self.assertEqual(self.rpc.call.call_args_list, [
            call("remoteControl/status/read", None),
            call("remoteControl/enable", {"ephemeral": False}),
            call("remoteControl/status/read", None),
        ])
        self.rpc.close.assert_called_once()

    def test_connecting_server_is_polled_without_duplicate_enable(self):
        self.rpc.call.side_effect = [response("connecting"), response("connected")]
        self.assertEqual(self.ensure(), "connected")
        self.assertEqual(self.rpc.call.call_args_list, [
            call("remoteControl/status/read", None), call("remoteControl/status/read", None),
        ])
        self.run.assert_not_called()

    def test_absent_or_refused_socket_uses_native_start_and_checks_connection(self):
        for absence in (FileNotFoundError(), ConnectionRefusedError()):
            with self.subTest(absence=type(absence).__name__):
                self.connect.side_effect = [absence, self.rpc]
                self.run.reset_mock()
                self.assertEqual(self.ensure(), "connected")
                self.run.assert_called_once_with(
                    ["/test/codex", "remote-control", "start", "--json"],
                    check=True, capture_output=True, timeout=45,
                )

    def test_start_race_adopts_the_existing_ssh_server(self):
        self.connect.side_effect = [FileNotFoundError(), self.rpc]
        self.run.side_effect = subprocess.CalledProcessError(1, ["codex"], output=SECRET, stderr=SECRET)
        self.assertEqual(self.ensure(), "connected")
        self.assertEqual(self.connect.call_args_list, [call(self.socket_path), call(self.socket_path)])
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_start_failure_without_a_server_is_not_success(self):
        self.connect.side_effect = [FileNotFoundError(), ConnectionRefusedError()]
        self.run.side_effect = subprocess.CalledProcessError(1, ["codex"], output=SECRET, stderr=SECRET)
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            self.ensure()
        self.assertEqual(self.connect.call_count, 2)

    def test_timeout_can_only_adopt_a_confirmed_running_server(self):
        self.connect.side_effect = [FileNotFoundError(), self.rpc]
        self.run.side_effect = subprocess.TimeoutExpired(["codex"], 45, output=SECRET)
        self.assertEqual(self.ensure(), "connected")
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_errored_connection_can_be_reenabled_without_starting_a_process(self):
        self.rpc.call.side_effect = [response("errored"), response("connected")]
        self.assertEqual(self.ensure(), "connected")
        self.rpc.call.assert_any_call("remoteControl/enable", {"ephemeral": False})
        self.run.assert_not_called()

    def test_enable_failure_or_unknown_protocol_does_not_claim_success(self):
        for results in (
            [response("disabled"), response("errored")],
            [response("disabled"), RuntimeError(SECRET)],
            [response(SECRET)], [response([])], [None],
        ):
            with self.subTest(results=results):
                self.rpc.reset_mock()
                self.rpc.call.side_effect = results
                with self.assertRaises(RuntimeError):
                    self.ensure()
                self.rpc.close.assert_called_once()
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_connection_timeout_closes_the_rpc(self):
        self.rpc.call.return_value = response("connecting")
        with patch.object(remote.time, "monotonic", side_effect=[0, 20]):
            with self.assertRaisesRegex(RuntimeError, "connect in time"):
                self.ensure()
        self.rpc.close.assert_called_once()

    def test_unavailable_socket_does_not_start_or_replace_a_server(self):
        self.connect.side_effect = OSError(SECRET)
        with self.assertRaises(OSError):
            self.ensure()
        self.run.assert_not_called()

    def test_check_only_never_starts_or_enables(self):
        for state in ("disabled", "connecting", "connected", "errored"):
            self.rpc.reset_mock()
            self.rpc.call.return_value = response(state)
            self.assertEqual(self.ensure(check_only=True), state)
            self.rpc.call.assert_called_once_with("remoteControl/status/read", None)
        for absence in (FileNotFoundError(), ConnectionRefusedError()):
            self.connect.side_effect = absence
            self.assertEqual(self.ensure(check_only=True), "stopped")
        self.run.assert_not_called()

    def test_permission_and_protocol_failures_do_not_trigger_native_start(self):
        for error in (PermissionError(SECRET), RuntimeError(SECRET)):
            self.connect.side_effect = error
            with self.assertRaises(type(error)):
                self.ensure()
        self.run.assert_not_called()

    def test_main_discards_sensitive_exception_messages(self):
        with patch.object(remote, "ensure_remote_control", side_effect=RuntimeError(SECRET)), \
                patch("sys.argv", ["codex-remote-control.py"]):
            self.assertEqual(remote.main(), 1)
        self.assertNotIn(SECRET, self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
