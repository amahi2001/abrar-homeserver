"""Safety checks for the one-time migration controller, without host changes."""

from contextlib import contextmanager, ExitStack, closing
import copy
import importlib.util
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, mock_open


spec = importlib.util.spec_from_file_location("account_rename", Path(__file__).with_name("account_rename.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class ControllerTests(unittest.TestCase):
    @contextmanager
    def homes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            old.mkdir()
            account = SimpleNamespace(pw_uid=1000, pw_gid=100, pw_dir=str(old))
            def lookup(name):
                if name == core.OLD:
                    return account
                raise KeyError(name)
            with patch.object(core.pwd, "getpwnam", side_effect=lookup):
                yield old, new

    def test_preflight_accepts_preserved_identity(self):
        with self.homes() as (old, new):
            self.assertEqual(core.preflight(old, new).pw_uid, 1000)

    def test_preflight_refuses_existing_home(self):
        with self.homes() as (old, new):
            new.mkdir()
            with self.assertRaisesRegex(RuntimeError, "Destination home"):
                core.preflight(old, new)

    def test_preflight_refuses_broken_destination_link(self):
        with self.homes() as (old, new):
            new.symlink_to(new.parent / "missing")
            with self.assertRaisesRegex(RuntimeError, "Destination home"):
                core.preflight(old, new)

    def test_preflight_refuses_existing_account(self):
        with self.homes() as (old, new):
            account = SimpleNamespace(pw_uid=1000, pw_gid=100, pw_dir=str(old))
            with patch.object(core.pwd, "getpwnam", return_value=account):
                with self.assertRaisesRegex(RuntimeError, "Destination account"):
                    core.preflight(old, new)

    def test_foreign_uid_app_in_old_session_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, uid, group in [(1, 993, '/user.slice/user-1000.slice/session-5.scope'),
                                    (2, 0, '/user.slice/user-1000.slice/session-5.scope'),
                                    (3, 1000, '/user.slice/user-1000.slice/user@1000.service'),
                                    (4, 993, '/system.slice/hermes-webui-manual.service'),
                                    (5, 0, '/user.slice/user-1000.slice/session-5.scope')]:
                proc = root / str(pid)
                proc.mkdir()
                (proc / 'cgroup').write_text('0::' + group + '\n')
                (proc / 'status').write_text(f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
                (proc / 'comm').write_text('sshd-session\n' if pid == 2 else 'python\n')
            with patch.object(core.os, "getpid", return_value=424242):
                self.assertEqual(core.foreign_session_processes(root), [{"pid": 1, "uid": 993}, {"pid": 5, "uid": 0}])

    def test_preflight_refuses_hidden_session_dependency_before_changes(self):
        with self.homes() as (old, new):
            with patch.object(core, "foreign_session_processes", return_value=[{"pid": 7, "uid": 993}]):
                with self.assertRaisesRegex(RuntimeError, "Foreign-UID"):
                    core.preflight(old, new)

    def test_rebase_retains_legacy_codex_trust(self):
        original = ('[projects."/home/buildfleet/projects/example"]\ntrust_level = "trusted"\n'
                    '[hooks]\ncommand = "/home/buildfleet/hook"\n')
        value = core.rebase_config(original, ".codex/config.toml")
        self.assertIn('[projects."/home/amahi/projects/example"]', value)
        self.assertIn('[projects."/home/buildfleet/projects/example"]', value)
        self.assertIn('command = "/home/amahi/hook"', value)
        self.assertNotIn('command = "/home/buildfleet/hook"', value)

    def test_rebase_preserves_unrelated_identity(self):
        original = 'email=buildfleet@example.org\nhost=buildfleet-server\npath=/home/buildfleet/a\n'
        value = core.rebase_config(original, ".gitconfig")
        self.assertIn('email=buildfleet@example.org', value)
        self.assertIn('host=buildfleet-server', value)
        self.assertIn('path=/home/amahi/a', value)

    def test_history_probe_skips_previously_deleted_project_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".codex").mkdir()
            rollout = home / "readable.jsonl"
            rollout.write_text('{}\n')
            database = home / ".codex/state_5.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('CREATE TABLE threads (cwd TEXT, rollout_path TEXT)')
                connection.execute('INSERT INTO threads VALUES (?, ?)', (str(home / "deleted"), str(home / "missing")))
                connection.execute('INSERT INTO threads VALUES (?, ?)', (str(home), str(rollout)))
                connection.commit()
            with patch.object(core, "OLD_HOME", home):
                sample = core.history_state(home)
            self.assertEqual(sample["count"], 2)
            self.assertEqual(sample["sample"], [str(home), str(rollout)])

    def test_atomic_preserves_mode_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account"
            path.write_bytes(b"old")
            path.chmod(0o640)
            core.atomic(path, b"new")
            self.assertEqual(path.read_bytes(), b"new")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_atomic_refuses_preexisting_temporary_link(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account"
            path.write_text("old")
            path.with_name(path.name + ".account-rename.tmp").symlink_to(path)
            with self.assertRaises(FileExistsError):
                core.atomic(path, b"new")
            self.assertEqual(path.read_text(), "old")

    def test_save_omits_private_credentials_from_public_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"phase": "prepared", "samba": {"credential_fingerprint": "secret"}}
            with patch.object(core, "STATE", root), patch.object(core, "MANIFEST", root / "manifest.json"), \
                 patch.object(core.os, "chown"):
                core.save(state)
            self.assertNotIn("secret", (root / "status.json").read_text())
            self.assertEqual((root / "manifest.json").stat().st_mode & 0o777, 0o600)

    def test_confirmation_requires_new_login(self):
        with patch.object(core, "load", return_value={"phase": "awaiting-confirmation"}), \
             patch.dict(os.environ, {"SUDO_USER": "buildfleet"}), patch.object(core, "verify") as verify:
            with self.assertRaisesRegex(RuntimeError, "fresh amahi"):
                core.confirm()
            verify.assert_not_called()

    def test_confirmation_checks_before_disarming_watchdog(self):
        state = {"phase": "awaiting-confirmation"}
        with patch.object(core, "load", return_value=state), patch.dict(os.environ, {"SUDO_USER": "amahi"}), \
             patch.object(core, "verify", side_effect=RuntimeError("invariant failed")), \
             patch.object(core, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "invariant failed"):
                core.confirm()
            run.assert_not_called()

    def test_push_failure_does_not_undo_confirmed_account(self):
        state = {"phase": "awaiting-confirmation"}
        def save(value, phase=None):
            if phase:
                value["phase"] = phase
        with patch.object(core, "load", return_value=state), patch.dict(os.environ, {"SUDO_USER": "amahi"}), \
             patch.object(core, "verify"), patch.object(core, "save", side_effect=save), \
             patch.object(core, "run") as run, patch.object(core, "rollback") as rollback, \
             patch.object(core, "finalize_project_source", side_effect=RuntimeError("push unavailable")):
            with self.assertRaisesRegex(RuntimeError, "push unavailable"):
                core.confirm()
            self.assertEqual(state["phase"], "confirmed")
            run.assert_called_once_with(["systemctl", "stop", "account-rename-rollback.timer"], check=False)
            rollback.assert_not_called()

    def test_confirmed_retry_and_rollback_are_noops(self):
        with patch.object(core, "load", return_value={"phase": "confirmed", "token_eye_pushed": True}), patch.object(core, "run") as run:
            core.confirm()
            core.rollback()
            run.assert_not_called()

    def test_callback_requires_quiesced_transaction(self):
        with patch.object(core, "load", return_value={"phase": "prepared"}), patch.object(core, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "armed transaction"):
                core.unix_rename()
            run.assert_not_called()

    def test_prepared_watchdog_does_not_disconnect_unchanged_account(self):
        state = {"phase": "prepared"}
        with patch.object(core, "save") as save, patch.object(core, "run") as run:
            core.rollback(state)
            save.assert_called_once_with(state, "rolled-back")
            run.assert_not_called()

    def test_partial_home_move_rolls_back_without_restoring_unmodified_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            new.mkdir()
            (new / "sentinel").write_text("preserved")
            old.symlink_to(new)
            state = {"phase": "unix-renamed", "timers": [], "runtime_files": {},
                     "account_files": {}, "development": {"projects": []},
                     "previous": "/nix/store/previous", "shadow_digest": hashlib.sha256(b"saved").hexdigest()}
            with patch.object(core, "OLD_HOME", old), patch.object(core, "NEW_HOME", new), \
                 patch.object(core, "save") as save, patch.object(core, "run"), \
                 patch.object(core, "host_uid_processes", return_value=[]), \
                 patch.object(core, "stop_user_runtime"), patch.object(core, "start_user_runtime"), \
                 patch.object(core, "user_command"), \
                 patch.object(core, "activate"), patch.object(core, "shadow_tail", return_value="saved"), \
                 patch.object(core.development, "rollback_files") as restore:
                core.rollback(state)
                restore.assert_not_called()
                save.assert_called_with(state, "rolled-back")
            self.assertFalse(old.is_symlink())
            self.assertFalse(new.exists())
            self.assertEqual((old / "sentinel").read_text(), "preserved")

    def test_rollback_refuses_unexpected_compatibility_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new, unrelated = root / "old", root / "new", root / "unrelated"
            new.mkdir()
            unrelated.mkdir()
            old.symlink_to(unrelated)
            state = {"phase": "activated", "timers": [], "development": {"projects": []}}
            with patch.object(core, "OLD_HOME", old), patch.object(core, "NEW_HOME", new), \
                 patch.object(core, "save"), patch.object(core, "run"), \
                 patch.object(core, "stop_user_runtime"), \
                 patch.object(core, "host_uid_processes", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "unexpected old-home"):
                    core.rollback(state)
            self.assertEqual(old.resolve(), unrelated)
            self.assertTrue(new.is_dir())

    @contextmanager
    def apply_fixture(self, failure):
        """Use real temporary homes and an in-memory journal; mock every host action."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            root = Path(directory)
            old, new = root / "old", root / "new"
            (old / ".local/state/dev/projects").mkdir(parents=True)
            (old / "sentinel").write_text("preserve data")
            fleet = {"projects": [
                {"name": "token-eye", "container": "old-token", "running": True},
                {"name": "prompt-optimizer", "container": "old-optimizer", "running": False},
            ]}
            journal = {"phase": "prepared", "previous": "/nix/store/previous",
                       "target": "/nix/store/target", "development": copy.deepcopy(fleet),
                       "timers": [], "account_files": {}, "runtime_files": {},
                       "failed_units": [],
                       "credentials": {".codex/auth.json": "same-digest"}}
            events = []

            def save(state, phase=None):
                if phase:
                    state["phase"] = phase
                journal.clear()
                journal.update(copy.deepcopy(state))
                events.append(("save", state["phase"]))

            def command(args, **kwargs):
                events.append(("run", list(map(str, args))))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def rename(**kwargs):
                self.assertEqual(journal["phase"], "quiesced")
                old.rename(new)
                old.symlink_to(new)
                # The real Samba callback persists progress in a separate process.
                journal["phase"] = "unix-renamed"
                journal["callback_progress"] = "must survive controller reload"
                events.append(("callback", "unix-renamed"))

            def runtime(state):
                self.assertTrue(new.is_dir())
                self.assertTrue(old.is_symlink())
                if failure == "runtime":
                    raise RuntimeError("injected failure after Samba rename")

            def start(*args, **kwargs):
                if failure == "native-start":
                    raise RuntimeError("injected native-start failure")

            for name, value in [
                ("OLD_HOME", old), ("NEW_HOME", new), ("PRIVATE", root / "private"),
                ("ACCOUNT_FILES", ("/fake/passwd",)),
            ]:
                patches.enter_context(patch.object(core, name, value))
            patches.enter_context(patch.object(core, "load", side_effect=lambda: copy.deepcopy(journal)))
            patches.enter_context(patch.object(core, "save", side_effect=save))
            patches.enter_context(patch.object(core, "run", side_effect=command))
            patches.enter_context(patch.object(core, "preflight"))
            patches.enter_context(patch.object(core.Path, "resolve", return_value=Path("/nix/store/previous")))
            patches.enter_context(patch("builtins.open", mock_open()))
            patches.enter_context(patch.object(core.fcntl, "flock"))
            patches.enter_context(patch.object(core, "pause"))
            patches.enter_context(patch.object(core, "backup", return_value=True))
            patches.enter_context(patch.object(core, "present", return_value=False))
            patches.enter_context(patch.object(core, "runtime_paths", return_value=[]))
            patches.enter_context(patch.object(core, "stop_user_runtime"))
            patches.enter_context(patch.object(core, "start_user_runtime"))
            patches.enter_context(patch.object(core.os, "getgrouplist", return_value=[1, 100]))
            patches.enter_context(patch.object(core, "shadow_tail", return_value="dummy shadow state"))
            patches.enter_context(patch.object(core, "digest", return_value="same-digest"))
            patches.enter_context(patch.object(core.samba, "snapshot", return_value={"sid": "same"}))
            patches.enter_context(patch.object(core.samba, "rename", side_effect=rename))
            patches.enter_context(patch.object(core.development, "capture", return_value=fleet))
            patches.enter_context(patch.object(core, "migrate_runtime", side_effect=runtime))
            patches.enter_context(patch.object(core.development, "migrate"))
            native = patches.enter_context(patch.object(core.development, "start", side_effect=start))
            activate = patches.enter_context(patch.object(core, "activate"))
            verify = patches.enter_context(patch.object(core, "verify"))
            rollback = patches.enter_context(patch.object(core, "rollback"))
            yield SimpleNamespace(journal=journal, events=events, rollback=rollback,
                                  activate=activate, verify=verify, native=native, fleet=fleet)

    def test_failure_after_samba_callback_rolls_back_latest_journal(self):
        with self.apply_fixture("runtime") as fixture:
            with self.assertRaisesRegex(RuntimeError, "after Samba rename"):
                core.apply()
            fixture.rollback.assert_called_once()
            state = fixture.rollback.call_args.args[0]
            self.assertEqual(state["phase"], "rolling-back")
            self.assertEqual(state["callback_progress"], "must survive controller reload")
            self.assertTrue(state["runtime_migration_started"])
            self.assertNotIn("development_migration_started", state)
            self.assertTrue(state["samba_backup"])
            fixture.activate.assert_not_called()
            fixture.native.assert_not_called()
            fixture.verify.assert_not_called()
            commands = [event[1] for event in fixture.events if event[0] == "run"]
            self.assertTrue(any(command[0] == "systemd-run" and "--on-active=30min" in command
                                for command in commands))
            self.assertEqual([command for command in commands if command[:2] == ["docker", "stop"]],
                             [["docker", "stop", "--time", "30", "old-token"]])

    def test_native_start_failure_rolls_back_activated_generation_and_files(self):
        with self.apply_fixture("native-start") as fixture:
            with self.assertRaisesRegex(RuntimeError, "native-start"):
                core.apply()
            fixture.rollback.assert_called_once()
            state = fixture.rollback.call_args.args[0]
            self.assertEqual(state["phase"], "rolling-back")
            self.assertEqual(state["callback_progress"], "must survive controller reload")
            self.assertTrue(state["runtime_migration_started"])
            self.assertTrue(state["development_migration_started"])
            self.assertTrue(state["credentials_preserved"])
            self.assertEqual(state["previous"], "/nix/store/previous")
            self.assertEqual(state["target"], "/nix/store/target")
            fixture.activate.assert_called_once_with("/nix/store/target", [])
            fixture.native.assert_called_once()
            fixture.verify.assert_not_called()

    def test_rollback_stops_new_ids_and_resumes_only_original_running_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            new.mkdir()
            (new / "sentinel").write_text("same data")
            old.symlink_to(new)
            projects = [
                {"name": "token-eye", "container": "original-token", "running": True},
                {"name": "agent-config", "container": "original-agent", "running": True},
                {"name": "prompt-optimizer", "container": "original-optimizer", "running": False},
            ]
            state = {"phase": "activated", "timers": ["paused.timer"],
                     "development": {"projects": projects}, "previous": "/nix/store/previous",
                     "runtime_migration_started": True, "development_migration_started": True,
                     "runtime_files": {".codex/config.toml": True},
                     "account_files": {"/fake/passwd": True}, "samba_backup": True,
                     "shadow_digest": hashlib.sha256(b"original shadow").hexdigest()}
            new_ids = {core.development.compose_identity(item["name"], new / "projects" / item["name"]):
                       "new-" + item["name"] for item in projects if item["running"]}
            events = []

            def command(args, **kwargs):
                args = list(map(str, args))
                events.append(("run", args))
                output = ""
                if args[:3] == ["docker", "ps", "-q"]:
                    identity = args[-1].split("=", 2)[2]
                    output = new_ids.get(identity, "")
                return SimpleNamespace(returncode=0, stdout=output, stderr="")

            def save(current, phase=None):
                if phase:
                    current["phase"] = phase
                events.append(("save", current["phase"]))

            with patch.object(core, "OLD_HOME", old), patch.object(core, "NEW_HOME", new), \
                 patch.object(core, "PRIVATE", root / "private"), \
                 patch.object(core, "save", side_effect=save), \
                 patch.object(core, "run", side_effect=command), \
                 patch.object(core, "stop_user_runtime"), patch.object(core, "start_user_runtime"), \
                 patch.object(core, "user_command"), patch.object(core, "resume_timers"), \
                 patch.object(core, "shadow_tail", return_value="original shadow"), \
                 patch.object(core, "restore", side_effect=lambda *args: events.append(("restore", args))), \
                 patch.object(core, "activate", side_effect=lambda target, *args: events.append(("activate", target))), \
                 patch.object(core.development, "rollback_files") as rollback_files:
                core.rollback(state)
                rollback_files.assert_called_once_with(state["development"], old)
            commands = [event[1] for event in events if event[0] == "run"]
            self.assertEqual([command for command in commands if command[:2] == ["docker", "stop"]], [
                ["docker", "stop", "--time", "30", "new-token-eye"],
                ["docker", "stop", "--time", "30", "new-agent-config"],
            ])
            self.assertEqual([command for command in commands if command[:2] == ["docker", "start"]], [
                ["docker", "start", "original-token"], ["docker", "start", "original-agent"],
            ])
            activation = events.index(("activate", "/nix/store/previous"))
            first_restart = next(index for index, event in enumerate(events)
                                 if event[0] == "run" and event[1][:2] == ["docker", "start"])
            self.assertLess(activation, first_restart)
            self.assertEqual(state["phase"], "rolled-back")
            self.assertFalse(new.exists())
            self.assertFalse(old.is_symlink())
            self.assertEqual((old / "sentinel").read_text(), "same data")


if __name__ == "__main__":
    unittest.main()
