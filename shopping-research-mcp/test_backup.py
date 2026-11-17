"""Only completed snapshots can become visible or displace a retained backup."""
import errno
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


def output_dir(args):
    mount = next(arg for arg in args if arg.endswith(",dst=/backups"))
    return Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/backups")) / args[-1].removeprefix("/backups/")


class SnapshotTests(unittest.TestCase):
    def test_failed_write_or_dead_client_keeps_previous_snapshots(self):
        for kind in ("daily", "pre-upgrade"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                (state / "data").mkdir()
                old = state / "backups" / kind / "20260101T000000Z"
                old.mkdir(parents=True)
                (old / "manifest.json").write_text("{}")
                # Neither an older unmarked format nor a link is ours to remove.
                legacy = old.with_name("20260102T000000Z")
                legacy.mkdir()
                link = old.with_name(".20260101T000001Z.incomplete")
                link.symlink_to(legacy, target_is_directory=True)
                now = datetime(2026, 11, 11, tzinfo=timezone.utc)
                final = old.with_name("20261111T000000Z")
                staging = old.with_name(".20261111T000000Z.incomplete")
                fail = True
                worker_exists = True

                def worker(args, **kwargs):
                    if "images" in args:
                        return SimpleNamespace(stdout="a" * 64)
                    if args[1] == "ps":
                        return SimpleNamespace(stdout="abc" if worker_exists else "")
                    out = output_dir(args)
                    self.assertEqual(out.parent, staging)
                    self.assertFalse(final.exists())
                    out.mkdir()
                    (out / "shopping.sqlite").write_text("shopping")
                    if fail:
                        raise OSError(errno.ENOSPC, "fixture disk full")
                    (out / "auth.sqlite").write_text("auth")
                    (out / "manifest.json").write_text("{}")

                with self.assertRaises(OSError):
                    snapshots.backup(kind, state=state, now=now, run=worker)
                self.assertFalse(final.exists())
                self.assertTrue(staging.is_dir())
                with self.assertRaisesRegex(RuntimeError, "worker still exists"):
                    snapshots.backup(kind, state=state, now=now, run=worker)
                self.assertTrue((staging / "snapshot/shopping.sqlite").is_file())
                worker_exists = fail = False
                snapshots.backup(kind, state=state, now=now, run=worker)
                self.assertEqual((final / "auth.sqlite").read_text(), "auth")
                self.assertFalse(staging.exists())
                self.assertTrue(old.exists())
                self.assertTrue(legacy.is_dir())
                self.assertTrue(link.is_symlink())


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
                if args[1] == "ps":
                    return SimpleNamespace(stdout="")
                if "images" in args:
                    return SimpleNamespace(stdout="a" * 64)
                # A failed weekly copy can fill the disk. Free it before new writes.
                self.assertFalse((weekly / ".20261111T000000Z.incomplete").exists())
                destination = output_dir(args)
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
