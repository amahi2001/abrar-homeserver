"""Regression checks for isolated, transactional host maintenance."""

import importlib.util
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("system_update", Path(__file__).with_name("system-update.py"))
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)

FLAKE = '''{
  inputs = {
    hermes-agent.url = "tarball+https://codeload.github.com/NousResearch/hermes-agent/tar.gz/refs/tags/v2026.9.24";
    caveman = { url = "github:JuliusBrussee/caveman/v1.9.1"; };
  };
}
'''


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "deployed"
        self.canonical = self.root / "checkout"
        for directory in (self.source, self.canonical):
            directory.mkdir()
            (directory / "flake.nix").write_text(FLAKE)
            (directory / "flake.lock").write_text("old lock\n")
            (directory / "development").mkdir()
            (directory / "development/Dockerfile").write_text("FROM node:24.22.1-bookworm-slim@sha256:" + "a" * 64 + "\nARG BUN_VERSION=1.4.4\nARG CODEX_VERSION=0.163.0\nARG CLAUDE_VERSION=2.1.306\nARG UV_VERSION=0.13.4\n")
        self.previous = self.root / "previous-system"
        self.previous.mkdir()
        self.target = self.root / "target-system"
        self.target.mkdir()
        self.hook = "/test/service-state"
        self.calls = []
        self.health = []
        self.failure = None
        self.change_pins = True
        self.running_package = "/nix/store/old-hermes"
        self.expected_package = self.running_package
        self.evaluated_package = None
        self.app_change = True
        self.docker_change = False
        self.real_docker_changed = update.docker_units_changed
        self.patches = [
            patch.object(update, "running_hermes_package", side_effect=lambda run: self.running_package),
            patch.object(update, "application_units_changed", side_effect=lambda *args: self.app_change),
            patch.object(update, "docker_units_changed", side_effect=lambda *args: self.docker_change),
            patch.object(update, "mark_reboot_required"),
            patch.object(update, "gateway_package_from_system", side_effect=lambda system: self.expected_package),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_command(self, args):
        self.calls.append(args)
        if self.failure and self.failure(args):
            raise RuntimeError("injected command failure")
        if args[:3] == ["nix", "flake", "update"] and self.change_pins:
            candidate = Path(args[-1].removeprefix("path:"))
            (candidate / "flake.lock").write_text("new lock\n")
        if args[:2] == ["nix", "eval"]:
            return json.dumps({"path": self.evaluated_package or self.expected_package, "version": "0.22.2"})
        if args[:2] == ["nix", "build"]:
            Path(args[args.index("--out-link") + 1]).symlink_to(self.target)
        if args[0] == "curl":
            if args[-1] == "https://nodejs.org/dist/index.json":
                return json.dumps([{"version": "v26.12.1"}, {"version": "v24.22.2"}, {"version": "v24.22.1"}])
            if args[-1] == "https://registry.npmjs.org/@openai/codex/latest":
                return json.dumps({"version": "0.164.0"})
            if args[-1] == "https://registry.npmjs.org/@anthropic-ai/claude-code/latest":
                return json.dumps({"version": "2.1.310"})
            if args[-1] == "https://pypi.org/pypi/uv/json":
                return json.dumps({"info": {"version": "0.13.5"}})
            if "releases/latest" in args[-1]:
                tag = "bun-v1.4.5" if "oven-sh/bun" in args[-1] else "v1.9.2" if "caveman" in args[-1] else "v0.22.2"
                return json.dumps({"tag_name": tag, "prerelease": False, "draft": False})
            return json.dumps({"gateway_running": True, "gateway_busy": False,
                               "gateway_drainable": True, "active_agents": 0, "active_sessions": 1})
        if args[:3] == ["systemctl", "restart", "hermes-agent.service"]:
            self.running_package = self.expected_package
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps(["node@sha256:" + "b" * 64])
        return ""

    def verify(self, run, version=None):
        self.health.append(version)

    def perform(self, task="codex", **kwargs):
        return update.update(task, source=self.source, state_dir=self.root / "state",
                             canonical=self.canonical, backup_hook=self.hook,
                             current_system=self.previous, run=self.run_command,
                             deployment_lock=self.root / "deployment.lock",
                             verify=kwargs.pop("verify", self.verify), **kwargs)

    def switched(self):
        return any(args[:2] == ["nixos-rebuild", "switch"] for args in self.calls)

    def hook_actions(self):
        return [args[1] for args in self.calls if args[0] == self.hook]

    def test_failed_build_never_backs_up_or_switches(self):
        self.failure = lambda args: args[:2] == ["nix", "build"]
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.perform()
        self.assertFalse(self.switched())
        self.assertEqual(self.hook_actions(), [])
        self.assertEqual((self.source / "flake.lock").read_text(), "old lock\n")
        self.assertEqual((self.canonical / "flake.lock").read_text(), "old lock\n")

    def test_deployment_lock_covers_mutation_and_rollback_but_not_builds(self):
        observed = []
        original = self.run_command

        def held():
            with (self.root / "deployment.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                return False

        def run(args):
            if args[:2] == ["nix", "build"] or args[:3] == ["nix", "flake", "check"]:
                self.assertFalse(held())
                observed.append("build")
            if args[0] in (self.hook, "nixos-rebuild", "nix-env", str(self.previous / "bin/switch-to-configuration")):
                self.assertTrue(held())
                observed.append(args[0])
            return original(args)

        def verify(run, version=None):
            self.assertTrue(held())
            if version is not None:
                raise RuntimeError("injected unhealthy candidate")

        self.run_command = run
        with self.assertRaisesRegex(RuntimeError, "prior system/state were restored"):
            self.perform(verify=verify)
        self.assertIn("build", observed)
        self.assertIn("nix-env", observed)
        self.assertFalse(held())

    def test_dirty_checkout_is_never_deployed_or_overwritten(self):
        (self.canonical / "flake.nix").write_text("administrator work\n")
        (self.canonical / "flake.lock").write_text("administrator lock\n")
        directory = self.perform("all")
        switches = [args for args in self.calls if args[:2] == ["nixos-rebuild", "switch"]]
        self.assertIn(f"path:{directory / 'candidate'}", switches[0])
        self.assertEqual((self.canonical / "flake.nix").read_text(), "administrator work\n")
        self.assertEqual((self.canonical / "flake.lock").read_text(), "administrator lock\n")
        self.assertIn("administrator edits", (directory / "update.log").read_text())

    def test_all_refreshes_exact_runtime_pins_and_syncs_after_checks(self):
        directory = self.perform("all")
        dockerfile = (directory / "candidate/development/Dockerfile").read_text()
        self.assertIn("FROM node:24.22.2-bookworm-slim@sha256:" + "b" * 64, dockerfile)
        self.assertIn("ARG BUN_VERSION=1.4.5", dockerfile)
        self.assertIn("ARG CODEX_VERSION=0.164.0", dockerfile)
        self.assertIn("ARG CLAUDE_VERSION=2.1.310", dockerfile)
        self.assertIn("ARG UV_VERSION=0.13.5", dockerfile)
        self.assertIn("github:JuliusBrussee/caveman/v1.9.2", (directory / "candidate/flake.nix").read_text())
        self.assertEqual((self.canonical / "development/Dockerfile").read_text(), dockerfile)

    def test_invalid_tool_release_never_reaches_deployment(self):
        original = self.run_command
        def run(args):
            if args[0] == "curl" and "@openai/codex/latest" in args[-1]:
                return json.dumps({"version": "0.165.0-alpha.1"})
            return original(args)
        with patch.object(self, "run_command", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "Unexpected stable development tool"):
                self.perform("all")
        self.assertFalse(self.switched())

    def test_development_tool_pins_never_downgrade(self):
        dockerfile = self.source / "development/Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace("ARG UV_VERSION=0.13.4", "ARG UV_VERSION=0.14.0"))
        directory = self.perform("all")
        self.assertIn("ARG UV_VERSION=0.14.0", (directory / "candidate/development/Dockerfile").read_text())

    def test_invalid_bun_release_never_reaches_deployment(self):
        original = self.run_command
        def run(args):
            if args[0] == "curl" and "oven-sh/bun" in args[-1]:
                return json.dumps({"tag_name": "bun-v1.4.5-canary", "prerelease": True})
            return original(args)
        with patch.object(self, "run_command", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "Unexpected stable Bun"):
                self.perform("all")
        self.assertFalse(self.switched())

    def test_switch_failure_restores_state_before_activating_prior_generation(self):
        self.failure = lambda args: args[:2] == ["nixos-rebuild", "switch"]
        with self.assertRaisesRegex(RuntimeError, "prior system/state were restored"):
            self.perform()
        self.assertEqual(self.hook_actions(), ["backup", "restore", "resume"])
        profile = next(i for i, args in enumerate(self.calls) if args[0] == "nix-env")
        activation = next(i for i, args in enumerate(self.calls) if args[0] == str(self.previous / "bin/switch-to-configuration"))
        restore = next(i for i, args in enumerate(self.calls) if args[:2] == [self.hook, "restore"])
        self.assertLess(profile, activation)
        self.assertLess(restore, profile)
        self.assertEqual((self.canonical / "flake.lock").read_text(), "old lock\n")

    def test_semver_stable_hermes_tag_is_accepted(self):
        directory = self.perform("hermes")
        self.assertIn("github:NousResearch/hermes-agent/v0.22.2", (directory / "candidate/flake.nix").read_text())
        self.assertEqual(self.hook_actions(), ["backup", "resume"])

    def test_failed_health_restores_prior_generation_and_state(self):
        def verify(run, version=None):
            self.health.append(version)
            if version is not None:
                raise RuntimeError("new services are unhealthy")
        with self.assertRaisesRegex(RuntimeError, "prior system/state were restored"):
            self.perform(verify=verify)
        self.assertEqual(self.health, ["0.22.2", None])
        self.assertEqual(self.hook_actions(), ["backup", "resume", "restore", "resume"])
        self.assertEqual(json.loads((self.root / "state/latest.json").read_text())["status"], "rolled-back")

    def test_failed_old_activation_still_restores_data_and_resumes(self):
        self.failure = lambda args: (args[:2] == ["nixos-rebuild", "switch"]
                                     or args[0] == str(self.previous / "bin/switch-to-configuration"))
        with self.assertRaisesRegex(RuntimeError, "rollback needs attention"):
            self.perform()
        self.assertEqual(self.hook_actions(), ["backup", "restore", "resume"])
        self.assertEqual(json.loads((self.root / "state/latest.json").read_text())["status"], "rollback-failed")

    def test_new_hermes_work_blocks_state_and_generation_rollback(self):
        def unhealthy(run, version=None):
            if version:
                raise RuntimeError("candidate health failed")
        with patch.object(update, "require_hermes_idle",
                          side_effect=[None, RuntimeError("new conversation is active")]):
            with self.assertRaisesRegex(RuntimeError, "rollback needs attention"):
                self.perform(verify=unhealthy)
        self.assertEqual(self.hook_actions(), ["backup", "resume"])
        self.assertFalse(any(args[0] == "nix-env" for args in self.calls))
        result = json.loads((self.root / "state/latest.json").read_text())
        self.assertEqual(result["status"], "rollback-failed")
        self.assertIn("work was left intact", result["rollback_errors"][0])

    def test_active_hermes_with_unreachable_status_blocks_rollback(self):
        idle = {"gateway_running": True, "gateway_busy": False,
                "gateway_drainable": True, "active_agents": 0, "active_sessions": 1}
        def unhealthy(run, version=None):
            if version:
                raise RuntimeError("candidate health failed")
        with patch.object(update, "hermes_status",
                          side_effect=[idle, RuntimeError("status endpoint unavailable")]):
            with self.assertRaisesRegex(RuntimeError, "rollback needs attention"):
                self.perform(verify=unhealthy)
        self.assertEqual(self.hook_actions(), ["backup", "resume"])
        self.assertFalse(any(args[0] == "nix-env" for args in self.calls))

    def test_failed_state_restore_does_not_activate_old_generation(self):
        self.failure = lambda args: args[0] == "nixos-rebuild" or args[:2] == [self.hook, "restore"]
        with self.assertRaisesRegex(RuntimeError, "rollback needs attention"):
            self.perform()
        self.assertEqual(self.hook_actions(), ["backup", "restore"])
        self.assertFalse(any(args[0] == "nix-env" for args in self.calls))

    def test_backup_failure_resumes_without_deployment(self):
        self.failure = lambda args: args[:2] == [self.hook, "backup"]
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.perform()
        self.assertFalse(self.switched())
        self.assertEqual(self.hook_actions(), ["backup", "resume"])

    def test_identical_pins_verify_without_switching(self):
        self.change_pins = False
        self.perform()
        self.assertFalse(self.switched())
        self.assertEqual(self.health, ["0.22.2"])
        self.assertEqual(self.hook_actions(), [])

    def test_unchanged_pins_still_refresh_an_idle_stale_gateway(self):
        self.change_pins = False
        self.expected_package = "/nix/store/new-hermes"
        self.perform()
        self.assertFalse(self.switched())
        self.assertEqual(self.running_package, self.expected_package)
        self.assertIn(["systemctl", "restart", "hermes-agent.service"], self.calls)

    def test_grafted_built_gateway_path_overrides_ungrafted_evaluation(self):
        self.expected_package = "/nix/store/grafted-hermes-agent-env"
        self.evaluated_package = "/nix/store/ungrafted-hermes-agent"
        self.perform()
        self.assertEqual(self.running_package, self.expected_package)
        update.gateway_package_from_system.assert_called_with(self.target)
        self.assertEqual(self.health, ["0.22.2"])

    def test_unchanged_pins_resolve_grafted_gateway_from_current_system(self):
        self.change_pins = False
        self.expected_package = "/nix/store/grafted-hermes-agent-env"
        self.evaluated_package = "/nix/store/ungrafted-hermes-agent"
        self.perform()
        self.assertFalse(self.switched())
        self.assertEqual(self.running_package, self.expected_package)
        update.gateway_package_from_system.assert_called_with(self.previous)

    def test_validated_pins_sync_only_after_success(self):
        self.perform()
        self.assertEqual((self.canonical / "flake.lock").read_text(), "new lock\n")
        self.assertEqual((self.source / "flake.lock").read_text(), "old lock\n")
        checks = next(i for i, args in enumerate(self.calls) if args[:3] == ["nix", "flake", "check"])
        backup = next(i for i, args in enumerate(self.calls) if args[:2] == [self.hook, "backup"])
        self.assertLess(checks, backup)

    def test_idle_lifecycle_contract_ignores_history_and_requires_every_field(self):
        idle = {"gateway_running": True, "gateway_busy": False, "gateway_drainable": True,
                "active_agents": 0, "active_sessions": 1}
        with patch.object(update, "hermes_status", return_value=idle):
            update.require_hermes_idle(self.run_command)
        for field in ("gateway_running", "gateway_busy", "gateway_drainable", "active_agents"):
            with self.subTest(missing=field):
                incomplete = {key: value for key, value in idle.items() if key != field}
                with patch.object(update, "hermes_status", return_value=incomplete):
                    with self.assertRaisesRegex(RuntimeError, "active or unknown work"):
                        update.require_hermes_idle(self.run_command)

    def test_busy_hermes_prevents_deployment(self):
        self.expected_package = "/nix/store/new-hermes"
        with patch.object(update, "hermes_status", return_value={"gateway_running": True, "gateway_busy": True,
                                                                 "gateway_drainable": True, "active_agents": 1, "active_sessions": 1}):
            with self.assertRaisesRegex(RuntimeError, "active or unknown work"):
                self.perform()
            self.assertFalse(self.switched())
        self.assertEqual(self.hook_actions(), [])

    def test_unchanged_application_units_skip_all_state_hooks_and_idle_gate(self):
        self.app_change = False
        with patch.object(update, "require_hermes_idle") as idle:
            self.perform()
        self.assertTrue(self.switched())
        self.assertEqual(self.hook_actions(), [])
        idle.assert_not_called()
        self.assertEqual(self.health, ["0.22.2"])

    def test_no_application_changes_still_roll_back_a_failed_generation(self):
        self.app_change = False
        self.failure = lambda args: args[:2] == ["nixos-rebuild", "switch"]
        with self.assertRaisesRegex(RuntimeError, "prior system/state were restored"):
            self.perform()
        self.assertEqual(self.hook_actions(), [])
        self.assertTrue(any(args[0] == "nix-env" for args in self.calls))

    def test_docker_dropin_change_invokes_fleet_guard(self):
        for system in (self.previous, self.target):
            units = system / "etc/systemd/system"
            units.mkdir(parents=True)
            for name in update.DOCKER_UNITS:
                (units / name).write_text("same main unit")
            (units / "docker.service.d").mkdir()
            (units / "docker.service.d/overrides.conf").write_text(str(system))
        events = []

        @contextmanager
        def guard(directory):
            events.append("enter")
            yield
            events.append("exit")

        with patch.object(update, "docker_units_changed", side_effect=self.real_docker_changed):
            self.perform(fleet_factory=guard)
        self.assertEqual(events, ["enter", "exit"])

    def test_runner_unit_change_gates_busy_or_unknown_work_before_activation(self):
        self.app_change = False
        for system in (self.previous, self.target):
            units = system / "etc/systemd/system"
            units.mkdir(parents=True)
            for name in update.DOCKER_UNITS:
                (units / name).write_text("unchanged Docker daemon")
            (units / "github-actions-app.service").write_text(f"ExecStart={system}-image")
        entered = []
        for state in ("busy", "unknown"):
            with self.subTest(state=state):
                @contextmanager
                def guard(directory):
                    entered.append(state)
                    raise RuntimeError(f"runner work is {state}")
                    yield

                with patch.object(update, "docker_units_changed", side_effect=self.real_docker_changed):
                    with self.assertRaisesRegex(RuntimeError, f"runner work is {state}"):
                        self.perform(fleet_factory=guard)
                self.assertFalse(self.switched())
                self.assertEqual(self.hook_actions(), [])
                self.assertEqual(self.health, [])
        self.assertEqual(entered, ["busy", "unknown"])

    def test_switch_failure_rolls_back_before_fleet_resumes(self):
        self.docker_change = True
        events = []
        original = self.run_command

        @contextmanager
        def guard(directory):
            events.append("fleet-stop")
            try:
                yield
            finally:
                events.append("fleet-resume")

        def run(args):
            if args[0] == str(self.previous / "bin/switch-to-configuration"):
                events.append("old-activation")
            return original(args)

        self.run_command = run
        self.failure = lambda args: args[:2] == ["nixos-rebuild", "switch"]
        with self.assertRaisesRegex(RuntimeError, "prior system/state were restored"):
            self.perform(fleet_factory=guard)
        self.assertLess(events.index("old-activation"), events.index("fleet-resume"))

    def test_fleet_exit_failure_triggers_generation_rollback_and_manifest_recovery(self):
        self.docker_change = True
        self.app_change = False
        events = []
        original = self.run_command

        @contextmanager
        def guard(directory):
            directory.mkdir()
            (directory / "fleet.json").write_text(json.dumps({"resumed": False}))
            events.append(directory.name + "-enter")
            try:
                yield
            finally:
                events.append(directory.name + "-exit")
            if directory.name == "fleet":
                raise RuntimeError("fleet resume failed")

        def run(args):
            if args[0] == str(self.previous / "bin/switch-to-configuration"):
                events.append("old-docker-activation")
            return original(args)
        self.run_command = run

        module = SimpleNamespace(guard=guard, resume=Mock(), runners=Mock(return_value="runner-module"))
        with patch.object(update, "fleet_module", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "prior system/state were restored"):
                self.perform()
        self.assertTrue(any(args[0] == "nix-env" for args in self.calls))
        self.assertEqual(self.health, ["0.22.2", None])
        module.resume.assert_called_once()
        self.assertEqual(module.resume.call_args.args[1], "runner-module")
        self.assertTrue(module.resume.call_args.args[0]["resumed"])
        self.assertEqual(self.hook_actions(), [])
        self.assertLess(events.index("fleet-exit"), events.index("fleet-rollback-enter"))
        self.assertLess(events.index("fleet-rollback-enter"), events.index("old-docker-activation"))
        self.assertLess(events.index("old-docker-activation"), events.index("fleet-rollback-exit"))

    def test_partial_fleet_resume_with_new_work_blocks_docker_rollback(self):
        self.docker_change = True
        self.app_change = False
        entered = []

        @contextmanager
        def guard(directory):
            entered.append(directory.name)
            if directory.name == "fleet-rollback":
                raise RuntimeError("runner accepted new work")
            directory.mkdir()
            (directory / "fleet.json").write_text(json.dumps({"resumed": False}))
            yield
            raise RuntimeError("partial fleet resume failed")

        module = SimpleNamespace(guard=guard, resume=Mock(), runners=Mock(return_value="runner-module"))
        with patch.object(update, "fleet_module", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "rollback needs attention"):
                self.perform()
        self.assertEqual(entered, ["fleet", "fleet-rollback"])
        self.assertFalse(any(args[0] in ("nix-env", str(self.previous / "bin/switch-to-configuration")) for args in self.calls))
        self.assertEqual(self.health, ["0.22.2"])
        self.assertEqual(self.hook_actions(), [])
        module.resume.assert_called_once()
        result = json.loads((self.root / "state/latest.json").read_text())
        self.assertEqual(result["status"], "rollback-failed")
        self.assertIn("active work was left intact", result["rollback_errors"][0])

    def test_missing_fleet_module_blocks_docker_change_before_services_stop(self):
        self.docker_change = True
        with patch.dict(update.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "require the configured fleet"):
                self.perform()
        self.assertFalse(self.switched())
        self.assertEqual(self.hook_actions(), [])

    def test_rollback_ignores_further_termination_signals(self):
        self.failure = lambda args: args[:2] == ["nixos-rebuild", "switch"]
        with patch.object(update.signal, "signal") as setter:
            with self.assertRaisesRegex(RuntimeError, "prior system/state were restored"):
                self.perform()
        setter.assert_any_call(signal.SIGTERM, signal.SIG_IGN)
        setter.assert_any_call(signal.SIGINT, signal.SIG_IGN)


class HistoryAndUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_unit_comparison_dereferences_main_and_dropin_symlinks(self):
        systems = [self.root / "old", self.root / "new"]
        for index, system in enumerate(systems):
            units = system / "etc/systemd/system"
            units.mkdir(parents=True)
            main = self.root / f"main-{index}"
            main.write_text("same main unit")
            (units / "docker.service").symlink_to(main)
            (units / "docker.socket").write_text("same socket")
            (units / "docker.service.d").mkdir()
            dropin = self.root / f"dropin-{index}"
            dropin.write_text("same dropin")
            (units / "docker.service.d/overrides.conf").symlink_to(dropin)
        self.assertFalse(update.docker_units_changed(*systems))
        (self.root / "dropin-1").write_text("new ExecStart")
        self.assertTrue(update.docker_units_changed(*systems))
        self.assertFalse(update.application_units_changed(*systems))
        (systems[1] / "etc/systemd/system/hermes-agent.service").write_text("new gateway")
        self.assertTrue(update.application_units_changed(*systems))

    def test_managed_runner_add_remove_main_and_dropin_changes_require_fleet(self):
        for change in ("add", "remove", "main", "dropin"):
            with self.subTest(change=change):
                systems = [self.root / change / name for name in ("old", "new")]
                for system in systems:
                    units = system / "etc/systemd/system"
                    units.mkdir(parents=True)
                    for unit in update.DOCKER_UNITS:
                        (units / unit).write_text("same daemon")
                    (units / "github-actions-app.service").write_text("same runner")
                    (units / "github-actions-maintenance.service").write_text(str(system))
                self.assertFalse(update.docker_units_changed(*systems))
                before, after = [system / "etc/systemd/system/github-actions-app.service" for system in systems]
                if change == "add":
                    before.unlink()
                elif change == "remove":
                    after.unlink()
                elif change == "main":
                    after.write_text("new runner image")
                else:
                    dropins = after.with_name(after.name + ".d")
                    dropins.mkdir()
                    (dropins / "overrides.conf").write_text("new runner command")
                self.assertTrue(update.docker_units_changed(*systems))

    def test_gateway_resolution_uses_effective_dropin_and_final_grafted_entrypoint(self):
        system = self.root / "system"
        units = system / "etc/systemd/system"
        units.mkdir(parents=True)
        (units / "hermes-agent.service").write_text("[Service]\nExecStart=/nix/store/old-hermes-gateway\n")
        (units / "hermes-agent.service.d").mkdir()
        (units / "hermes-agent.service.d/overrides.conf").write_text(
            "[Service]\nExecStart=\nExecStart=/nix/store/grafted-hermes-gateway\n")
        scripts = {
            "/nix/store/grafted-hermes-gateway": "exec /nix/store/grafted-hermes-agent/bin/hermes gateway\n",
            "/nix/store/grafted-hermes-agent/bin/hermes": 'exec "/nix/store/grafted-hermes-agent-env/bin/hermes" "$@"\n',
            "/nix/store/grafted-hermes-agent-env/bin/hermes": "#!/nix/store/grafted-hermes-agent-env/bin/python3.12\nimport sys\n",
        }
        self.assertEqual(update.gateway_package_from_system(system, read_script=scripts.__getitem__),
                         "/nix/store/grafted-hermes-agent-env")
        scripts["/nix/store/grafted-hermes-agent/bin/hermes"] = "#!/bin/sh\nexec unknown-command\n"
        with self.assertRaisesRegex(RuntimeError, "final Hermes Python entry point"):
            update.gateway_package_from_system(system, read_script=scripts.__getitem__)

    def test_ambiguous_gateway_execstart_fails_closed(self):
        units = self.root / "etc/systemd/system"
        units.mkdir(parents=True)
        (units / "hermes-agent.service").write_text(
            "[Service]\nExecStart=/nix/store/old-hermes-gateway\nExecStart=/nix/store/new-hermes-gateway\n")
        with self.assertRaisesRegex(RuntimeError, "ExecStart wrapper"):
            update.gateway_package_from_system(self.root)

    def test_retention_keeps_recent_failures_manual_and_symlinked_paths(self):
        state = self.root / "state"
        state.mkdir()
        histories = []
        for month in range(1, 7):
            directory = state / f"2020{month:02}01T000000Z-codex-abcdefgh"
            directory.mkdir()
            (directory / "result.json").write_text(json.dumps({"status": "succeeded"}))
            histories.append(directory)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").touch()
        (histories[1] / "outside-link").symlink_to(outside)
        symlink = state / "20190101T000000Z-all-abcdefgh"
        symlink.symlink_to(outside)
        failure = state / "20180101T000000Z-hermes-abcdefgh"
        failure.mkdir()
        (failure / "result.json").write_text(json.dumps({"status": "rolled-back"}))
        manual = state / "manual-backup"
        manual.mkdir()
        update.prune_successful(state, histories[0], lambda message: None,
                                now=datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp())
        self.assertTrue(histories[0].exists())
        self.assertFalse(histories[1].exists())
        self.assertFalse(histories[2].exists())
        self.assertTrue(all(directory.exists() for directory in histories[3:]))
        self.assertTrue(failure.exists())
        self.assertTrue(manual.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue((outside / "keep").exists())

    def test_reboot_marker_tracks_kernel_target_without_rebooting(self):
        state, booted, target = (self.root / name for name in ("state", "booted", "target"))
        for directory in (state, booted, target):
            directory.mkdir()
        old_kernel, new_kernel = self.root / "old-kernel", self.root / "new-kernel"
        old_kernel.touch()
        new_kernel.touch()
        (booted / "kernel").symlink_to(old_kernel)
        (target / "kernel").symlink_to(new_kernel)
        update.mark_reboot_required(state, target, lambda message: None, booted=booted)
        self.assertTrue((state / "reboot-required").exists())
        (booted / "kernel").unlink()
        (booted / "kernel").symlink_to(new_kernel)
        update.mark_reboot_required(state, target, lambda message: None, booted=booted)
        self.assertFalse((state / "reboot-required").exists())


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.maintenance_lock = root / "maintenance.lock"
        self.deployment_lock = root / "deployment.lock"
        self.calls = []
        self.states = {name + ".service": "active" for name in update.SERVICES}
        health = patch.object(update, "health_check")
        self.health = health.start()
        self.addCleanup(health.stop)

    def run_command(self, args):
        self.calls.append(args)
        if args[:2] == ["systemctl", "show"]:
            return self.states[args[-1]] + "\n"
        return ""

    def recover(self, run=None):
        return update.recover_services(
            run or self.run_command, maintenance_lock=self.maintenance_lock,
            deployment_lock=self.deployment_lock,
        )

    def test_starts_only_failed_or_inactive_critical_services(self):
        self.states["jellyfin.service"] = "failed"
        self.states["redis-nextcloud.service"] = "inactive"
        for name, state in zip(("hermes-agent", "hermes-dashboard", "nginx", "docker"),
                               ("active", "activating", "reloading", "deactivating")):
            self.states[name + ".service"] = state
        self.states["postgresql.service"] = "unknown"
        self.assertTrue(self.recover())
        starts = [args for args in self.calls if args[:2] == ["systemctl", "start"]]
        self.assertEqual(starts, [["systemctl", "start", "jellyfin.service"],
                                  ["systemctl", "start", "redis-nextcloud.service"]])
        self.assertFalse(any("restart" in args or "reset-failed" in args for args in self.calls))
        self.assertFalse(any(name in args[-1] for args in self.calls
                             for name in ("sonarr", "radarr", "github-actions", "dev-")))
        self.health.assert_called_once()

    def test_failed_start_does_not_prevent_other_services_recovering(self):
        self.states["jellyfin.service"] = self.states["redis-nextcloud.service"] = "failed"

        def run(args):
            result = self.run_command(args)
            if args == ["systemctl", "start", "jellyfin.service"]:
                raise RuntimeError("start limit remains in effect")
            return result

        self.health.side_effect = RuntimeError("Jellyfin is still unavailable")
        with self.assertRaisesRegex(RuntimeError, "still unavailable"):
            self.recover(run)
        self.assertIn(["systemctl", "start", "redis-nextcloud.service"], self.calls)
        self.health.assert_called_once()

    def test_either_maintenance_lock_skips_all_probes_and_mutations(self):
        for path in (self.maintenance_lock, self.deployment_lock):
            with self.subTest(path=path.name), path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(self.recover())
        self.assertEqual(self.calls, [])
        self.health.assert_not_called()

    def test_both_locks_remain_held_through_recovery_and_health_check(self):
        self.states["jellyfin.service"] = "failed"

        def assert_locked():
            for path in (self.maintenance_lock, self.deployment_lock):
                with path.open("a") as lock, self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def run(args):
            assert_locked()
            return self.run_command(args)

        self.health.side_effect = lambda run: assert_locked()
        self.assertTrue(self.recover(run))

    def test_unhealthy_active_services_are_reported_without_restarting(self):
        self.health.side_effect = RuntimeError("Hermes gateway is not healthy")
        with self.assertRaisesRegex(RuntimeError, "Hermes gateway"):
            self.recover()
        self.assertTrue(all(args[:2] == ["systemctl", "show"] for args in self.calls))

    def test_recover_cli_never_runs_an_update(self):
        with patch("sys.argv", ["system-update", "--recover"]), \
                patch.object(update, "recover_services") as recover, \
                patch.object(update, "update") as perform, \
                patch.object(update.signal, "signal"), patch.object(update.os, "umask"):
            self.assertEqual(update.main(), 0)
        recover.assert_called_once()
        perform.assert_not_called()

    def test_check_and_recover_are_mutually_exclusive(self):
        with patch("sys.argv", ["system-update", "--check", "--recover"]), \
                patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
            update.main()
        self.assertEqual(error.exception.code, 2)


class HealthTests(unittest.TestCase):
    def test_missing_optional_units_do_not_force_unrelated_app_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            previous, candidate = (Path(directory) / name for name in ("previous", "candidate"))
            for root in (previous, candidate):
                (root / "etc/systemd/system").mkdir(parents=True)
                (root / "etc/systemd/system/hermes-agent.service").write_text("unchanged gateway\n")
            self.assertFalse(update.units_changed(previous, candidate,
                                                  ("hermes-agent.service", "nextcloud-update-db.service")))

    def run_command(self, args):
        self.calls.append(args)
        if args[0] == "dig":
            return "status: NOERROR\nlocalhost. 60 IN A 127.0.0.1\n"
        if args[0] == "curl":
            if args[-1].endswith("status.php"):
                return json.dumps(self.nextcloud)
            if args[-1].endswith("/health"):
                return "Healthy"
            if args[-1].endswith("/api/status"):
                return json.dumps({"gateway_running": True, "overall": "ok", "version": "0.22.2"})
        return ""

    def setUp(self):
        self.calls = []
        self.nextcloud = {"installed": True, "maintenance": False, "needsDbUpgrade": False}

    def test_health_uses_local_endpoints_and_excludes_on_demand_services(self):
        update.health_check(self.run_command, "0.22.2")
        services = [unit for args in self.calls if args[:2] == ["systemctl", "is-active"]
                    for unit in args[3:]]
        self.assertNotIn("sonarr.service", services)
        self.assertNotIn("radarr.service", services)
        self.assertTrue(all("example.com" not in args for args in self.calls))
        nextcloud = next(args for args in self.calls if args[-1].endswith("status.php"))
        self.assertEqual(nextcloud[-1], "https://cloud.amahi.dev/status.php")
        self.assertEqual(nextcloud[nextcloud.index("--resolve") + 1], "cloud.amahi.dev:443:127.0.0.1")
        self.assertIn("http://127.0.0.1:8222/alive", [args[-1] for args in self.calls])

    def test_recovery_reports_a_failed_non_http_service(self):
        def run(args):
            if args[:2] == ["systemctl", "show"]:
                return "failed" if args[-1] == "samba-smbd.service" else "active"
            if args == ["systemctl", "start", "samba-smbd.service"]:
                raise RuntimeError("Samba start failed")
            if args[:3] == ["systemctl", "is-active", "--quiet"]:
                # systemctl succeeds if any named unit is active.
                if not any(unit != "samba-smbd.service" for unit in args[3:]):
                    raise RuntimeError("Samba remains failed")
            return self.run_command(args)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "Samba remains failed"):
                update.recover_services(
                    run, maintenance_lock=Path(directory) / "maintenance.lock",
                    deployment_lock=Path(directory) / "deployment.lock",
                )

    def test_nextcloud_maintenance_and_pending_migrations_fail_health(self):
        for key in ("maintenance", "needsDbUpgrade"):
            with self.subTest(key=key):
                self.nextcloud = {"installed": True, "maintenance": False, "needsDbUpgrade": False, key: True}
                with self.assertRaisesRegex(RuntimeError, "Nextcloud is not ready"):
                    update.health_check(self.run_command)


if __name__ == "__main__":
    unittest.main()
