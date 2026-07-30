from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database
from static_feed_generator import generate_static_feed


class HistoryVisibilityTrainingRetainedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_database_path = database.DATABASE_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DATABASE_PATH = Path(self.temp_dir.name) / "history_visibility.db"
        database.initialize_database()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_old_seasons_train_model_but_do_not_appear_in_history(self) -> None:
        teams = [1, 2, 3, 4, 5, 6]
        fixtures: list[dict] = []
        statistics: list[dict] = []
        start = datetime(2024, 8, 1, 17, 0, tzinfo=timezone.utc)

        for index in range(180):
            fixture_date = start + timedelta(days=index * 3)
            season = 2024 if fixture_date < datetime(2025, 7, 1, tzinfo=timezone.utc) else 2025
            home = teams[index % len(teams)]
            away = teams[(index * 2 + 1) % len(teams)]
            if home == away:
                away = teams[(teams.index(away) + 1) % len(teams)]
            fixture_id = 20_000 + index
            fixtures.append({
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
                "goals": {"home": index % 4, "away": index % 3},
            })
            statistics.append({
                "fixture_id": fixture_id,
                "league_id": 197,
                "season": season,
                "fixture_date": fixture_date.isoformat(),
                "home_team_id": home,
                "home_team_name": f"Team {home}",
                "away_team_id": away,
                "away_team_name": f"Team {away}",
                "home_corners": 4 + index % 5,
                "away_corners": 3 + index % 5,
                "home_yellow_cards": 1,
                "away_yellow_cards": 2,
                "home_red_cards": 0,
                "away_red_cards": 0,
                "source": "test",
                "collected_at": "2026-07-01T00:00:00Z",
            })

        # One completed current-season game: this is the only match visible in history.
        fixtures.append({
            "fixture": {
                "id": 30_001,
                "date": "2026-07-20T17:00:00+00:00",
                "status": {"short": "FT"},
                "time_confirmed": True,
                "source": "test",
            },
            "league": {"id": 197, "season": 2026},
            "teams": {
                "home": {"id": 1, "name": "Team 1"},
                "away": {"id": 2, "name": "Team 2"},
            },
            "goals": {"home": 1, "away": 0},
        })
        statistics.append({
            "fixture_id": 30_001,
            "league_id": 197,
            "season": 2026,
            "fixture_date": "2026-07-20T17:00:00+00:00",
            "home_team_id": 1,
            "home_team_name": "Team 1",
            "away_team_id": 2,
            "away_team_name": "Team 2",
            "home_corners": 5,
            "away_corners": 4,
            "home_yellow_cards": 1,
            "away_yellow_cards": 2,
            "home_red_cards": 0,
            "away_red_cards": 0,
            "source": "test",
            "collected_at": "2026-07-20T20:00:00Z",
        })

        fixtures.append({
            "fixture": {
                "id": 30_002,
                "date": "2026-08-22T17:00:00+00:00",
                "status": {"short": "NS"},
                "time_confirmed": True,
                "source": "verified date and time; test schedule",
            },
            "league": {"id": 197, "season": 2026},
            "teams": {
                "home": {"id": 1, "name": "Team 1"},
                "away": {"id": 2, "name": "Team 2"},
            },
            "goals": {"home": None, "away": None},
        })

        database.save_fixtures(fixtures)
        database.save_fixture_statistics(statistics)

        output = Path(self.temp_dir.name) / "output"
        generated = generate_static_feed(
            output_dir=output,
            league_id=197,
            league_name="Super League 1",
            seasons=(2024, 2025, 2026),
            as_of=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            lookahead_days=45,
            upcoming_statuses=("NS", "TBD"),
            feed_public_url="feed.json",
            sync_summary={"test": True},
        )

        feed = json.loads(generated.feed_path.read_text(encoding="utf-8"))
        self.assertEqual(feed["model"]["training_season_window"], 3)
        self.assertEqual(feed["history"]["available_seasons"], [2026])
        self.assertEqual([item["season"] for item in feed["history"]["seasons"]], [2026])
        self.assertEqual(feed["history"]["seasons"][0]["matches_count"], 1)

        fixture = feed["seasons"][0]["fixtures"][0]
        self.assertEqual(fixture["prediction_status"], "ready")
        self.assertEqual(fixture["prediction"]["corners_market"]["status"], "ready")
        self.assertGreater(
            fixture["prediction"]["corners_market"]["fixtures_used"],
            1,
            "The current-season match alone is insufficient; older seasons must be retained for training.",
        )


if __name__ == "__main__":
    unittest.main()
