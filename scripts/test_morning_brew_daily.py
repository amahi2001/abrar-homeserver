"""Regression checks for Morning Brew expiry; never call Jellyfin or delete media."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("morning_brew_daily", Path(__file__).with_name("morning_brew_daily.py"))
automation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(automation)


class ExpiryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name).resolve()
        self.video_id = "gqbJtZrl0To"
        self.path = self.library / f"Episode [{self.video_id}]" / "video.mkv"
        self.path.parent.mkdir()
        self.path.touch()
        self.job = {"status": "ready", "item_id": "abc123", "path": str(self.path)}
        self.item = {"Id": "abc123", "Path": str(self.path)}
        patcher = patch.object(automation, "LIBRARY", self.library)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_matching_item_dry_run_uses_collection_and_keeps_file(self):
        with patch.object(automation, "jellyfin", return_value={"Items": [self.item]}) as api:
            removed, message = automation.expire(self.video_id, {}, self.job, True)
        self.assertTrue(removed)
        self.assertIn("would delete expired", message)
        api.assert_called_once_with("GET", "/Items", {"Ids": "abc123", "Fields": "Path"})
        self.assertTrue(self.path.is_file())

    def test_verified_item_delete_removes_file(self):
        calls = []

        def api(method, path, params=None):
            calls.append((method, path, params))
            if method == "GET":
                return {"Items": [self.item]}
            self.assertEqual((method, path), ("DELETE", "/Items/abc123"))
            self.path.unlink()

        with patch.object(automation, "jellyfin", side_effect=api):
            removed, message = automation.expire(self.video_id, {}, self.job, False)
        self.assertTrue(removed)
        self.assertIn("deleted expired", message)
        self.assertEqual(calls, [("GET", "/Items", {"Ids": "abc123", "Fields": "Path"}),
                                 ("DELETE", "/Items/abc123", None)])
        self.assertFalse(self.path.exists())

    def test_missing_ambiguous_or_mismatched_item_never_deletes(self):
        cases = [None, {"Items": []}, {"Items": [self.item, self.item]},
                 {"Items": [{**self.item, "Id": "other"}]},
                 {"Items": [{**self.item, "Path": "/unexpected/video.mkv"}]}]
        for response in cases:
            with self.subTest(response=response), patch.object(automation, "jellyfin", return_value=response) as api:
                with self.assertRaisesRegex(RuntimeError, "refusing deletion"):
                    automation.expire(self.video_id, {}, self.job, False)
                self.assertEqual(api.call_count, 1)
                self.assertTrue(self.path.is_file())


if __name__ == "__main__":
    unittest.main()
