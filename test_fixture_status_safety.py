from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import database
from fixtur_es_source import _extract_teams, _fixture_status, _to_api_fixture
from static_feed_generator import generate_static_feed


class FixtureStatusSafetyTests(unittest.TestCase):
    def test_suspended_prefix_is_not_part_of_team_name(self) -> None:
        self.assertEqual(
            _extract_teams("⚠️ SUSPENDED: Panathinaikos - Kifisia"),
            ("Panathinaikos", "Kifisia"),
        )
        self.assertEqual(
            _extract_teams("POSTPONED - Panathinaikos - Kifisia"),
            ("Panathinaikos", "Kifisia"),
        )
        self.assertEqual(
            _extract_teams("Panathinaikos - Kifisia (SUSPENDED)"),
            ("Panathinaikos", "Kifisia"),
        )

    def test_suspended_fixture_resolves_real_team_ids_and_is_postponed(self) -> None:
        kickoff = datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc)
        as_of = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
        payload = _to_api_fixture(
            kickoff_local=kickoff,
            date_only=False,
            home_name="Panathinaikos",
            away_name="Kifisia",
            score=None,
            event_status="",
            source_text="⚠️ SUSPENDED: Panathinaikos - Kifisia",
            as_of=as_of,
        )
        self.assertEqual(payload["teams"]["home"], {"id": 617, "name": "Panathinaikos"})
        self.assertEqual(payload["teams"]["away"], {"id": 5050, "name": "Kifisia"})
        self.assertEqual(payload["fixture"]["status"]["short"], "PST")

        status = _fixture_status(
            kickoff_local=kickoff,
            date_only=False,
            score=None,
            event_status="SUSPENDED",
            text="Panathinaikos - Kifisia",
            as_of=as_of,
        )
        self.assertEqual(status, ("PST", None, None))

    def test_postponed_fixture_never_enters_prediction_feed(self) -> None:
        original_database_path = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                database.DATABASE_PATH = Path(temp_dir) / "status_safety.db"
                database.initialize_database()
                database.save_fixtures([
                    {
                        "fixture": {
                            "id": 90_001,
                            "date": "2026-08-23T17:00:00+00:00",
                            "status": {"short": "PST"},
                            "time_confirmed": True,
                            "source": "test suspended fixture",
                        },
                        "league": {"id": 197, "season": 2026},
                        "teams": {
                            "home": {"id": 617, "name": "Panathinaikos"},
                            "away": {"id": 5050, "name": "Kifisia"},
                        },
                        "goals": {"home": None, "away": None},
                    }
                ])
                generated = generate_static_feed(
                    output_dir=Path(temp_dir) / "output",
                    league_id=197,
                    league_name="Super League 1",
                    seasons=(2026,),
                    as_of=datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc),
                    lookahead_days=45,
                    # Even if a bad environment value includes PST, it is blocked.
                    upcoming_statuses=("NS", "TBD", "PST"),
                    feed_public_url="feed.json",
                    sync_summary={"test": True},
                )
                feed = json.loads(generated.feed_path.read_text(encoding="utf-8"))
                self.assertEqual(feed["fixtures_count"], 0)
                self.assertEqual(feed["ready_predictions"], 0)
                self.assertEqual(feed["unavailable_predictions"], 0)
            finally:
                database.DATABASE_PATH = original_database_path


if __name__ == "__main__":
    unittest.main()
