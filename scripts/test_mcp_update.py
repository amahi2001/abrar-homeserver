"""Verify external MCP maintenance without installing packages or restarting Hermes."""

import importlib.util
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("mcp_update", Path(__file__).with_name("mcp-update.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class FakeCommands:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.home = root / "hermes"
        self.state = root / "state"
        self.state.mkdir()
        self.directories = [self.home / "warden-mcp", self.home / ".venv"]
        for directory in self.directories:
            directory.mkdir(parents=True)
            (directory / "version").write_text("old")
        self.services = {service: "active" for service in updater.SERVICES}
        self.busy_after = None
        self.unknown_after = None
        self.status_calls = 0
        self.fail = None
        self.backup_failure = False
        self.start_failure = False
        self.google_roots = updater.GOOGLE_ROOTS

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("systemctl", "show"):
            return self.services[args[-1]]
        if args[:2] == ("systemctl", "stop"):
            for service in args[2:]:
                self.services[service] = "inactive"
        elif args[:2] == ("systemctl", "start"):
            for service in args[2:]:
                self.services[service] = "active"
            if self.start_failure:
                self.start_failure = False
                raise subprocess.CalledProcessError(1, args)
        elif args[0] == "curl":
            self.status_calls += 1
            if self.unknown_after and self.status_calls >= self.unknown_after:
                raise subprocess.CalledProcessError(1, args)
            return json.dumps({"gateway_running": True, "gateway_busy": bool(self.busy_after and
                               self.status_calls >= self.busy_after), "active_agents": 0,
                               "gateway_drainable": True, "active_sessions": 1})
        elif args[0] == "cp":
            if self.backup_failure:
                raise subprocess.CalledProcessError(1, args)
            shutil.copytree(args[-2], args[-1])
        elif args[0] == "runuser":
            command = args[4:]
            if command[0] == "env":
                assert command[1] == "PATH=/test/node22/bin:/test/global-node24/bin:/test/bash/bin"
                command = command[2:]
            if command[0] == "/test/python312":
                self.assert_owner(args)
                self.modify(self.directories[1])
                if self.fail == "python":
                    raise subprocess.CalledProcessError(1, args)
            elif command[0] == "/test/node22/bin/npm":
                self.assert_owner(args)
                self.modify(self.directories[0])
                if self.fail == "npm" or self.fail == "npm-update" and "update" in command:
                    raise subprocess.CalledProcessError(1, args)
            elif "pip" in command and "list" in command:
                return json.dumps([{"name": name} for name in
                                   ("cloakbrowsermcp", "hermes-cli", "pip", *self.google_roots)])
            elif "pip" in command and "install" in command:
                self.modify(self.directories[1])
                if (self.fail == "pip" and "--upgrade-strategy" in command
                        or self.fail == "pip-self" and command[-1] == "pip"):
                    raise subprocess.CalledProcessError(1, args)
                if self.fail == "signal":
                    updater.interrupted(15, None)
            elif "check" in command and self.fail == "check":
                raise subprocess.CalledProcessError(1, args)
            elif "-c" in command and ((self.fail == "smoke" and command[-1] == updater.SMOKE)
                                      or (self.fail == "python-smoke" and command[-1] == updater.PYTHON_SMOKE)):
                raise subprocess.CalledProcessError(1, args)
        return ""

    @staticmethod
    def assert_owner(args):
        assert args[:4] == ("runuser", "-u", "hermes", "--")

    @staticmethod
    def modify(directory):
        (directory / "version").write_text("new")
        (directory / "added-by-upgrade").write_text("new")


class McpUpdateTests(unittest.TestCase):
    def test_python_smoke_uses_semantic_ssl_version_and_checks_base_interpreter(self):
        for version, same_base, accepted in (("OpenSSL 3.6.5 29 Sep 2026", True, True),
                                              ("OpenSSL 3.6.4 1 Sep 2026", True, False),
                                              ("OpenSSL 3.6.5 29 Sep 2026", False, False)):
            with self.subTest(version=version, same_base=same_base), \
                    patch.object(sys, "version_info", (3, 12, 14)), \
                    patch.object(ssl, "OPENSSL_VERSION", version), \
                    patch.object(ssl, "OPENSSL_VERSION_INFO", (3, 6, 0, 5, 0)), \
                    patch.object(updater.os.path, "samefile", return_value=same_base) as compare, \
                    patch.dict(updater.os.environ, {"UPGRADE_PYTHON": "/test/python312"}):
                if accepted:
                    exec(updater.PYTHON_SMOKE, {})
                else:
                    with self.assertRaises(AssertionError):
                        exec(updater.PYTHON_SMOKE, {})
                compare.assert_called_once_with(sys._base_executable, "/test/python312")

    def test_idle_lifecycle_contract_ignores_history_and_requires_every_field(self):
        idle = {"gateway_running": True, "gateway_busy": False, "gateway_drainable": True,
                "active_agents": 0, "active_sessions": 1}
        with patch.object(updater, "status", return_value=idle):
            updater.require_idle()
        for field in ("gateway_running", "gateway_busy", "gateway_drainable", "active_agents"):
            with self.subTest(missing=field):
                incomplete = {key: value for key, value in idle.items() if key != field}
                with patch.object(updater, "status", return_value=incomplete):
                    with self.assertRaisesRegex(RuntimeError, "busy or its idle state is unknown"):
                        updater.require_idle()

    def test_embedded_smoke_program_compiles(self):
        compile(updater.SMOKE, "mcp-smoke", "exec")

    def exercise(self, **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        commands = FakeCommands(Path(temporary.name))
        for key, value in options.items():
            setattr(commands, key, value)
        with patch.object(updater, "run", commands), patch.object(updater, "HERMES_HOME", commands.home), \
                patch.object(updater.time, "sleep"), patch.object(updater.signal, "signal"), \
                patch.dict(updater.os.environ, {"UPGRADE_PYTHON": "/test/python312", "NPM_BIN": "/test/node22/bin/npm",
                                              "PATH": "/test/global-node24/bin:/test/bash/bin"}):
            try:
                updater.refresh(commands.state)
                error = None
            except (RuntimeError, subprocess.SubprocessError) as caught:
                error = caught
        return commands, error

    def assert_restored(self, commands):
        for directory in commands.directories:
            self.assertEqual((directory / "version").read_text(), "old")
            self.assertFalse((directory / "added-by-upgrade").exists())
        self.assertTrue(all(state == "active" for state in commands.services.values()))

    def test_success_upgrades_owner_packages_and_python_before_resuming(self):
        commands, error = self.exercise()
        self.assertIsNone(error)
        actions = [call for call in commands.calls if call[0] == "runuser"]
        self.assertIn("list", actions[0])
        self.assertIn("--clear", actions[1])
        self.assertNotIn("--upgrade", actions[1])
        self.assertNotIn("--without-pip", actions[1])
        self.assertEqual(actions[2][-1], updater.PYTHON_SMOKE)
        self.assertIn("@icoretech/warden-mcp@latest", actions[3])
        for command in actions[3:5]:
            self.assertEqual(command[4:8], ("env", "PATH=/test/node22/bin:/test/global-node24/bin:/test/bash/bin",
                                           "/test/node22/bin/npm", "--engine-strict"))
        self.assertEqual(actions[4][8:10], ("update", "--prefix"))
        self.assertEqual(actions[5][-5:], ("-m", "pip", "install", "--upgrade", "pip"))
        self.assertIn("playwright", actions[6])
        self.assertIn("--upgrade-strategy", actions[6])
        self.assertEqual(actions[6][actions[6].index("--upgrade-strategy") + 1], "eager")
        self.assertTrue(set(updater.GOOGLE_ROOTS).issubset(actions[6]))
        self.assertNotIn("hermes-cli", actions[6])
        self.assertIn("check", actions[7])
        self.assertEqual(actions[8][-1], updater.SMOKE)
        for directory in commands.directories:
            self.assertEqual((directory / "version").read_text(), "new")
        self.assertEqual(list(commands.state.iterdir()), [])
        self.assertTrue(all(state == "active" for state in commands.services.values()))

    def test_google_packages_are_only_updated_when_already_installed(self):
        for roots in ((), ("google-api-python-client",), ("google-auth-oauthlib",)):
            with self.subTest(roots=roots):
                commands, error = self.exercise(google_roots=roots)
                self.assertIsNone(error)
                runtime_upgrade = next(call for call in commands.calls if "--upgrade-strategy" in call)
                self.assertEqual(set(updater.GOOGLE_ROOTS).intersection(runtime_upgrade), set(roots))

    def test_busy_or_newly_busy_gateway_never_stops_or_changes_packages(self):
        for busy_after in (1, 2):
            with self.subTest(busy_after=busy_after):
                commands, error = self.exercise(busy_after=busy_after)
                self.assertIsInstance(error, RuntimeError)
                self.assertFalse(any(call[0] in ("runuser", "cp") or call[:2] == ("systemctl", "stop")
                                     for call in commands.calls))
                self.assert_restored(commands)

    def test_upgrade_and_smoke_failures_restore_both_whole_directories(self):
        for stage in ("python", "python-smoke", "npm", "npm-update", "pip", "pip-self", "check", "smoke", "signal"):
            with self.subTest(stage=stage):
                commands, error = self.exercise(fail=stage)
                self.assertIsNotNone(error)
                self.assert_restored(commands)
                self.assertEqual(len(list(commands.state.iterdir())), 1)

    def test_backup_failure_resumes_services_without_running_managers(self):
        commands, error = self.exercise(backup_failure=True)
        self.assertIsNotNone(error)
        self.assert_restored(commands)
        self.assertFalse(any(call[0] == "runuser" for call in commands.calls))

    def test_restart_failure_also_restores_packages_and_retries_services(self):
        commands, error = self.exercise(start_failure=True)
        self.assertIsNotNone(error)
        self.assert_restored(commands)

    def test_busy_or_unknown_gateway_after_partial_resume_keeps_runtime_and_services(self):
        for state in ("busy", "unknown"):
            with self.subTest(state=state):
                commands, error = self.exercise(start_failure=True,
                                               **{state + "_after": 3})
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("rollback needs attention", str(error))
                for directory in commands.directories:
                    self.assertEqual((directory / "version").read_text(), "new")
                    self.assertTrue((directory / "added-by-upgrade").exists())
                self.assertTrue(all(value == "active" for value in commands.services.values()))
                stops = [call for call in commands.calls if call[:2] == ("systemctl", "stop")]
                self.assertEqual(stops, [("systemctl", "stop", *updater.SERVICES)])
                self.assertEqual(len(list(commands.state.iterdir())), 1)

    def test_previously_stopped_dashboard_remains_stopped(self):
        commands, error = self.exercise(services={"hermes-agent.service": "active", "hermes-dashboard.service": "inactive"})
        self.assertIsNone(error)
        self.assertEqual(commands.services["hermes-dashboard.service"], "inactive")
        stops = [call for call in commands.calls if call[:2] == ("systemctl", "stop")]
        self.assertEqual(stops, [("systemctl", "stop", "hermes-agent.service")])

    def test_transitioning_services_are_not_interrupted(self):
        commands, error = self.exercise(services={"hermes-agent.service": "activating", "hermes-dashboard.service": "active"})
        self.assertIsNotNone(error)
        self.assertFalse(any(call[0] in ("cp", "runuser") for call in commands.calls))


if __name__ == "__main__":
    unittest.main()
