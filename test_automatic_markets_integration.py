from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database
from static_feed_generator import generate_static_feed


class AutomaticMarketsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_database_path = database.DATABASE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DATABASE_PATH = Path(self.temp_dir.name) / "integration.db"
        database.initialize_database()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_feed_contains_ready_dynamic_corners_and_cards(self) -> None:
        teams = [1, 2, 3, 4, 5, 6]
        fixtures = []
        statistics = []
        start = datetime(2024, 7, 15, 17, 0, tzinfo=timezone.utc)
        for index in range(180):
            home = teams[index % len(teams)]
            away = teams[(index * 2 + 1) % len(teams)]
            if home == away:
                away = teams[(teams.index(away) + 1) % len(teams)]
            fixture_id = 10_000 + index
            fixture_date = start + timedelta(days=index * 3)
            season = 2024 if fixture_date.year < 2025 else 2025
            fixtures.append(
                {
                    "fixture": {
                        "id": fixture_id,
                        "date": fixture_date.isoformat(),
                        "status": {"short": "FT"},
                        "time_confirmed": True,
                        "source": "test",
                    },
                    "league": {"id": 197, "season": season},
                    "teams": {
                        "home": {"id": home, "name": f"Team {home}"},
                        "away": {"id": away, "name": f"Team {away}"},
                    },
                    "goals": {
                        "home": (home + index) % 4,
                        "away": (away + index) % 3,
                    },
                }
            )
            statistics.append(
                {
                    "fixture_id": fixture_id,
                    "league_id": 197,
                    "season": season,
                    "fixture_date": fixture_date.isoformat(),
                    "home_team_id": home,
                    "home_team_name": f"Team {home}",
                    "away_team_id": away,
                    "away_team_name": f"Team {away}",
                    "home_corners": 4 + (home + index) % 5,
                    "away_corners": 3 + (away + index) % 5,
                    "home_yellow_cards": 1 + (home + index) % 4,
                    "away_yellow_cards": 1 + (away + index * 2) % 4,
                    "home_red_cards": 0,
                    "away_red_cards": 0,
                    "source": "test",
                    "collected_at": "2026-07-01T00:00:00Z",
                }
            )

        upcoming_id = 99_999
        fixtures.append(
            {
                "fixture": {
                    "id": upcoming_id,
                    "date": "2026-08-22T17:00:00+00:00",
                    "status": {"short": "NS"},
                    "time_confirmed": True,
                    "source": "test schedule",
                },
                "league": {"id": 197, "season": 2026},
                "teams": {
                    "home": {"id": 1, "name": "Team 1"},
                    "away": {"id": 2, "name": "Team 2"},
                },
                "goals": {"home": None, "away": None},
            }
        )
        database.save_fixtures(fixtures)
        database.save_fixture_statistics(statistics)

        output = Path(self.temp_dir.name) / "output"
        generated = generate_static_feed(
            output_dir=output,
            league_id=197,
            league_name="Super League 1",
            seasons=(2024, 2025, 2026),
            as_of=datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
            lookahead_days=45,
            upcoming_statuses=("NS", "TBD"),
            feed_public_url="feed.json",
            sync_summary={"test": True},
        )
        self.assertEqual(generated.fixture_count, 1)
        feed = json.loads(generated.feed_path.read_text(encoding="utf-8"))
        fixture = feed["seasons"][0]["fixtures"][0]
        self.assertTrue(fixture["kickoff_time_confirmed"])
        self.assertEqual(fixture["schedule_source"], "test schedule")
        self.assertEqual(fixture["prediction_status"], "ready")
        self.assertEqual(fixture["prediction"]["corners_market"]["status"], "ready")
        self.assertEqual(
            fixture["prediction"]["yellow_cards_market"]["status"], "ready"
        )
        self.assertIsNotNone(
            fixture["prediction"]["corners_market"]["selected"]
        )


if __name__ == "__main__":
    unittest.main()
