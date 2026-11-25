"""Exercise worker update recovery without touching the real plugins or database."""
from contextlib import closing
import fcntl
import io
import json
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("claude_mem_host", Path(__file__).with_name("claude-mem-host.py"))
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class WorkerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(host, DATA=self.root, RECEIPT=self.root / "receipt",
                                        CACHE=self.root / "cache", ACTIVE=self.root / "version",
                                        PENDING=self.root / "pending"))
        host.CACHE.mkdir()
        self.addCleanup(os.umask, os.umask(0o077))
        self.enterContext(patch.dict(os.environ, CREDENTIALS_DIRECTORY=str(self.root)))
        (self.root / "gateway-key").write_text("fixture")
        self.enterContext(patch.object(host, "worker_cached", return_value=True))
        self.complete = self.enterContext(patch.object(host, "complete", return_value=True))
        self.install = self.enterContext(patch.object(host, "install"))
        host.RECEIPT.write_text("1.0.0")
        (self.root / "telemetry.json").write_text('{"enabled":false}')

    def stage(self, release):
        promotion = {"schemaVersion": 1, "promotedVersion": release}
        with patch.object(host, "urlopen", return_value=io.BytesIO(json.dumps(promotion).encode())):
            return host.main("stage")

    def test_failed_install_keeps_previous_boot_release(self):
        self.install.side_effect = OSError("registry unavailable")
        with self.assertRaises(OSError):
            self.stage("1.0.2")
        self.complete.return_value = False  # Installer already modified the client registry.
        host.main("prepare")
        self.assertEqual((self.root / "version").read_text(), "1.0.0")
        self.assertFalse(host.PENDING.exists())

    def test_retry_and_superseding_candidates_preserve_pre_migration_data(self):
        with closing(sqlite3.connect(self.root / "claude-mem.db")) as db:
            db.execute("CREATE TABLE events(id INTEGER)")
            self.stage("1.0.2")
            db.execute("INSERT INTO events VALUES (1)")
            db.commit()  # Last event from the old worker, after staging.
            host.main("prepare")
            db.execute("INSERT INTO events VALUES (2)")
            db.commit()  # New worker used the database, but failed readiness.
        self.assertEqual(self.stage("1.0.1"), 1)
        self.assertEqual(host.read_json(host.PENDING)["version"], "1.0.2")
        self.assertEqual(self.stage("1.0.2"), 1)
        host.main("prepare")
        self.stage("1.0.3")
        host.main("prepare")
        self.assertEqual(self.install.call_count, 2)
        with closing(sqlite3.connect(self.root / "nixos-pre-upgrade.db")) as backup:
            self.assertEqual(backup.execute("SELECT id FROM events").fetchall(), [(1,)])
        with patch.object(host, "worker_ready", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                host.main("ready")
        self.assertEqual(host.RECEIPT.read_text(), "1.0.0")
        self.assertEqual(host.read_json(host.PENDING)["version"], "1.0.3")

    def test_readiness_runs_unlocked_and_keeps_concurrently_staged_candidate(self):
        self.stage("1.0.1")
        host.main("prepare")

        def check(release):
            self.assertEqual(release, "1.0.1")
            with (self.root / "nixos-install.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.stage("1.0.2")

        with patch.object(host, "worker_ready", side_effect=check):
            host.main("ready")
        self.assertEqual(host.RECEIPT.read_text().strip(), "1.0.1")
        self.assertEqual(host.read_json(host.PENDING)["version"], "1.0.2")
        with patch.object(host, "snapshot_database") as snapshot:
            host.main("prepare")
            snapshot.assert_called_once_with()
        with patch.object(host, "worker_ready"):
            host.main("ready")
        self.assertEqual(host.RECEIPT.read_text().strip(), "1.0.2")
        self.assertFalse(host.PENDING.exists())

    def test_relocated_database_cannot_be_migrated_without_a_snapshot(self):
        self.stage("1.0.1")
        (self.root / "settings.json").write_text(json.dumps({"CLAUDE_MEM_DATA_DIR": str(self.root / "elsewhere")}))
        with self.assertRaisesRegex(ValueError, "different data directory"):
            host.main("prepare")
        self.assertIsNone(host.read_json(host.PENDING)["snapshot_from"])
        with self.assertRaisesRegex(ValueError, "different data directory"):
            self.stage("1.0.2")
        self.assertEqual(self.install.call_count, 1)

    def test_failed_client_repair_recovers_cached_worker_offline(self):
        cache = host.CACHE / "1.0.0"
        cache.mkdir()
        (cache / "fixture").write_text("original worker")
        self.complete.return_value = False
        self.assertEqual(self.stage("0.9.9"), 0)

        def fail_install(release):
            cache.mkdir()
            (cache / "fixture").write_text("partial")
            raise OSError("network failure")

        self.install.side_effect = fail_install
        with self.assertRaises(OSError):
            host.main("prepare")
        host.main("prepare")
        self.assertEqual((cache / "fixture").read_text(), "original worker")
        self.assertEqual(host.ACTIVE.read_text(), "1.0.0")
        self.install.assert_called_once_with("1.0.0")
