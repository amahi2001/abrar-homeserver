"""Snapshot copy failures never publish a completed weekly backup."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("backup", Path(__file__).with_name("backup.py"))
snapshots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshots)


class WeeklyBackupTests(unittest.TestCase):
    def test_failed_copy_is_not_published_or_counted_for_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "data").mkdir()
            weekly = state / "backups/weekly"
            weekly.mkdir(parents=True)
            previous = []
            for index in range(4):
                path = weekly / f"202610{index + 1:02d}T000000Z"
                path.mkdir()
                (path / "manifest.json").write_text("{}")
                (path / "week").write_text(f"old-{index}")
                previous.append(path)

            def fake_cli(args, **kwargs):
                if "images" in args:
                    return SimpleNamespace(stdout="a" * 64)
                destination = state / "backups" / args[-1].removeprefix("/backups/")
                destination.mkdir()
                for name in ("shopping.sqlite", "auth.sqlite"):
                    (destination / name).write_bytes(b"synthetic database")
                (destination / "manifest.json").write_text("{}")

            copytree = shutil.copytree
            def fail_auth(source, destination):
                if Path(source).name == "auth.sqlite":
                    raise OSError("synthetic per-file I/O failure")
                return shutil.copy2(source, destination)
            def partial_copy(source, destination):
                return copytree(source, destination, copy_function=fail_auth)

            now = datetime(2026, 11, 11, tzinfo=timezone.utc)
            with patch.object(snapshots.os, "chown"), patch.object(snapshots.shutil, "copytree", side_effect=partial_copy):
                with self.assertRaises(shutil.Error):
                    snapshots.backup("daily", state=state, now=now, run=fake_cli)
            published = [path for path in weekly.iterdir() if snapshots.STAMP.fullmatch(path.name)]
            self.assertEqual(set(published), set(previous))
            self.assertTrue(all(path.is_dir() for path in previous))

            # A crash after writing staging metadata must not suppress next day's retry.
            staging = weekly / ".20261111T000000Z.incomplete"
            (staging / "week").write_text("2026-W46")
            unrelated = weekly / ".notes.incomplete"
            unrelated.mkdir()
            external = state / "external"
            external.mkdir()
            link = weekly / ".20261109T000000Z.incomplete"
            link.symlink_to(external, target_is_directory=True)

            # Old incomplete copies must not displace a healthy snapshot either.
            legacy = weekly / "20261110T000000Z"
            legacy.mkdir()
            (legacy / "manifest.json").write_text("{}")
            with patch.object(snapshots.os, "chown"):
                snapshots.backup("daily", state=state, now=now + timedelta(days=1), run=fake_cli)
            complete = [path for path in weekly.iterdir() if snapshots.STAMP.fullmatch(path.name) and (path / "week").is_file()]
            self.assertEqual(len(complete), 4)
            self.assertTrue(previous[1].is_dir())
            self.assertFalse(staging.exists())
            self.assertTrue(unrelated.is_dir())
            self.assertTrue(link.is_symlink())
            self.assertTrue(external.is_dir())
            latest = weekly / "20261112T000000Z"
            self.assertEqual((latest / "auth.sqlite").read_bytes(), b"synthetic database")
            self.assertEqual((latest / "week").read_text(), "2026-W46")


if __name__ == "__main__":
    unittest.main()
