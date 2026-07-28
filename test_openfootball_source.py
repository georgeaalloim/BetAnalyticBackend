from __future__ import annotations

import unittest
from datetime import datetime, timezone

from openfootball_source import parse_openfootball_text, season_label


SAMPLE = """
= Greek Super League 2025/26
# Teams 14

▪ 1. Round

 Sat Aug 23 2025

 19:00 Aris Saloniki v Volos NFC 2-0 (0-0)
 Olympiakos Piraeus v Asteras Tripolis 2-0 (0-0)

 Sun Aug 24

 Panathinaikos v OFI Heraklion [postponed]
 19:15 AEK Athen v Panserraikos
"""


class OpenFootballSourceTests(unittest.TestCase):
    def test_season_label(self) -> None:
        self.assertEqual(season_label(2025), "2025-26")

    def test_parser_reads_results_future_and_postponed(self) -> None:
        fixtures = parse_openfootball_text(
            SAMPLE,
            season=2025,
            as_of=datetime(2025, 8, 23, 22, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(fixtures), 4)

        aris = next(
            item for item in fixtures if item["teams"]["home"]["name"] == "Aris Thessalonikis"
        )
        self.assertEqual(aris["fixture"]["status"]["short"], "FT")
        self.assertTrue(aris["fixture"]["time_confirmed"])
        self.assertEqual(aris["goals"], {"home": 2, "away": 0})

        olympiakos = next(
            item
            for item in fixtures
            if item["teams"]["home"]["name"] == "Olympiakos Piraeus"
        )
        self.assertFalse(olympiakos["fixture"]["time_confirmed"])
        self.assertEqual(olympiakos["fixture"]["status"]["short"], "FT")

        postponed = next(
            item
            for item in fixtures
            if item["teams"]["home"]["name"] == "Panathinaikos"
        )
        self.assertEqual(postponed["fixture"]["status"]["short"], "PST")

        aek = next(
            item for item in fixtures if item["teams"]["home"]["name"] == "AEK Athens FC"
        )
        self.assertEqual(aek["fixture"]["status"]["short"], "NS")
        self.assertTrue(aek["fixture"]["time_confirmed"])


if __name__ == "__main__":
    unittest.main()
