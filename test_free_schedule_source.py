from __future__ import annotations

import unittest
from datetime import datetime, timezone

from free_schedule_source import merge_free_schedule_sources


def fixture(
    source: str,
    kickoff: str,
    *,
    home_id: int = 619,
    home_name: str = "PAOK",
    away_id: int = 1124,
    away_name: str = "OFI",
    confirmed: bool = True,
) -> dict:
    return {
        "fixture": {
            "id": 1,
            "date": kickoff,
            "status": {"short": "NS"},
            "time_confirmed": confirmed,
            "source": source,
        },
        "league": {"id": 197, "name": "Super League 1", "season": 2026},
        "teams": {
            "home": {"id": home_id, "name": home_name},
            "away": {"id": away_id, "name": away_name},
        },
        "goals": {"home": None, "away": None},
    }


class FreeScheduleSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2026, 7, 28, tzinfo=timezone.utc)

    def test_two_sources_verify_time(self) -> None:
        result = merge_free_schedule_sources(
            fixtur_es_fixtures=[fixture("Fixtur.es", "2026-08-22T17:00:00+00:00")],
            openfootball_fixtures=[
                fixture("OpenFootball CC0", "2026-08-22T17:10:00+00:00")
            ],
            football_data_fixtures=[],
            as_of=self.as_of,
        )
        self.assertEqual(len(result.fixtures), 1)
        merged = result.fixtures[0]
        self.assertTrue(merged["fixture"]["time_confirmed"])
        self.assertEqual(merged["fixture"]["verification"], "time_verified")

    def test_single_source_hides_time(self) -> None:
        result = merge_free_schedule_sources(
            fixtur_es_fixtures=[fixture("Fixtur.es", "2026-08-22T17:00:00+00:00")],
            openfootball_fixtures=[],
            football_data_fixtures=[],
            as_of=self.as_of,
        )
        merged = result.fixtures[0]
        self.assertFalse(merged["fixture"]["time_confirmed"])
        self.assertEqual(merged["fixture"]["verification"], "single_source")

    def test_conflicting_dates_hide_time(self) -> None:
        result = merge_free_schedule_sources(
            fixtur_es_fixtures=[fixture("Fixtur.es", "2026-08-22T17:00:00+00:00")],
            openfootball_fixtures=[
                fixture("OpenFootball CC0", "2026-08-23T17:00:00+00:00")
            ],
            football_data_fixtures=[],
            as_of=self.as_of,
        )
        merged = result.fixtures[0]
        self.assertFalse(merged["fixture"]["time_confirmed"])
        self.assertEqual(merged["fixture"]["verification"], "source_conflict")

    def test_two_of_three_sources_form_date_consensus(self) -> None:
        result = merge_free_schedule_sources(
            fixtur_es_fixtures=[fixture("Fixtur.es", "2026-08-22T17:00:00+00:00")],
            openfootball_fixtures=[
                fixture("OpenFootball CC0", "2026-08-23T17:00:00+00:00")
            ],
            football_data_fixtures=[
                fixture("Football-Data", "2026-08-22T17:15:00+00:00")
            ],
            as_of=self.as_of,
        )
        merged = result.fixtures[0]
        self.assertTrue(merged["fixture"]["time_confirmed"])
        self.assertEqual(merged["fixture"]["verification"], "time_verified")

    def test_optional_free_api_can_verify_fixtures_source(self) -> None:
        result = merge_free_schedule_sources(
            fixtur_es_fixtures=[fixture("Fixtur.es", "2026-08-22T17:00:00+00:00")],
            openfootball_fixtures=[],
            football_data_fixtures=[],
            api_football_fixtures=[
                fixture("API-Football Free", "2026-08-22T17:05:00+00:00")
            ],
            as_of=self.as_of,
        )
        merged = result.fixtures[0]
        self.assertTrue(merged["fixture"]["time_confirmed"])
        self.assertIn("API-Football Free", merged["fixture"]["sources"])


if __name__ == "__main__":
    unittest.main()
