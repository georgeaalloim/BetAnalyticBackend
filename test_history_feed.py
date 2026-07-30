from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import database
from database import (
    initialize_database,
    save_fixture_history_details,
    save_fixture_statistics,
    save_fixtures,
)
from static_feed_generator import generate_static_feed


class HistoryFeedTests(unittest.TestCase):
    def test_completed_match_is_in_history(self) -> None:
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures([{
                    "fixture": {"id": 1, "date": "2026-09-01T17:00:00Z", "status": {"short": "FT"}, "time_confirmed": True, "source": "test"},
                    "league": {"id": 197, "season": 2026},
                    "teams": {"home": {"id": 10, "name": "Home"}, "away": {"id": 20, "name": "Away"}},
                    "goals": {"home": 2, "away": 1},
                }])
                save_fixture_statistics([{
                    "fixture_id": 1, "league_id": 197, "season": 2026,
                    "fixture_date": "2026-09-01T17:00:00Z",
                    "home_team_id": 10, "home_team_name": "Home",
                    "away_team_id": 20, "away_team_name": "Away",
                    "home_corners": 6, "away_corners": 3,
                    "home_yellow_cards": 2, "away_yellow_cards": 4,
                    "home_red_cards": 0, "away_red_cards": 0,
                    "home_total_shots": 12, "away_total_shots": 8,
                    "home_shots_on_target": 5, "away_shots_on_target": 3,
                    "home_fouls": 11, "away_fouls": 14,
                    "home_offsides": 1, "away_offsides": 2,
                    "source": "test", "collected_at": "2026-09-01T20:00:00Z",
                }])
                save_fixture_history_details([{
                    "fixture_id": 1,
                    "provider_fixture_id": 999,
                    "home_total_shots": 13, "away_total_shots": 9,
                    "home_shots_on_target": 6, "away_shots_on_target": 2,
                    "home_fouls": 10, "away_fouls": 12,
                    "home_yellow_cards": 1, "away_yellow_cards": 3,
                    "home_red_cards": 0, "away_red_cards": 0,
                    "home_offsides": 2, "away_offsides": 1,
                    "home_corners": 7, "away_corners": 2,
                    "goal_scorers_json": '[{"player_name":"A Player","side":"home","minute":22}]',
                    "score_verified": True,
                    "available_stat_pairs": 7,
                    "data_quality": "complete",
                    "source": "API-Football Free fixture details",
                    "collected_at": "2026-09-01T20:05:00Z",
                }])
                output = Path(temp_dir) / "out"
                generated = generate_static_feed(
                    output_dir=output, league_id=197, league_name="Super League 1",
                    seasons=(2025, 2026), as_of=datetime(2026, 9, 2, tzinfo=timezone.utc),
                    lookahead_days=45, upcoming_statuses=("NS", "TBD"),
                    feed_public_url="feed.json", sync_summary={},
                )
                import json
                feed = json.loads(generated.feed_path.read_text(encoding="utf-8"))
                self.assertEqual(feed["history"]["default_season"], 2026)
                self.assertEqual(feed["history"]["available_seasons"], [2026])
                self.assertEqual(
                    [item["season"] for item in feed["history"]["seasons"]],
                    [2026],
                )
                season = feed["history"]["seasons"][0]
                self.assertEqual(season["matches_count"], 1)
                self.assertEqual(season["matches"][0]["statistics"]["corners"]["home"], 7)
                self.assertEqual(season["matches"][0]["statistics"]["total_shots"]["home"], 13)
                self.assertTrue(season["matches"][0]["goal_scorers"]["available"])
                self.assertTrue(season["matches"][0]["statistics_source_consistent"])
                self.assertEqual(
                    season["matches"][0]["statistics_source"],
                    "API-Football Free fixture details",
                )
            finally:
                database.DATABASE_PATH = original


if __name__ == "__main__":
    unittest.main()
