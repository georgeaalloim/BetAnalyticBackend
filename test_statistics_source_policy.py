from __future__ import annotations

import unittest

from statistics_source_policy import choose_whole_record, is_mixed_source, source_key


class StatisticsSourcePolicyTests(unittest.TestCase):
    def test_higher_priority_provider_replaces_whole_snapshot(self) -> None:
        old = {
            "fixture_id": 1,
            "source": "Football-Data.co.uk",
            "home_total_shots": 8,
            "away_total_shots": 4,
            "home_corners": 3,
            "away_corners": 5,
        }
        new = {
            "fixture_id": 1,
            "source": "API-Football Free fixture details",
            "home_total_shots": 6,
            "away_total_shots": 6,
            "home_corners": 3,
            "away_corners": 5,
        }
        selected = choose_whole_record(old, new)
        self.assertEqual(selected["home_total_shots"], 6)
        self.assertEqual(selected["away_total_shots"], 6)
        self.assertEqual(selected["source"], "API-Football Free fixture details")

    def test_lower_priority_provider_cannot_fill_missing_field(self) -> None:
        old = {
            "fixture_id": 1,
            "source": "API-Football Free fixture details",
            "home_total_shots": 6,
            "away_total_shots": 6,
            "home_fouls": None,
            "away_fouls": None,
        }
        new = {
            "fixture_id": 1,
            "source": "Football-Data.co.uk",
            "home_total_shots": 8,
            "away_total_shots": 4,
            "home_fouls": 17,
            "away_fouls": 15,
        }
        selected = choose_whole_record(old, new)
        self.assertEqual(selected["home_total_shots"], 6)
        self.assertIsNone(selected["home_fouls"])
        self.assertEqual(source_key(selected["source"]), "api_football")

    def test_mixed_labels_are_rejected(self) -> None:
        self.assertTrue(is_mixed_source("API-Football + Football-Data.co.uk"))
        self.assertEqual(source_key("Mixed providers"), "mixed")


if __name__ == "__main__":
    unittest.main()
