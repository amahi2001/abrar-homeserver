"""Safety checks for movie imports that share a title across release years."""

import contextlib
import importlib.util
import io
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


library = load_script("media_library")
organizer = load_script("media_organizer")


class ImportPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "Obsession.2025.mp4"
        self.source.touch()
        self.args = SimpleNamespace(
            kind="movie", path=str(self.source), title="Obsession", year=2025,
            tmdb_id=None, dry_run=False, confirm=True, replace_existing=False,
        )
        self.lookup = {"title": "Obsession", "year": 2025, "tmdbId": 1436161, "runtime": 18}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(library, "MANUAL_ROOT", self.root).start()
        mock.patch.object(library, "require_active").start()
        mock.patch.object(library, "matching_lookup", return_value=self.lookup).start()
        mock.patch.object(library, "scan_source", return_value=[
            {"path": str(self.source), "movie": None, "rejections": []}
        ]).start()

    def test_runtime_mismatch_blocks_record_creation(self):
        mock.patch.object(library, "existing_record", return_value=None).start()
        mock.patch.object(library, "probe_runtime_minutes", return_value=109).start()
        mock.patch.object(library, "api", return_value=[
            {"title": "Obsession", "year": 2026, "tmdbId": 1339713, "runtime": 109}
        ]).start()
        add_record = mock.patch.object(library, "add_record").start()

        with self.assertRaisesRegex(library.LibraryError, r"Movie runtime mismatch:.*2026"):
            library.organize(self.args)
        add_record.assert_not_called()

    def test_existing_record_dry_run_checks_radarr_scan(self):
        self.args.dry_run = True
        mock.patch.object(library, "existing_record", return_value={"id": 8}).start()
        mock.patch.object(library, "probe_runtime_minutes", return_value=18).start()
        with self.assertRaisesRegex(library.LibraryError, "scanned movie file does not match"):
            library.organize(self.args)

    def test_new_record_dry_run_does_not_claim_final_match(self):
        self.args.dry_run = True
        self.lookup["runtime"] = 109
        mock.patch.object(library, "existing_record", return_value=None).start()
        mock.patch.object(library, "probe_runtime_minutes", return_value=109).start()
        add_record = mock.patch.object(library, "add_record").start()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            library.organize(self.args)
        self.assertIn("preflight only; final match checked after record creation", output.getvalue())
        add_record.assert_not_called()


class OrganizerReviewTests(unittest.TestCase):
    def test_identity_conflicts_require_review(self):
        self.assertTrue(organizer.requires_review("media-library: Movie runtime mismatch: file 109 min"))
        self.assertTrue(organizer.requires_review("Could not verify the movie runtime"))
        self.assertTrue(organizer.requires_review("The scanned movie file does not match the selected movie."))
        self.assertFalse(organizer.requires_review("Radarr API request failed."))


if __name__ == "__main__":
    unittest.main()
