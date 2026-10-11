"""Review alerts survive polling failures and approved imports are verified."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


organizer = load_script("media_organizer")
queue = load_script("media_queue")
library = load_script("media_library")


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manual = self.root / "manual"
        self.source = self.manual / "The Middle (2009) Season 1 S01"
        self.source.mkdir(parents=True)
        self.episode = self.source / "The.Middle.S01E01.mkv"
        self.episode.touch()
        self.tv = self.root / "tv"
        self.tv.mkdir()
        self.events = self.root / "events"
        self.state_file = self.root / "organizer-state.json"
        self.key = "a" * 40
        self.torrent = {
            "hash_string": self.key, "name": self.source.name,
            "id": 7, "left_until_done": 0,
        }
        self.blocker = "Could not identify exactly one tv match. Candidates: The Middle (2009)."
        self.addCleanup(mock.patch.stopall)
        for module, name, value in (
            (organizer, "STATE_FILE", self.state_file),
            (organizer, "EVENT_DIR", self.events),
            (organizer, "LIBRARY_ROOTS", (self.tv,)),
            (organizer, "MANUAL_ROOT", self.manual),
            (queue, "ORGANIZER_EVENT_DIR", self.events),
            (queue, "WATCH_STATE", self.root / "watch-state.json"),
            (library, "MANUAL_ROOT", self.manual),
        ):
            mock.patch.object(module, name, value).start()
        mock.patch.object(organizer, "transmission_torrents", return_value=[self.torrent]).start()
        mock.patch.object(organizer, "source_path", return_value=self.source).start()
        self.import_torrent = mock.patch.object(
            organizer, "organize_torrent", side_effect=organizer.OrganizerError(self.blocker)
        ).start()
        mock.patch.object(queue, "network_guardrails").start()
        mock.patch.object(queue, "transmission_torrents", return_value=[]).start()

    def event_messages(self):
        return [json.loads(path.read_text())["message"] for path in self.events.glob("*.json")]

    def test_review_event_names_blocker_and_conversation_handoff(self):
        organizer.run()
        messages = self.event_messages()
        self.assertEqual(len(messages), 1)
        message = messages[0]
        for expected in (self.blocker, self.key, "title", "year", "new message", "dry-run", "approval"):
            self.assertIn(expected, message)
        self.assertIn("not been verified", message)
        self.assertIn("no-agent", message)
        self.assertNotIn("reply ‘import’", message)
        item = organizer.load_state()["items"][self.key]
        self.assertEqual(item["status"], "needs-review")
        self.assertEqual(item["sourcePath"], str(self.source))
        self.assertIn(self.key, self.capture(organizer.show_status))

    def capture(self, function, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function(*args)
        return output.getvalue()

    def test_pending_review_is_not_imported_and_gets_daily_reminder(self):
        organizer.run()
        self.capture(queue.watch_downloads)
        self.import_torrent.reset_mock()
        organizer.run()
        self.import_torrent.assert_not_called()
        self.assertEqual(self.event_messages(), [])
        state = organizer.load_state()
        state["items"][self.key]["reviewNotifiedAt"] = 0
        organizer.save_private_json(self.state_file, state)
        organizer.run()
        self.assertEqual(len(self.event_messages()), 1)
        self.import_torrent.assert_not_called()

    def test_legacy_review_gets_actionable_alert_without_retrying_import(self):
        organizer.save_private_json(self.state_file, {"items": {self.key: {
            "status": "needs-review", "name": self.source.name, "detail": self.blocker,
        }}})
        organizer.run()
        self.assertEqual(len(self.event_messages()), 1)
        self.import_torrent.assert_not_called()

    def test_success_output_alone_cannot_claim_verified_import(self):
        self.import_torrent.side_effect = None
        self.import_torrent.return_value = "Imported 1 file"
        organizer.run()
        self.assertNotEqual(organizer.load_state()["items"][self.key]["status"], "organized")
        self.assertIn("verification", self.event_messages()[0].lower())
        self.assertNotIn("Media organized for Jellyfin", self.event_messages()[0])

    def test_review_inspection_prints_exact_source_without_importing(self):
        organizer.run()
        output = self.capture(organizer.show_review, self.key)
        for expected in (self.key, str(self.source), self.blocker, "--dry-run", "--year 2009"):
            self.assertIn(expected, output)
        self.assertNotIn("--confirm", output)

    def test_dry_run_does_not_create_review_state_or_events(self):
        self.capture(organizer.run, True)
        self.assertFalse(self.state_file.exists())
        self.assertFalse(self.events.exists())
        self.import_torrent.assert_not_called()

    def test_dry_run_of_resolved_review_does_not_emit_resolution_or_save_state(self):
        organizer.run()
        self.capture(queue.watch_downloads)
        before = self.state_file.read_text()
        os.link(self.episode, self.tv / self.episode.name)
        self.capture(organizer.run, True)
        self.assertEqual(self.event_messages(), [])
        self.assertEqual(self.state_file.read_text(), before)

    def test_resolved_review_inspection_does_not_propose_another_import(self):
        organizer.run()
        os.link(self.episode, self.tv / self.episode.name)
        organizer.run()
        with self.assertRaisesRegex(organizer.OrganizerError, "No pending"):
            organizer.show_review(self.key)

    def test_rejected_import_requires_explicit_replacement_review(self):
        self.import_torrent.side_effect = organizer.OrganizerError(
            "No accepted media files were found: Not an upgrade for existing episode file"
        )
        organizer.run()
        self.assertEqual(organizer.load_state()["items"][self.key]["status"], "needs-review")
        message = self.event_messages()[0]
        self.assertIn("Not an upgrade", message)
        self.assertIn("kept", message)
        self.assertIn("explicit replacement", message)
        self.import_torrent.reset_mock()
        organizer.run()
        self.import_torrent.assert_not_called()

    def test_unrelated_rejection_asks_for_inspection_instead_of_replacement(self):
        self.import_torrent.side_effect = organizer.OrganizerError(
            "No accepted media files were found: Unknown series"
        )
        organizer.run()
        message = self.event_messages()[0]
        self.assertIn("Unknown series", message)
        self.assertIn("inspect the rejected files", message)
        self.assertNotIn("explicit replacement", message)

    def test_review_inspection_requires_matching_hash_not_reused_transmission_id(self):
        organizer.run()
        self.torrent["hash_string"] = "b" * 40
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(organizer.OrganizerError, "no longer completed"):
                organizer.show_review(self.key)

    def test_failed_stdout_flush_keeps_event_for_next_poll(self):
        organizer.run()
        output = io.StringIO()
        with mock.patch.object(output, "flush", side_effect=BrokenPipeError):
            with contextlib.redirect_stdout(output):
                with self.assertRaises(BrokenPipeError):
                    queue.watch_downloads()
        self.assertEqual(len(self.event_messages()), 1)

    def test_review_cli_accepts_exact_hash(self):
        with mock.patch.object(sys, "argv", ["media-organizer", "review", self.key]):
            args = organizer.parse_args()
        self.assertEqual((args.command, args.hash), ("review", self.key))

    def test_watcher_delivers_event_when_transmission_poll_fails(self):
        organizer.run()
        mock.patch.object(queue, "transmission_torrents", side_effect=queue.QueueError("RPC unavailable")).start()
        output = self.capture(queue.watch_downloads)
        self.assertIn(self.blocker, output)
        self.assertIn("RPC unavailable", output)
        self.assertEqual(self.event_messages(), [])
        self.assertEqual(self.capture(queue.watch_downloads), "\n")

    def test_watcher_delivers_event_when_guardrail_fails(self):
        organizer.run()
        mock.patch.object(queue, "network_guardrails", side_effect=queue.QueueError("VPN unavailable")).start()
        output = self.capture(queue.watch_downloads)
        self.assertIn(self.blocker, output)
        self.assertIn("VPN unavailable", output)
        self.assertEqual(self.event_messages(), [])

    def test_failed_stdout_keeps_event_for_next_poll(self):
        organizer.run()
        with mock.patch("builtins.print", side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                queue.watch_downloads()
        self.assertEqual(len(self.event_messages()), 1)
        self.assertIn(self.blocker, self.capture(queue.watch_downloads))

    def test_malformed_event_is_reported_and_preserved(self):
        self.events.mkdir()
        event = self.events / "broken.json"
        event.write_text("[]")
        self.assertIn("broken.json", self.capture(queue.watch_downloads))
        self.assertTrue(event.exists())

    def test_approved_tv_import_then_timer_verifies_and_announces_resolution(self):
        organizer.run()
        self.capture(queue.watch_downloads)
        self.import_torrent.reset_mock()
        record = {"id": 10, "path": str(self.tv)}
        args = SimpleNamespace(kind="tv", path=str(self.source), title="The Middle", year=2009,
                               dry_run=True, confirm=False, replace_existing=False)
        mock.patch.object(library, "require_active").start()
        mock.patch.object(library, "matching_lookup", return_value={"title": "The Middle (2009)", "year": 2009}).start()
        mock.patch.object(library, "existing_record", return_value=record).start()
        mock.patch.object(library, "scan_source", side_effect=lambda *a: [{
            "path": str(self.episode), "series": {"id": 10}, "episodes": [{"id": 100}], "rejections": [],
        }]).start()
        api = mock.patch.object(library, "api").start()
        self.assertIn("scan matches record", self.capture(library.organize, args))
        api.assert_not_called()
        args.dry_run = False
        with self.assertRaisesRegex(library.LibraryError, "requires --confirm"):
            library.organize(args)
        api.assert_not_called()
        args.confirm = True

        def import_api(manager, path, method="GET", body=None, **kwargs):
            self.assertEqual((manager, path, method), ("tv", "/command", "POST"))
            self.assertEqual(body["importMode"], "copy")
            self.assertFalse(body["replaceExistingFiles"])
            os.link(self.episode, self.tv / self.episode.name)
            return {"id": 1}

        api.side_effect = import_api
        mock.patch.object(library, "wait_for_command").start()
        self.assertIn("verified hard links", self.capture(library.organize, args))
        organizer.run()
        self.import_torrent.assert_not_called()
        self.assertEqual(organizer.load_state()["items"][self.key]["status"], "organized")
        self.assertIn("Verified", self.capture(queue.watch_downloads))


class CanonicalTvLookupTests(unittest.TestCase):
    def lookup(self, candidates, title="The Middle", year=2009, kind="tv"):
        with mock.patch.object(library, "api", return_value=candidates):
            return library.matching_lookup(kind, title, year)

    def test_explicit_year_accepts_sonarr_year_suffix(self):
        candidate = {"title": "The Middle (2009)", "year": 2009, "tvdbId": 95021}
        self.assertEqual(self.lookup([candidate]), candidate)
        candidate = {"title": "INVINCIBLE (2021)", "year": 2021, "tvdbId": 368207}
        self.assertEqual(self.lookup([candidate], "Invincible", 2021), candidate)

    def test_year_mismatch_and_duplicate_identity_still_require_review(self):
        for candidates in ([{"title": "The Middle (2009)", "year": 2010}],
                           [{"title": "The Middle", "year": 2009}, {"title": "The Middle (2009)", "year": 2009}]):
            with self.subTest(candidates=candidates):
                with self.assertRaisesRegex(library.LibraryError, "exactly one"):
                    self.lookup(candidates)

    def test_without_year_does_not_relax_exact_title_matching(self):
        candidate = {"title": "One Punch Man", "year": 2015}
        self.assertEqual(self.lookup([candidate], "One Punch Man", None), candidate)
        with self.assertRaises(library.LibraryError):
            self.lookup([{"title": "The Middle (2009)", "year": 2009}], year=None)

    def test_movie_exact_matching_is_unchanged(self):
        candidate = {"title": "Obsession", "year": 2026}
        self.assertEqual(self.lookup([candidate], "Obsession", 2026, "movie"), candidate)
        with self.assertRaises(library.LibraryError):
            self.lookup([{"title": "Obsession (2026)", "year": 2026}], "Obsession", 2026, "movie")


if __name__ == "__main__":
    unittest.main()
