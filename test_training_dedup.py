from __future__ import annotations

import unittest
from datetime import datetime, timezone

from static_feed_generator import select_training_fixtures, select_training_statistics


class TrainingDedupTests(unittest.TestCase):
    def test_same_completed_match_is_used_once_by_goal_model(self) -> None:
        fixtures = [
            {
                "fixture_id": 101,
                "season": 2026,
                "fixture_date": "2026-08-23T17:00:00Z",
                "status": "FT",
                "home_team_id": 15,
                "away_team_id": 16,
                "home_goals": 4,
                "away_goals": 0,
                "kickoff_time_confirmed": 1,
            },
            {
                "fixture_id": 202,
                "season": 2026,
                "fixture_date": "2026-08-23T18:00:00Z",
                "status": "FT",
                "home_team_id": 15,
                "away_team_id": 16,
                "home_goals": 4,
                "away_goals": 0,
                "kickoff_time_confirmed": 1,
            },
        ]
        selected = select_training_fixtures(
            fixtures,
            cutoff=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        self.assertEqual(len(selected), 1)


    def test_same_completed_match_with_provider_spelling_variant_is_used_once(self) -> None:
        fixtures = [
            {
                "fixture_id": 101,
                "season": 2026,
                "fixture_date": "2026-08-23T17:00:00Z",
                "status": "FT",
                "home_team_id": 619,
                "home_team_name": "PAOK",
                "away_team_id": 957,
                "away_team_name": "Levadiakos",
                "home_goals": 4,
                "away_goals": 0,
                "kickoff_time_confirmed": 1,
            },
            {
                "fixture_id": 202,
                "season": 2026,
                "fixture_date": "2026-08-23T18:00:00Z",
                "status": "FT",
                "home_team_id": 619,
                "home_team_name": "PAOK",
                "away_team_id": 1099999999,
                "away_team_name": "Levadeiakos",
                "home_goals": 4,
                "away_goals": 0,
                "kickoff_time_confirmed": 1,
            },
        ]
        selected = select_training_fixtures(
            fixtures,
            cutoff=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        self.assertEqual(len(selected), 1)

    def test_current_season_statistics_enter_corner_training_once(self) -> None:
        records = [
            {
                "fixture_id": 101,
                "season": 2026,
                "fixture_date": "2026-08-23T17:00:00Z",
                "home_team_id": 15,
                "away_team_id": 16,
                "home_corners": 3,
                "away_corners": 1,
                "collected_at": "2026-08-23T21:00:00Z",
            },
            {
                "fixture_id": 202,
                "season": 2026,
                "fixture_date": "2026-08-23T18:00:00Z",
                "home_team_id": 15,
                "away_team_id": 16,
                "home_corners": 3,
                "away_corners": 1,
                "collected_at": "2026-08-24T01:00:00Z",
            },
        ]
        selected = select_training_statistics(
            records,
            cutoff=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["season"], 2026)
        self.assertEqual(selected[0]["home_corners"], 3)


if __name__ == "__main__":
    unittest.main()
