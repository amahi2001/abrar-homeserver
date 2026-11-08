"""Availability alerts stop for owned movies and retain stable release numbers."""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "media_queue", Path(__file__).resolve().parents[1] / "scripts/media_queue.py")
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.movies = self.root / "movies"
        self.movies.mkdir()
        self.addCleanup(mock.patch.stopall)
        for name, value in (("MOVIE_LIBRARY", self.movies),
                            ("AVAILABILITY_STATE", self.root / "availability"),
                            ("SEARCH_CACHE", self.root / "search.json")):
            mock.patch.object(queue, name, value).start()
        mock.patch.object(queue, "network_guardrails").start()
        self.torrents = mock.patch.object(queue, "transmission_torrents", return_value=[]).start()
        self.search = mock.patch.object(queue, "search_results", return_value=[]).start()

    def run_check(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            queue.check_availability("Buddy", 2026, 10)
        return output.getvalue()

    def release(self, title="Buddy.2026.1080p.WEBRip.x265-GROUP", **extra):
        return {"title": title, "indexer": "1337x", "size": 2 * 1024 ** 3,
                "seeders": 10, **extra}

    def test_completed_and_in_progress_downloads_are_silent_without_search(self):
        for left in (0, 1000):
            with self.subTest(left=left):
                self.torrents.return_value = [{"name": "Buddy (2026) [2160p] [YTS]", "left_until_done": left}]
                self.assertEqual(self.run_check(), "")
        self.search.assert_not_called()

    def test_library_copy_survives_removal_from_transmission(self):
        folder = self.movies / "Buddy (2026)"
        folder.mkdir()
        (folder / "Buddy.mkv").touch()
        self.assertEqual(self.run_check(), "")
        self.search.assert_not_called()

    def test_empty_library_folder_does_not_hide_missing_film(self):
        (self.movies / "Buddy (2026)").mkdir()
        self.run_check()
        self.search.assert_called_once_with("Buddy 2026", 10)

    def test_exact_title_and_year(self):
        for name in ("Buddy.1997.1080p", "Buddy Returns (2026)", "My Buddy (2026)"):
            with self.subTest(name=name):
                self.assertFalse(queue.movie_matches(name, "Buddy", 2026))
        self.assertTrue(queue.movie_matches("2001.A.Space.Odyssey.1968.1080p", "2001: A Space Odyssey", 1968))

    def test_new_results_keep_original_numbers_and_do_not_repeat_on_swarm_changes(self):
        self.search.return_value = [self.release("Buddy Returns 2026 2160p"), self.release(),
                                   self.release("Buddy 2026 2160p WEBRip-YTS", indexer="YTS")]
        output = self.run_check()
        self.assertIn("Recommended result: 3", output)
        self.assertIn("2. Buddy.", output)
        self.assertNotIn("1. Buddy", output)
        cache = json.loads(queue.SEARCH_CACHE.read_text())
        self.assertEqual(cache["results"], self.search.return_value)
        self.search.return_value[1]["seeders"] = 999
        self.assertEqual(self.run_check(), "")
        self.search.return_value.append(self.release("Buddy 2026 1440p WEB-DL-New"))
        self.assertIn("4. Buddy", self.run_check())

    def test_policy_rejects_cam_low_resolution_oversize_and_wrong_year(self):
        self.search.return_value = [self.release(name) for name in (
            "Buddy 2026 1080p HDCAM", "Buddy 2026 720p WEBRip", "Buddy 1997 2160p")]
        self.search.return_value.append(self.release("Buddy 2026 2160p", size=25 * 1024 ** 3))
        self.assertEqual(self.run_check(), "")
        self.assertFalse(queue.SEARCH_CACHE.exists())

    def test_download_queued_during_search_suppresses_alert(self):
        self.search.return_value = [self.release()]
        self.torrents.side_effect = [[], [{"name": "Buddy 2026 1080p"}]]
        self.assertEqual(self.run_check(), "")
        self.assertFalse(queue.SEARCH_CACHE.exists())

    def test_status_failure_does_not_search_or_claim_missing(self):
        self.torrents.side_effect = queue.QueueError("unavailable")
        with self.assertRaises(queue.QueueError):
            self.run_check()
        self.search.assert_not_called()

    def test_corrupt_history_does_not_repeat_alerts(self):
        self.search.return_value = [self.release()]
        self.run_check()
        next(queue.AVAILABILITY_STATE.glob("*.json")).write_text("broken")
        with self.assertRaisesRegex(queue.QueueError, "history"):
            self.run_check()


if __name__ == "__main__":
    unittest.main()
