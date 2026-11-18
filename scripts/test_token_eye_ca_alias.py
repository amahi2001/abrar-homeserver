import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import token_eye_ca_alias as alias
from token_eye_update import Deferred, Updater, atomic, locked


class HomeAliasTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.home = base / "amahi"
        self.home.mkdir(mode=0o700)
        self.legacy = base / "buildfleet"
        self.legacy.symlink_to(self.home)
        self.deployment = base / "deployment.lock"
        self.u = Updater(self.home)
        self.u.box_ready = Mock(return_value="exact-container-id")
        self.docker = self.enterContext(patch.object(alias, "command"))
        self.data = self.u.project / "home/.local/share/com.amahi.tokeneye.desktop"
        self.owner = self.data / "claude-routing-owner.json"
        self.backup = self.u.state / "host-ca-home-rename.preimage"
        for d in (self.data, self.u.state):
            d.mkdir(mode=0o700, parents=True)
        self.proxy_lock = self.data / "headroom-proxy/.operations.lock"
        for path in (self.proxy_lock, self.data / "server-forward/.operations.lock",
                     self.data / ".operations.lock", self.u.state / ".lock", self.deployment,
                     self.u.project / "startup.lock", self.u.project / "home/.agent-session.lock"):
            path.parent.mkdir(exist_ok=True)
            path.touch(mode=0o600)
        self.host = self.home / ".claude"
        self.box = self.home / ".local/state/dev/accounts/claude"
        records = []
        for name, root, client in ((alias.HOST, self.host, self.legacy / ".claude"),
                                   (alias.BOX, self.box, Path(alias.BOX))):
            root.mkdir(mode=0o700, parents=True)
            atomic(root / alias.CA, b"fixture public CA")
            installed = {k: "fixture-"+k for k in alias.KEYS}
            installed[alias.KEY] = str(client / alias.CA)
            actual = installed.copy()
            if name == alias.HOST:
                actual[alias.KEY] = str(self.host / alias.CA)
            atomic(root / "settings.json", json.dumps({"env": actual}).encode())
            records.append({"account": {"root": name, "clientRoot": str(client)},
                            "installed": installed, "previous": {k: None for k in alias.KEYS},
                            "env_existed": False,
                            "certificate_hash": hashlib.sha256(b"fixture public CA").hexdigest()})
        self.raw = json.dumps({"version": 1, "enabled": True, "accounts": records}).encode()
        atomic(self.owner, self.raw)

    def run_repair(self, mode):
        return alias.reconcile(mode, self.u, self.legacy, self.deployment)

    def test_only_two_fields_change_and_rollback_is_exact(self):
        settings = (self.host / "settings.json").read_bytes()
        self.assertEqual(self.run_repair("check"), "eligible")
        args = self.docker.call_args.args
        self.assertEqual(args[:3], ("docker", "exec", "exact-container-id"))
        mapping = json.loads(args[-3])
        info = (self.host / alias.CA).stat()
        self.assertEqual(mapping[alias.HOST + "/" + alias.CA], [info.st_dev, info.st_ino])
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.run_repair("apply"), "applied")
        self.assertEqual(self.backup.read_bytes(), self.raw)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
        expected = json.loads(self.raw)
        entry = expected["accounts"][0]
        entry["account"]["clientRoot"] = str(self.host)
        entry["installed"][alias.KEY] = str(self.host / alias.CA)
        self.assertEqual(json.loads(self.owner.read_bytes()), expected)
        self.assertEqual(self.run_repair("apply"), "current")
        self.assertEqual(self.run_repair("rollback"), "restored")
        self.assertEqual(self.owner.read_bytes(), self.raw)
        self.assertEqual(self.run_repair("rollback"), "already_restored")
        self.assertEqual((self.host / "settings.json").read_bytes(), settings)

    def test_retargeted_alias_is_rejected_even_with_identical_certificates(self):
        other = self.home.parent / "unrelated"
        (other / ".claude").mkdir(parents=True)
        atomic(other / ".claude" / alias.CA, (self.host / alias.CA).read_bytes())
        self.legacy.unlink()
        self.legacy.symlink_to(other)
        with self.assertRaises(ValueError):
            self.run_repair("apply")
        self.assertEqual(self.owner.read_bytes(), self.raw)

    def test_changed_certificate_or_other_settings_block_repair(self):
        atomic(self.host / alias.CA, b"changed")
        with self.assertRaises(ValueError):
            self.run_repair("apply")
        atomic(self.host / alias.CA, b"fixture public CA")
        settings = json.loads((self.box / "settings.json").read_text())
        settings["env"]["HTTP_PROXY"] = "changed"
        atomic(self.box / "settings.json", json.dumps(settings).encode())
        with self.assertRaises(ValueError):
            self.run_repair("apply")
        self.assertFalse(self.backup.exists())

    def test_active_owner_or_unverified_sockets_defer_before_writing(self):
        with locked(self.proxy_lock), self.assertRaises(Deferred):
            self.run_repair("apply")
        self.docker.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            self.run_repair("apply")
        self.assertEqual(self.owner.read_bytes(), self.raw)
        self.assertFalse(self.backup.exists())

    def test_rollback_preserves_a_subsequently_changed_receipt(self):
        self.run_repair("apply")
        changed = json.loads(self.owner.read_text())
        changed["accounts"][0]["previous"]["HTTP_PROXY"] = "external change"
        raw = json.dumps(changed).encode()
        atomic(self.owner, raw)
        with self.assertRaises(ValueError):
            self.run_repair("rollback")
        self.assertEqual(self.owner.read_bytes(), raw)

    def test_unrecognized_receipt_is_preserved(self):
        changed = json.loads(self.raw)
        for update in ({"extra_field": True}, {"env_existed": None}):
            entry = changed["accounts"][0].copy()
            entry.update(update)
            if entry["env_existed"] is None:
                del entry["env_existed"]
            raw = json.dumps({**changed, "accounts": [entry, changed["accounts"][1]]}).encode()
            atomic(self.owner, raw)
            with self.assertRaises(ValueError):
                self.run_repair("apply")
            self.assertEqual(self.owner.read_bytes(), raw)

    def test_preview_blocks_conflicting_preimage_and_changed_mount_identity(self):
        atomic(self.backup, b"unrelated preimage")
        with self.assertRaises(ValueError):
            self.run_repair("check")
        info = self.host.stat()
        scope = {alias.HOST: str(self.host), alias.BOX: alias.BOX}
        env = {**os.environ, "TOKEN_EYE_DATA_DIR": alias.BOX_DATA,
               "TOKEN_EYE_CLAUDE_ACCOUNTS": json.dumps(
                   [{"root": k, "clientRoot": v} for k, v in scope.items()])}
        for inode, current, code in ((info.st_ino, scope, 0), (info.st_ino + 1, scope, 1),
                                     (info.st_ino, {alias.HOST: str(self.home)}, 1)):
            mapping = json.dumps({str(self.host): [info.st_dev, inode]})
            result = subprocess.run([sys.executable, "-c", alias.BOX_CHECK, mapping, "check",
                                     json.dumps(current)], env=env)
            self.assertEqual(result.returncode, code)


if __name__ == "__main__":
    unittest.main()
