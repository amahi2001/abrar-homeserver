"""Observable queue, publication, recovery, and notification behavior."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "youtube_queue", Path(__file__).resolve().parents[1] / "scripts/youtube_queue.py")
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)

FIRST, SECOND = "BaW_jenozKc", "abcdefghijk"


class QueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scope = "telegram:owner:"
        for attribute, path in (("STATE", "state"), ("STAGING", "staging"), ("LIBRARY", "library")):
            directory = self.root / path
            directory.mkdir()
            patch = mock.patch.object(q, attribute, directory)
            patch.start()
            self.addCleanup(patch.stop)
        config = {"ytDlp": "/test/yt-dlp",
                  "reserveBytes": q.RESERVE, "publicJellyfinUrl": "https://jellyfin.example.test",
                  "jellyfinUrl": "http://localhost:8096", "jellyfinKeyFile": "/test/key"}
        patches = [mock.patch.object(q, "settings", return_value=config),
                   mock.patch.object(q.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
                   mock.patch.object(q, "run", return_value="")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def item(self, video_id=FIRST, **extra):
        return {"id": video_id, "title": "A & B <video>", "duration": 20,
                "channel": "Test channel", "description": "A <description>",
                "upload_date": "20261020", **extra}

    def enqueue(self, video_id=FIRST):
        return q.queue(self.scope, url=q.video_url(video_id))

    def job(self, video_id=FIRST):
        with q.database() as db:
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (video_id,)).fetchone())

    def capture(self, function, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function(*args)
        return output.getvalue()

    def test_normalizes_supported_video_links_and_strips_playlist(self):
        for url in (f"https://youtu.be/{FIRST}?si=tracking", f"https://www.youtube.com/shorts/{FIRST}",
                    f"https://m.youtube.com/watch?v={FIRST}&list=PLabcdefghij", f"https://youtube.com/live/{FIRST}"):
            self.assertEqual(q.normalize_url(url), ("video", q.video_url(FIRST)))

    def test_rejects_untrusted_urls_channels_invalid_ids_and_ports(self):
        for url in (f"https://youtube.com.evil.test/watch?v={FIRST}", "file:///etc/passwd",
                    "https://youtube.com/@channel", "https://youtube.com/watch?v=bad",
                    f"https://name:password@youtube.com/watch?v={FIRST}",
                    f"https://youtube.com:invalid/watch?v={FIRST}"):
            with self.subTest(url=url), self.assertRaises(q.QueueError):
                q.normalize_url(url)

    def test_scope_is_required_before_any_job_is_inserted(self):
        with self.assertRaises(q.QueueError):
            q.queue(None, url=q.video_url(FIRST))
        self.assertFalse((q.STATE / "queue.db").exists())

    def test_search_selection_uses_saved_ids_without_searching_again(self):
        saved = q.snapshot(self.scope, "search", "query", [self.item(), self.item(SECOND)])
        with mock.patch.object(q, "extract", side_effect=AssertionError("must not search again")):
            result = q.queue(self.scope, snapshot_id=saved["snapshot"], indices=[2])
        self.assertEqual(result["jobs"][0]["id"], SECOND)
        self.assertEqual(len(q.show_status()), 1)

    def test_cross_conversation_and_expired_selections_are_rejected(self):
        saved = q.snapshot(self.scope, "search", "query", [self.item()])
        with self.assertRaises(q.QueueError):
            q.queue("another-chat", snapshot_id=saved["snapshot"], indices=[1])
        with q.database() as db:
            db.execute("UPDATE snapshots SET created=?", (time.time() - 86401,))
        with self.assertRaises(q.QueueError):
            q.queue(self.scope, snapshot_id=saved["snapshot"], indices=[1])
        self.assertEqual(q.show_status(), [])

    def test_playlist_requires_confirmation_and_keeps_previewed_membership(self):
        saved = q.snapshot(self.scope, "playlist", "Playlist", [self.item(), self.item(SECOND), None])
        with self.assertRaises(q.QueueError):
            q.queue(self.scope, snapshot_id=saved["snapshot"])
        with self.assertRaises(q.QueueError):
            q.queue(self.scope, url="https://youtube.com/playlist?list=PLabcdefghijk")
        result = q.queue(self.scope, snapshot_id=saved["snapshot"], confirm=True)
        self.assertEqual([job["id"] for job in result["jobs"]], [FIRST, SECOND])
        with q.database() as db:
            request = db.execute("SELECT * FROM requests WHERE id=?", (result["request"],)).fetchone()
        self.assertEqual(request["skipped"], 1)

    def test_playlist_preview_reports_unknown_durations(self):
        with mock.patch.object(q, "extract", return_value={"title": "Playlist", "entries": [self.item(), self.item(SECOND, duration=None), None]}):
            result = q.preview("https://youtube.com/playlist?list=PLabcdefghijk", self.scope)
        self.assertEqual(result["video_count"], 3)
        self.assertEqual(result["known_duration_seconds"], 20)
        self.assertEqual(result["unknown_durations"], 2)

    def test_duplicate_requests_reuse_job_and_existing_quality(self):
        self.enqueue()
        q.queue(self.scope, url=q.video_url(FIRST), height=2160)
        self.assertEqual(len(q.show_status()), 1)
        self.assertEqual(self.job()["height"], 1080)

    def test_status_filters_by_conversation(self):
        self.enqueue()
        q.queue("another-chat", url=q.video_url(SECOND))
        self.assertEqual([item["id"] for item in q.show_status(self.scope)], [FIRST])

    def test_native_runner_drops_privileges_and_filters_gateway_secrets(self):
        process = mock.Mock(pid=12345, returncode=0)
        process.poll.return_value = 0
        def launch(_command, **kwargs):
            kwargs["stdout"].write("2026.08.19\n")
            return process
        with mock.patch.object(q.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=988, pw_gid=987)), \
             mock.patch.object(q.os, "chown"), mock.patch.object(q.os, "killpg"), \
             mock.patch.object(q.subprocess, "Popen", side_effect=launch) as start, \
             mock.patch.dict(q.os.environ, {"GATEWAY_SECRET": "must-not-inherit"}):
            self.assertEqual(q.ytdlp(["--version"]).strip(), "2026.08.19")
        self.assertEqual(start.call_args.args[0][0], "/test/yt-dlp")
        options = start.call_args.kwargs
        self.assertEqual((options["user"], options["group"], options["extra_groups"]), (988, 987, []))
        self.assertTrue(options["start_new_session"])
        self.assertNotIn("GATEWAY_SECRET", options["env"])
        self.assertEqual(options["cwd"], options["env"]["HOME"])

    def test_native_runner_timeout_kills_downloader_process_group(self):
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        with mock.patch.object(q.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=988, pw_gid=987)), \
             mock.patch.object(q.os, "chown"), mock.patch.object(q.os, "killpg") as terminate, \
             mock.patch.object(q.subprocess, "Popen", return_value=process), \
             mock.patch.object(q.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(q.QueueError, "timed out"):
                q.ytdlp(["--", q.video_url(FIRST)], timeout=1)
        terminate.assert_called_once_with(process.pid, q.signal.SIGKILL)
        process.wait.assert_called_once()

    def test_native_runner_disk_pause_kills_process_group_and_preserves_partial(self):
        stage = q.STAGING / FIRST
        stage.mkdir()
        partial = stage / "video.part"
        partial.write_bytes(b"partial video")
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        with mock.patch.object(q.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=988, pw_gid=987)), \
             mock.patch.object(q.os, "chown"), mock.patch.object(q.os, "killpg") as terminate, \
             mock.patch.object(q.subprocess, "Popen", return_value=process), \
             mock.patch.object(q.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            with self.assertRaises(q.Paused):
                q.ytdlp(["--", q.video_url(FIRST)], stage=stage)
        terminate.assert_called_once_with(process.pid, q.signal.SIGKILL)
        self.assertEqual(partial.read_bytes(), b"partial video")

    def test_metadata_escapes_xml_keeps_source_and_removes_signed_json(self):
        stage = q.STAGING / FIRST
        stage.mkdir()
        (stage / "video.jpg").write_bytes(b"thumbnail")
        (stage / "video.info.json").write_text('{"url":"signed secret"}')
        q.metadata_files(stage, self.item())
        root = ET.parse(stage / "movie.nfo").getroot()
        self.assertEqual(root.findtext("title"), "A & B <video>")
        self.assertIn(q.video_url(FIRST), root.findtext("plot"))
        self.assertEqual(root.findtext("uniqueid"), FIRST)
        self.assertEqual(root.findtext("premiered"), "2026-10-20")
        self.assertTrue((stage / "poster.jpg").exists())
        self.assertFalse((stage / "video.info.json").exists())

    def test_unicode_filenames_remain_within_filesystem_limits(self):
        name = q.safe_name("../" + "🎥" * 1000 + ":bad?", FIRST)
        self.assertLess(len(name.encode()), 255)
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith(f"[{FIRST}]"))

    def download_patches(self, valid=True, live=False):
        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        def fake_ytdlp(args, stage=None, **_kwargs):
            (stage / "video.mkv").write_bytes(b"completed-media")
            (stage / "video.info.json").write_text("{}")
            (stage / "old-format.webm.part").write_bytes(b"old fragments")
            return ""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(q, "extract", return_value=self.item(is_live=live)))
        stack.enter_context(mock.patch.object(q.pwd, "getpwnam", return_value=account))
        # Ownership changes are a host integration boundary, unavailable to Nix build users.
        stack.enter_context(mock.patch.object(q.os, "chown"))
        stack.enter_context(mock.patch.object(q.os, "chmod"))
        stack.enter_context(mock.patch.object(q, "ytdlp", side_effect=fake_ytdlp))
        stack.enter_context(mock.patch.object(q, "verify_media", return_value=valid))
        stack.enter_context(mock.patch.object(q, "jellyfin", return_value=None))
        stack.enter_context(mock.patch.object(q.shutil, "disk_usage", return_value=SimpleNamespace(free=q.RESERVE * 2)))
        return stack

    def test_only_validated_completed_media_is_published(self):
        self.enqueue()
        with self.download_patches():
            q.download(self.job())
        job = self.job()
        self.assertEqual(job["status"], "awaiting-index")
        self.assertTrue(Path(job["path"]).is_file())
        self.assertFalse((q.STAGING / FIRST).exists())
        self.assertFalse(list(q.LIBRARY.rglob("*.info.json")))
        self.assertFalse(list(q.LIBRARY.rglob("*.part")))

    def test_validation_failure_and_livestream_never_publish(self):
        self.enqueue()
        with self.download_patches(valid=False), self.assertRaises(q.QueueError):
            q.download(self.job())
        self.assertEqual(list(q.LIBRARY.iterdir()), [])
        with self.download_patches(live=True), self.assertRaises(q.QueueError):
            q.download(self.job())
        self.assertEqual(list(q.LIBRARY.iterdir()), [])

    def test_existing_published_folder_is_never_overwritten(self):
        self.enqueue()
        final = q.LIBRARY / q.safe_name(self.item()["title"], FIRST)
        final.mkdir()
        (final / "video.mkv").write_bytes(b"original")
        with self.download_patches(), self.assertRaises(q.QueueError):
            q.download(self.job())
        self.assertEqual((final / "video.mkv").read_bytes(), b"original")

    def test_indexing_requires_exact_published_path_before_ready(self):
        self.enqueue()
        with self.download_patches():
            q.download(self.job())
        with mock.patch.object(q, "indexed_item", return_value=None):
            q.process_pending_index()
        self.assertEqual(self.job()["status"], "awaiting-index")
        with mock.patch.object(q, "indexed_item", return_value={"Id": "jellyfin-item"}):
            q.process_pending_index()
        self.assertEqual(self.job()["status"], "ready")
        self.assertEqual(self.job()["item_id"], "jellyfin-item")

    def test_index_lookup_rejects_similar_item_with_different_path(self):
        path = q.LIBRARY / f"Title [{FIRST}]" / "video.mkv"
        with mock.patch.object(q, "jellyfin", side_effect=[
            [{"Locations": [str(q.LIBRARY)], "ItemId": "library"}],
            {"Items": [{"Id": "wrong", "Path": str(path) + ".wrong"}]}]):
            self.assertIsNone(q.indexed_item(path))

    def test_empty_library_gets_one_validation_fallback_after_monitor_grace(self):
        self.enqueue()
        path = q.LIBRARY / "Title" / "video.mkv"
        path.parent.mkdir()
        path.write_bytes(b"published")
        q.update_job(FIRST, status="awaiting-index", path=str(path))
        with q.database() as db:
            db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 61, FIRST))
        with mock.patch.object(q, "indexed_item", return_value=None), mock.patch.object(q, "jellyfin") as api:
            q.process_pending_index()
            api.assert_called_once_with("POST", "/Library/Refresh")
            with q.database() as db:
                db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 1000, FIRST))
            q.process_pending_index()
            api.assert_called_once()
        self.assertEqual(self.job()["status"], "awaiting-index")

    def test_worker_pauses_on_disk_pressure_without_consuming_attempts(self):
        self.enqueue()
        with mock.patch.object(q, "process_pending_index"), \
             mock.patch.object(q.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), \
             mock.patch.object(q, "download") as download:
            q.worker()
        download.assert_not_called()
        self.assertEqual(self.job()["status"], "paused")
        self.assertEqual(self.job()["attempts"], 0)

    def test_worker_bounds_retries_and_keeps_other_jobs(self):
        self.enqueue()
        self.enqueue(SECOND)
        q.update_job(FIRST, attempts=2)
        with mock.patch.object(q, "process_pending_index"), \
             mock.patch.object(q.shutil, "disk_usage", return_value=SimpleNamespace(free=q.RESERVE * 2)), \
             mock.patch.object(q, "download", side_effect=q.QueueError("Network timeout")):
            q.worker()
        self.assertEqual(self.job()["status"], "failed")
        self.assertEqual(self.job()["attempts"], 3)
        self.assertEqual(self.job(SECOND)["status"], "queued")

    def test_worker_recovers_publication_before_crash(self):
        self.enqueue()
        path = q.LIBRARY / "Title" / "video.mkv"
        path.parent.mkdir()
        path.write_bytes(b"published")
        q.update_job(FIRST, status="downloading", path=str(path))
        with mock.patch.object(q, "verify_media", return_value=True), \
             mock.patch.object(q, "process_pending_index"), mock.patch.object(q, "download") as download:
            q.worker()
        download.assert_not_called()
        self.assertEqual(self.job()["status"], "awaiting-index")

    def test_retry_resets_failed_job_but_preserves_partial_data(self):
        self.enqueue()
        partial = q.STAGING / "video.part"
        partial.write_bytes(b"partial")
        q.update_job(FIRST, status="failed", attempts=3, error="transient failure")
        q.retry(FIRST)
        self.assertEqual(self.job()["status"], "queued")
        self.assertEqual(self.job()["attempts"], 0)
        self.assertEqual(partial.read_bytes(), b"partial")
        with self.assertRaises(q.QueueError):
            q.retry(FIRST)

    def test_watcher_is_quiet_during_normal_progress_and_deduplicates_ready(self):
        self.enqueue()
        for status in ("queued", "downloading", "awaiting-index"):
            q.update_job(FIRST, status=status)
            self.assertEqual(self.capture(q.watch), "")
        q.update_job(FIRST, status="ready", item_id="jellyfin-item")
        message = self.capture(q.watch)
        self.assertIn("Ready in Jellyfin", message)
        self.assertIn("jellyfin-item", message)
        self.assertEqual(self.capture(q.watch), "")

    def test_watcher_does_not_acknowledge_failed_stdout(self):
        self.enqueue()
        q.update_job(FIRST, status="failed", error="Private video")
        with mock.patch("builtins.print", side_effect=BrokenPipeError), self.assertRaises(BrokenPipeError):
            q.watch()
        self.assertIn("Private video", self.capture(q.watch))

    def test_playlist_reports_partial_success_without_losing_successful_video(self):
        saved = q.snapshot(self.scope, "playlist", "Playlist", [self.item(), self.item(SECOND), None])
        q.queue(self.scope, snapshot_id=saved["snapshot"], confirm=True)
        q.update_job(FIRST, status="ready", item_id="ready-item")
        q.update_job(SECOND, status="failed", error="Video unavailable")
        result = self.capture(q.watch)
        self.assertIn("1 ready in Jellyfin, 1 skipped, 1 failed", result)
        self.assertEqual(self.job()["status"], "ready")

    def test_playlist_does_not_repeat_existing_failure_for_normal_download_progress(self):
        saved = q.snapshot(self.scope, "playlist", "Playlist", [self.item(), self.item(SECOND)])
        q.queue(self.scope, snapshot_id=saved["snapshot"], confirm=True)
        q.update_job(FIRST, status="failed", error="Video unavailable")
        self.assertIn("Video unavailable", self.capture(q.watch))
        q.update_job(SECOND, status="downloading")
        self.assertEqual(self.capture(q.watch), "")
        q.update_job(SECOND, status="ready", item_id="ready-item")
        self.assertIn("1 ready in Jellyfin", self.capture(q.watch))


if __name__ == "__main__":
    unittest.main()
