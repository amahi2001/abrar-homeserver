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
        for name, root, client in ((alias.BOX, self.box, Path(alias.BOX)),
                                   (alias.HOST, self.host, self.legacy / ".claude")):
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
        self.key = hashlib.sha256(self.raw).hexdigest()
        self.backup = self.u.state / f"host-ca-home-rename.{self.key}.preimage"
        atomic(self.owner, self.raw)
        atomic(self.data / "routing-owner.json", b'{"applying": false}')

    def run_repair(self, mode, key=None):
        return alias.reconcile(mode, self.u, self.legacy, self.deployment,
                               None if mode == "check" else key or self.key)

    def test_only_two_fields_change_and_rollback_is_exact(self):
        settings = (self.host / "settings.json").read_bytes()
        self.assertEqual(self.run_repair("check"), f"eligible {self.key}")
        args = self.docker.call_args.args
        self.assertEqual(args[:8], ("docker", "exec", "--workdir", "/", "exact-container-id",
                                    "/usr/bin/python3", "-IS", "-c"))
        mapping = json.loads(args[-3])
        info = (self.host / alias.CA).stat()
        self.assertEqual(mapping[alias.HOST + "/" + alias.CA], [info.st_dev, info.st_ino])
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.run_repair("apply"), f"applied {self.key}")
        self.assertEqual(self.backup.read_bytes(), self.raw)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
        expected = json.loads(self.raw)
        entry = expected["accounts"][1]
        entry["account"]["clientRoot"] = str(self.host)
        entry["installed"][alias.KEY] = str(self.host / alias.CA)
        self.assertEqual(json.loads(self.owner.read_bytes()), expected)
        self.assertEqual(self.run_repair("apply"), f"current {self.key}")
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
        duplicate = self.raw.replace(b'"enabled": true', b'"enabled": false, "enabled": true')
        atomic(self.owner, duplicate)
        with self.assertRaises(ValueError):
            self.run_repair("apply")
        self.assertEqual(self.owner.read_bytes(), duplicate)

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
        declared = json.loads(env["TOKEN_EYE_CLAUDE_ACCOUNTS"])
        declared[0]["unexpected"] = True
        env["TOKEN_EYE_CLAUDE_ACCOUNTS"] = json.dumps(declared)
        result = subprocess.run([sys.executable, "-c", alias.BOX_CHECK, "{}", "check",
                                 json.dumps(scope)], env=env)
        self.assertEqual(result.returncode, 1)

    def test_pending_routing_and_receipt_drift_during_final_probe_defer(self):
        routing = self.data / "routing-owner.json"
        atomic(routing, b'{"applying": true}')
        with self.assertRaises(ValueError):
            self.run_repair("check")
        atomic(routing, b'{"applying": false}')
        self.docker.reset_mock()
        changed = self.raw + b" "
        def mutate_on_second_probe(*args, **kwargs):
            if self.docker.call_count == 2:
                atomic(self.owner, changed)
        self.docker.side_effect = mutate_on_second_probe
        with self.assertRaises(ValueError):
            self.run_repair("apply")
        self.assertEqual(self.owner.read_bytes(), changed)
        self.assertFalse(self.backup.exists())
        self.docker.side_effect = None
        key = hashlib.sha256(changed).hexdigest()
        with self.assertRaises(ValueError):
            self.run_repair("apply")
        self.assertEqual(self.run_repair("check"), f"eligible {key}")
        self.assertEqual(self.run_repair("apply", key), f"applied {key}")
        self.assertEqual((self.u.state / f"host-ca-home-rename.{key}.preimage").read_bytes(), changed)

    def test_invalid_or_ambiguous_settings_block_preview(self):
        path = self.host / "settings.json"
        raw = path.read_bytes()
        duplicate = raw.replace(b'"NODE_EXTRA_CA_CERTS":', b'"NODE_EXTRA_CA_CERTS":"other","NODE_EXTRA_CA_CERTS":')
        for settings in (duplicate, b'{"extra":NaN,' + raw[1:], b'{"extra":1e400,' + raw[1:],
                         raw.decode().encode("utf-16"), b"\xef\xbb\xbf" + raw,
                         b'{"extra":"\\udfff",' + raw[1:]):
            atomic(path, settings)
            with self.assertRaises(ValueError):
                self.run_repair("check")
        self.assertFalse(self.backup.exists())

    def test_native_scope_order_and_output_size_are_checked_before_eligibility(self):
        with patch.object(alias, "OWNER_LIMIT", len(self.raw)), self.assertRaises(ValueError):
            self.run_repair("check")
        record = json.loads(self.raw)
        record["accounts"].reverse()
        atomic(self.owner, json.dumps(record).encode())
        with self.assertRaises(ValueError):
            self.run_repair("check")

    def test_receipt_change_after_publishing_backup_preserves_both_and_allows_retry(self):
        changed = self.raw + b"\n"
        def change_during_backup(path, data, **kwargs):
            atomic(path, data, **kwargs)
            if path == self.backup:
                atomic(self.owner, changed)
        with patch.object(alias, "atomic", side_effect=change_during_backup), self.assertRaises(ValueError):
            self.run_repair("apply")
        self.assertEqual(self.owner.read_bytes(), changed)
        self.assertEqual(self.backup.read_bytes(), self.raw)
        key = hashlib.sha256(changed).hexdigest()
        self.assertEqual(self.run_repair("apply", key), f"applied {key}")
        self.assertEqual((self.u.state / f"host-ca-home-rename.{key}.preimage").read_bytes(), changed)
        self.assertEqual(self.run_repair("check"), f"current {key}")
        with self.assertRaises(ValueError):
            self.run_repair("rollback", self.key)
        self.assertEqual(self.run_repair("rollback", key), "restored")
        self.assertEqual(self.owner.read_bytes(), changed)

    def test_concurrent_preimage_is_never_replaced_or_removed(self):
        def create_during_staging(path, data, **kwargs):
            if path == self.backup:
                atomic(self.backup, b"another writer")
            atomic(path, data, **kwargs)
        with patch.object(alias, "atomic", side_effect=create_during_staging), self.assertRaises(FileExistsError):
            self.run_repair("apply")
        self.assertEqual(self.backup.read_bytes(), b"another writer")
        self.assertEqual(self.owner.read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
