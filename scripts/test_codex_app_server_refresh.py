"""Regression checks for automatic retirement of remote app servers."""

import importlib.util
from pathlib import Path
import signal
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "refresh", Path(__file__).with_name("codex-app-server-refresh.py"),
)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


def status(server="0.157.1", cli="0.160.1", running="running"):
    return {
        "status": running, "cliVersion": cli, "appServerVersion": server,
        "managedCodexPath": None,
    }


def rpc_for(state="idle", terminals=None):
    rpc = Mock()

    def call(method, params=None):
        if method == "thread/loaded/list":
            return {"data": ["thread-1"]}
        if method == "thread/read":
            return {"thread": {"status": {"type": state}}}
        if method == "thread/backgroundTerminals/list":
            return {"data": terminals or [], "nextCursor": None}
        raise AssertionError(method)

    rpc.call.side_effect = call
    return rpc


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.rpc = rpc_for()
        self.socket_path = Path("/unused/test.sock")
        self.patches = [
            patch.object(refresh, "daemon_status", return_value=status()),
            patch.object(refresh, "Rpc", return_value=self.rpc),
            patch.object(refresh, "listener_pid", return_value=1234),
            patch.object(refresh, "retire_server", return_value=True),
            patch.object(refresh, "log"),
        ]
        self.daemon, self.connect, self.listener, self.retire, self.log = [
            item.start() for item in self.patches
        ]
        for item in self.patches:
            self.addCleanup(item.stop)

    def run_refresh(self):
        refresh.refresh("/test/codex", self.socket_path)

    def test_current_or_newer_servers_are_never_restarted(self):
        for version in ("0.160.1", "0.161.0"):
            self.daemon.return_value = status(server=version)
            self.run_refresh()
        self.connect.assert_not_called()
        self.retire.assert_not_called()

    def test_disconnected_app_does_not_launch_a_server(self):
        self.daemon.return_value = status(running="stopped", server=None)
        self.run_refresh()
        self.connect.assert_not_called()

    def test_active_chat_defers_stale_server(self):
        self.rpc.call.side_effect = rpc_for("active").call.side_effect
        self.run_refresh()
        self.retire.assert_not_called()
        self.listener.assert_not_called()

    def test_background_terminal_defers_stale_server(self):
        self.rpc.call.side_effect = rpc_for(terminals=[{"processId": "42"}]).call.side_effect
        self.run_refresh()
        self.retire.assert_not_called()

    def test_unknown_work_state_defers_stale_server(self):
        self.rpc.call.side_effect = rpc_for("systemError").call.side_effect
        self.run_refresh()
        self.retire.assert_not_called()

    def test_unreadable_state_leaves_server_running(self):
        self.rpc.call.side_effect = RuntimeError("unsupported method")
        with self.assertRaises(RuntimeError):
            self.run_refresh()
        self.retire.assert_not_called()
        self.rpc.close.assert_called_once()

    def test_check_mode_never_restarts(self):
        refresh.refresh("/test/codex", self.socket_path, check_only=True)
        self.retire.assert_not_called()
        self.listener.assert_not_called()

    def test_idle_stale_server_is_retired_and_reconnection_verified(self):
        self.daemon.side_effect = [status(), status(), status(server="0.160.1")]
        self.run_refresh()
        self.retire.assert_called_once_with(self.rpc, self.socket_path)
        self.rpc.close.assert_called_once()

    def test_server_updated_between_checks_is_preserved(self):
        self.daemon.side_effect = [status(), status(server="0.160.1")]
        self.run_refresh()
        self.retire.assert_not_called()

    def test_unknown_version_leaves_server_running(self):
        self.daemon.return_value = status(server="unknown")
        with self.assertRaises(RuntimeError):
            self.run_refresh()
        self.connect.assert_not_called()

    def test_managed_server_uses_the_supported_package_update(self):
        managed = status()
        managed["managedCodexPath"] = "/managed/codex"
        self.daemon.side_effect = [managed, managed, status(server="0.160.1")]
        with patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "resolve", return_value=Path("/managed/codex")), \
                patch.object(refresh.subprocess, "run") as run:
            self.run_refresh()
        self.assertEqual(run.call_args.args[0], [
            "/test/codex", "app-server", "daemon", "update", "--from-cli", "--yes",
        ])
        self.retire.assert_not_called()

    def test_managed_server_defers_if_work_starts_before_update(self):
        managed = status()
        managed["managedCodexPath"] = "/managed/codex"
        self.daemon.return_value = managed
        with patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "resolve", return_value=Path("/managed/codex")), \
                patch.object(refresh, "server_idle", side_effect=[True, False]), \
                patch.object(refresh.subprocess, "run") as run:
            self.run_refresh()
        run.assert_not_called()
        self.retire.assert_not_called()


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.rpc = rpc_for()
        self.socket_path = Path("/unused/test.sock")
        self.patches = [
            patch.object(refresh, "listener_pid", return_value=1234),
            patch.object(refresh.os, "pidfd_open", return_value=99),
            patch.object(refresh.os, "close"),
            patch.object(refresh.signal, "pidfd_send_signal"),
            patch.object(refresh, "exited"),
            patch.object(refresh, "server_idle", return_value=True),
        ]
        self.listener, self.open, self.close, self.send, self.exited, self.idle = [
            item.start() for item in self.patches
        ]
        for item in self.patches:
            self.addCleanup(item.stop)

    def test_term_stops_only_the_identified_process(self):
        self.exited.side_effect = [False, True]
        self.assertTrue(refresh.retire_server(self.rpc, self.socket_path))
        self.send.assert_called_once_with(99, signal.SIGTERM)
        self.close.assert_called_once_with(99)

    def test_ignored_term_requires_another_idle_check_before_kill(self):
        self.exited.side_effect = [False, False, True]
        self.assertTrue(refresh.retire_server(self.rpc, self.socket_path))
        self.assertEqual(self.idle.call_count, 2)
        self.assertEqual([c.args for c in self.send.call_args_list], [
            (99, signal.SIGTERM), (99, signal.SIGKILL),
        ])

    def test_new_work_after_term_prevents_force_kill(self):
        self.exited.side_effect = [False, False]
        self.idle.side_effect = [True, False]
        self.assertFalse(refresh.retire_server(self.rpc, self.socket_path))
        self.send.assert_called_once_with(99, signal.SIGTERM)

    def test_replaced_listener_is_never_signalled(self):
        self.exited.return_value = False
        self.listener.side_effect = [1234, 5678]
        self.assertFalse(refresh.retire_server(self.rpc, self.socket_path))
        self.send.assert_not_called()


class ListenerTests(unittest.TestCase):
    def test_unrelated_unix_listener_is_not_a_codex_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listener.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.bind(str(path))
                sock.listen()
                with self.assertRaises(RuntimeError):
                    refresh.listener_pid(path)


if __name__ == "__main__":
    unittest.main()
