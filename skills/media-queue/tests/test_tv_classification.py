"""TV season release naming should resolve exact series title and year."""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "media_organizer.py"
spec = importlib.util.spec_from_file_location("media_organizer", SCRIPT)
organizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(organizer)


class TvClassificationTests(unittest.TestCase):
    def test_release_variants(self):
        fixtures = {
            "The Middle (2009) Season 1 S01 (1080p AMZN WEB-DL x265 HEVC 10bit EAC3 5.1 RCVR)": ("tv", "The Middle", 2009),
            "Invincible [2021] Season 2 S02 1080p": ("tv", "Invincible", 2021),
            "The.Middle.2009.Season.1.S01.1080p": ("tv", "The Middle", 2009),
            "The Middle Season 1 S01 1080p": ("tv", "The Middle", None),
            "The.Middle.2009.S01E01.mkv": ("tv", "The Middle", 2009),
            "One Punch Man - S03E05 - Monster King.mkv": ("tv", "One Punch Man", None),
            "One.Punch.Man.S03.1080p.WEB-DL": ("tv", "One Punch Man", None),
            "One.Punch.Man.S03": ("tv", "One Punch Man", None),
            "1899.S01E01.mkv": ("tv", "1899", None),
            "Backrooms.2026.2160p.WEB-DL": ("movie", "Backrooms", 2026),
            "Obsession (2026) 1080p.mkv": ("movie", "Obsession", 2026),
            "Backrooms.2026": ("movie", "Backrooms", 2026),
        }
        for name, expected in fixtures.items():
            with self.subTest(name=name):
                self.assertEqual(organizer.classify(name), expected)

    def test_unknown_release_requires_review(self):
        with self.assertRaises(organizer.OrganizerError):
            organizer.classify("Unidentified download")

    def test_middle_season_release_extracts_show_title_and_year(self):
        self.assertEqual(
            organizer.classify(
                "The Middle (2009) Season 1 S01 (1080p AMZN WEB-DL).mkv"
            ),
            ("tv", "The Middle", 2009),
        )

    def test_invincible_season_release_extracts_show_title_and_year(self):
        self.assertEqual(
            organizer.classify(
                "Invincible (2021) Season 2 S02 (1080p AMZN WEB-DL)"
            ),
            ("tv", "Invincible", 2021),
        )

    def test_episode_release_without_year_remains_supported(self):
        self.assertEqual(
            organizer.classify("One.Punch.Man.S03E05.1080p.WEB-DL"),
            ("tv", "One Punch Man", None),
        )


if __name__ == "__main__":
    unittest.main()
