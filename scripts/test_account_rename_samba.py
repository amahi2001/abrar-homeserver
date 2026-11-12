import copy
import importlib.util
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "account_rename_samba", Path(__file__).with_name("account_rename_samba.py")
)
samba = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(samba)


class PassdbError(Exception):
    pass


class FakeDatabase:
    def __init__(self):
        self.accounts = {"buildfleet": SimpleNamespace(
            username="buildfleet", user_sid="S-1-5-21-1-1000",
            group_sid="S-1-5-21-1-513", acct_ctrl=16,
            nt_passwd=b"private-password-hash", lanman_passwd=None, pw_history=b"",
            pass_last_set_time=123, pass_can_change_time=123, pass_must_change_time=2147483647,
        )}
        self.renames = 0
        self.fail = False
        self.change_password = False

    def getsampwnam(self, name):
        if name not in self.accounts:
            raise PassdbError("No such user (3221225572)")
        return self.accounts[name]

    def rename_sam_account(self, account, name):
        self.renames += 1
        if self.fail:
            raise PassdbError("backend error with private data")
        replacement = copy.copy(account)
        replacement.username = name
        if self.change_password:
            replacement.nt_passwd = b"unexpected"
        del self.accounts[account.username]
        self.accounts[name] = replacement


class SambaRenameTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.open = patch.object(samba, "_open", return_value=(self.database, PassdbError))
        self.root = patch.object(samba, "_require_root")
        self.script = patch.object(samba, "_validate_script")
        self.open.start()
        self.root.start()
        self.script.start()
        self.addCleanup(self.open.stop)
        self.addCleanup(self.root.stop)
        self.addCleanup(self.script.stop)

    def rename(self, **kwargs):
        return samba.rename("/test/smb.conf", rename_script="/root/rename %uold %unew", **kwargs)

    def test_snapshot_never_contains_raw_password_hash(self):
        snapshot = samba.snapshot("/test/smb.conf")
        self.assertNotIn("private-password-hash", str(snapshot))
        self.assertEqual(snapshot["username"], "buildfleet")

    def test_rename_preserves_sid_password_and_flags(self):
        before = samba.snapshot("/test/smb.conf")
        after = self.rename(expected=before)
        self.assertEqual(after["username"], "amahi")
        self.assertTrue(samba.preserved(before, after))
        self.assertNotIn("buildfleet", self.database.accounts)

    def test_verified_retry_does_not_repeat_callback(self):
        before = samba.snapshot("/test/smb.conf")
        self.rename(expected=before)
        self.rename(expected=before)
        self.assertEqual(self.database.renames, 1)

    def test_unverified_retry_is_rejected(self):
        self.rename()
        with self.assertRaisesRegex(RuntimeError, "unverified partial"):
            self.rename()

    def test_destination_conflict_does_not_mutate(self):
        self.database.accounts["amahi"] = copy.copy(self.database.accounts["buildfleet"])
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.rename()
        self.assertEqual(self.database.renames, 0)

    def test_preexisting_credential_change_is_rejected(self):
        before = samba.snapshot("/test/smb.conf")
        self.database.accounts["buildfleet"].nt_passwd = b"changed"
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.rename(expected=before)
        self.assertEqual(self.database.renames, 0)

    def test_post_rename_credential_change_is_detected(self):
        self.database.change_password = True
        with self.assertRaisesRegex(RuntimeError, "password, SID or account flags changed"):
            self.rename()

    def test_reverse_rename_preserves_original_identity(self):
        before = samba.snapshot("/test/smb.conf")
        self.rename(expected=before)
        after = self.rename(old="amahi", new="buildfleet", expected=before)
        self.assertEqual(before, after)

    def test_failed_callback_reports_partial_failure_without_backend_details(self):
        self.database.fail = True
        with self.assertRaisesRegex(RuntimeError, "controller rollback") as caught:
            self.rename()
        self.assertNotIn("private data", str(caught.exception))
        self.assertIn("buildfleet", self.database.accounts)

    def test_backend_read_failure_is_not_treated_as_missing_user(self):
        with patch.object(self.database, "getsampwnam", side_effect=PassdbError("corrupt database")):
            with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                self.rename()

    def test_signed_ntstatus_missing_destination_is_accepted(self):
        original = self.database.getsampwnam

        def lookup(name):
            if name not in self.database.accounts:
                raise PassdbError("Unable to get user information for 'amahi', (-1073741724,The specified account does not exist.)")
            return original(name)

        with patch.object(self.database, "getsampwnam", side_effect=lookup):
            result = self.rename()
        self.assertEqual(result["username"], "amahi")

    def test_other_usernames_are_rejected(self):
        with self.assertRaises(ValueError):
            self.rename(new="root")
        with self.assertRaises(ValueError):
            samba.snapshot("/test/smb.conf", "root")

    def test_missing_invariant_is_not_accepted(self):
        self.assertFalse(samba.preserved({}, {}))

    def test_login_verification_hides_hash_and_removes_secret(self):
        self.database.accounts["amahi"] = self.database.accounts.pop("buildfleet")
        account = self.database.accounts["amahi"]
        account.username = "amahi"
        account.nt_passwd = b"x" * 16
        expected = samba._snapshot(account)
        with tempfile.TemporaryDirectory() as directory:
            real_stat = Path.stat

            def root_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                return SimpleNamespace(st_mode=result.st_mode, st_uid=0)

            calls = []

            def run(command, **kwargs):
                self.assertNotIn(account.nt_passwd.hex(), str(command))
                filename = command[command.index("-A") + 1]
                self.assertEqual(os.stat(filename).st_mode & 0o777, 0o600)
                text = Path(filename).read_text()
                self.assertIn("password = " + account.nt_passwd.hex(), text)
                calls.append(text.splitlines()[0])
                return SimpleNamespace(returncode=0)

            with patch.object(Path, "stat", root_stat), patch.object(samba.shutil, "which", return_value="/bin/smbclient"), patch.object(samba.subprocess, "run", side_effect=run):
                result = samba.verify_logins("/test/smb.conf", expected, directory)
            self.assertEqual(result, {"buildfleet": True, "amahi": True})
            self.assertEqual(calls, ["username = buildfleet", "username = amahi"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failed_login_still_removes_secret(self):
        self.database.accounts["amahi"] = self.database.accounts.pop("buildfleet")
        account = self.database.accounts["amahi"]
        account.username = "amahi"
        account.nt_passwd = b"x" * 16
        expected = samba._snapshot(account)
        with tempfile.TemporaryDirectory() as directory:
            real_stat = Path.stat

            def root_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                return SimpleNamespace(st_mode=result.st_mode, st_uid=0)

            with patch.object(Path, "stat", root_stat), patch.object(samba.shutil, "which", return_value="/bin/smbclient"), patch.object(samba.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
                with self.assertRaisesRegex(RuntimeError, "authentication verification failed"):
                    samba.verify_logins("/test/smb.conf", expected, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
