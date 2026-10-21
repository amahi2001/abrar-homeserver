"""A completed import remains watched until Jellyfin indexes every media file."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "organizer", Path(__file__).resolve().parents[1] / "scripts/media_organizer.py"
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "manual" / "Show.S01"
        self.source.mkdir(parents=True)
        self.library = self.root / "tv"
        self.library.mkdir()
        for name in ("E01.mkv", "E02.mkv"):
            (self.source / name).touch()
            os.link(self.source / name, self.library / name)
        self.torrent = {"hash_string": "a" * 40, "left_until_done": 0}
        self.item = {"status": "organized", "name": "Show.S01"}
        self.state = {"items": {"a" * 40: self.item}}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(q, "source_path", return_value=self.source).start()
        mock.patch.object(q, "LIBRARY_ROOTS", (self.library,)).start()
        self.api_patch = mock.patch.object(q, "jellyfin_request", return_value={"Items": []})
        self.api = self.api_patch.start()
        self.events = mock.patch.object(q, "emit_event").start()
        self.clock = mock.patch.object(q.time, "time", return_value=100000).start()

    def tick(self, seconds=0):
        self.clock.return_value = 100000 + seconds
        q.verify_publications(self.state, [self.torrent])

    def indexed(self, names):
        self.api.return_value = {"Items": [{"Path": str(self.library / name)} for name in names]}

    def test_all_files_must_be_indexed_and_ready_alert_is_sent_once(self):
        self.indexed(["E01.mkv"])
        self.tick()
        self.assertNotIn("jellyfinVerifiedAt", self.item)
        self.events.assert_not_called()
        self.indexed(["E01.mkv", "E02.mkv"])
        self.tick(120)
        self.assertEqual(self.item["jellyfinVerifiedAt"], 100120)
        self.assertIn("Ready in Jellyfin", self.events.call_args.args[1])
        self.tick(240)
        self.assertEqual(self.events.call_count, 1)

    def test_delayed_indexing_refreshes_after_grace_and_alerts_then_recovers(self):
        self.tick()
        self.assertEqual(self.api.call_count, 1)
        self.tick(120)
        self.api.assert_called_with("/Library/Refresh", body={})
        self.tick(600)
        self.assertIn("publication needs attention", self.events.call_args.args[1])
        self.assertEqual(self.item["status"], "organized")
        self.tick(720)
        self.assertEqual(self.events.call_count, 1)
        self.tick(87000)
        self.assertEqual(self.events.call_count, 2)
        self.indexed(["E01.mkv", "E02.mkv"])
        self.tick(87120)
        self.assertIn("Ready in Jellyfin", self.events.call_args.args[1])

    def test_api_failure_never_claims_ready_and_is_retried(self):
        self.api.side_effect = q.OrganizerError("Jellyfin verification unavailable")
        self.tick()
        self.tick(600)
        self.assertNotIn("jellyfinVerifiedAt", self.item)
        self.assertIn("unavailable", self.events.call_args.args[1])
        self.api.side_effect = None
        self.indexed(["E01.mkv", "E02.mkv"])
        self.tick(720)
        self.assertIn("jellyfinVerifiedAt", self.item)

    def test_backfill_verifies_old_imports_quietly_and_new_imports_announce(self):
        self.indexed(["E01.mkv", "E02.mkv"])
        self.tick()
        self.events.assert_not_called()
        self.assertIn("jellyfinVerifiedAt", self.item)
        del self.item["jellyfinVerifiedAt"]
        del self.item["jellyfinPendingSince"]
        self.item["updatedAt"] = 100000
        self.tick()
        self.assertIn("Ready in Jellyfin", self.events.call_args.args[1])

    def test_organized_items_are_verified_without_reimporting(self):
        self.indexed(["E01.mkv", "E02.mkv"])
        with mock.patch.object(q, "load_state", return_value=self.state), \
                mock.patch.object(q, "transmission_torrents", return_value=[self.torrent]), \
                mock.patch.object(q, "published_inodes", return_value=set()), \
                mock.patch.object(q, "save_private_json"), \
                mock.patch.object(q, "organize_torrent") as organize:
            q.run()
            self.assertIn("jellyfinVerifiedAt", self.item)
            organize.assert_not_called()

    def test_refresh_failure_keeps_pending_import_for_next_pass(self):
        self.tick()
        self.api.side_effect = [{"Items": []}, q.OrganizerError("refresh unavailable")]
        self.tick(120)
        self.assertNotIn("jellyfinVerifiedAt", self.item)
        self.assertNotIn("jellyfinRefreshAt", self.state)
        self.api.side_effect = None
        self.tick(240)
        self.api.assert_called_with("/Library/Refresh", body={})

    def test_manual_download_path_in_index_cannot_count_as_published(self):
        self.api.return_value = {"Items": [{"Path": str(path)} for path in self.source.iterdir()]}
        self.tick()
        self.assertNotIn("jellyfinVerifiedAt", self.item)

    def test_indexed_different_copy_with_same_name_cannot_count(self):
        (self.library / "E02.mkv").unlink()
        (self.library / "E02.mkv").touch()
        self.indexed(["E01.mkv", "E02.mkv"])
        self.tick()
        self.assertNotIn("jellyfinVerifiedAt", self.item)

    def test_reviews_and_incomplete_or_removed_torrents_do_not_publish(self):
        self.item["status"] = "needs-review"
        self.tick()
        self.item["status"] = "organized"
        self.torrent["left_until_done"] = 1
        self.tick()
        q.verify_publications(self.state, [])
        self.api.assert_not_called()
        self.events.assert_not_called()

    def test_listing_paginates_and_rejects_malformed_response(self):
        self.api.side_effect = [{"Items": [{}] * 500}, {"Items": [{"Path": str(self.library / "E01.mkv")}]}]
        self.assertEqual(len(q.indexed_inodes()), 1)
        self.assertEqual(self.api.call_args.kwargs["query"]["StartIndex"], 500)
        self.api.side_effect = None
        self.api.return_value = {}
        with self.assertRaises(q.OrganizerError):
            q.indexed_inodes()

    def test_transport_keeps_credential_in_header_and_sanitizes_failures(self):
        config = self.root / "connection.json"
        key = self.root / "key"
        key.write_text("test-private-key")
        config.write_text(json.dumps({"jellyfinKeyFile": str(key), "jellyfinUrl": "http://127.0.0.1:8096"}))
        self.api_patch.stop()
        with mock.patch.object(q, "JELLYFIN_CONFIG", config), mock.patch.object(q, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"Items": []}'
            self.assertEqual(q.jellyfin_request("/Items"), {"Items": []})
            request = urlopen.call_args.args[0]
            self.assertNotIn("test-private-key", request.full_url)
            self.assertIn("test-private-key", request.headers.values())
            urlopen.side_effect = OSError("test-private-key")
            with self.assertRaises(q.OrganizerError) as caught:
                q.jellyfin_request("/Items")
            self.assertNotIn("test-private-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
