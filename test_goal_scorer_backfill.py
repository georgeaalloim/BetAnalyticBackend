from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import database
from database import get_connection, initialize_database, save_fixtures
from goal_scorer_backfill import apply_committed_scorer_backfill


class GoalScorerBackfillTests(unittest.TestCase):
    def test_committed_backfill_matches_fixture_by_date_teams_and_score(self) -> None:
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures([{
                    "fixture": {"id": 888, "date": "2026-08-22T17:00:00Z", "status": {"short": "FT"}, "time_confirmed": True, "source": "test"},
                    "league": {"id": 197, "season": 2026},
                    "teams": {"home": {"id": 553, "name": "Olympiakos Piraeus"}, "away": {"id": 12260, "name": "Atromitos"}},
                    "goals": {"home": 1, "away": 0},
                }])
                path = Path(temp_dir) / "backfill.json"
                path.write_text(json.dumps([{
                    "local_date": "2026-08-22",
                    "home_team_id": 553,
                    "away_team_id": 12260,
                    "home_goals": 1,
                    "away_goals": 0,
                    "scorers": [{
                        "player_name": "David Carmo", "side": "home",
                        "team_id": 553, "team_name": "Olympiakos Piraeus",
                        "minute": 82, "extra_minute": None, "detail": "Goal",
                    }],
                }], ensure_ascii=False), encoding="utf-8")
                result = apply_committed_scorer_backfill(path)
                self.assertEqual(result.matched, 1)
                self.assertEqual(result.saved, 1)
                with get_connection() as connection:
                    row = connection.execute(
                        "SELECT source, goal_scorers_json FROM fixture_goal_scorers WHERE fixture_id = 888"
                    ).fetchone()
                self.assertIn("verified backfill", row["source"])
                self.assertEqual(json.loads(row["goal_scorers_json"])[0]["player_name"], "David Carmo")
            finally:
                database.DATABASE_PATH = original


if __name__ == "__main__":
    unittest.main()
