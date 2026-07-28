from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import database
from football_data_source import (
    FootballDataResult,
    parse_football_data_csv,
    reconcile_and_save_football_data,
    season_code,
)


class FootballDataSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_database_path = database.DATABASE_PATH
        self._temporary_directory = tempfile.TemporaryDirectory()
        database.DATABASE_PATH = Path(self._temporary_directory.name) / "test.db"
        database.initialize_database()

    def tearDown(self) -> None:
        database.DATABASE_PATH = self._original_database_path
        self._temporary_directory.cleanup()

    def test_season_code(self) -> None:
        self.assertEqual(season_code(2025), "2526")
        self.assertEqual(season_code(2026), "2627")

    def test_parser_reads_goals_corners_cards_and_shots(self) -> None:
        csv_text = (
            "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC,HY,AY,HR,AR,HS,AS,HST,AST,HF,AF,HO,AO,Referee\n"
            "G1,20/08/2025,20:00,AEK,PAOK,2,1,7,4,2,3,0,0,14,9,6,3,11,13,1,2,Test Ref\n"
        )
        fixtures, statistics = parse_football_data_csv(
            csv_text,
            season=2025,
            as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(len(statistics), 1)
        self.assertEqual(fixtures[0]["teams"]["home"]["id"], 575)
        self.assertEqual(fixtures[0]["teams"]["away"]["id"], 619)
        self.assertTrue(fixtures[0]["fixture"]["time_confirmed"])
        self.assertEqual(statistics[0]["home_corners"], 7)
        self.assertEqual(statistics[0]["away_yellow_cards"], 3)
        self.assertEqual(statistics[0]["home_shots_on_target"], 6)
        self.assertTrue(statistics[0]["statistics_available"])

    def test_reconciliation_reuses_existing_fixture_id_by_teams_and_local_date(self) -> None:
        database.save_fixtures(
            [
                {
                    "fixture": {
                        "id": 999,
                        "date": "2025-08-20T17:00:00+00:00",
                        "status": {"short": "FT"},
                        "time_confirmed": True,
                        "source": "existing",
                    },
                    "league": {"id": 197, "season": 2025},
                    "teams": {
                        "home": {"id": 575, "name": "AEK Athens FC"},
                        "away": {"id": 619, "name": "PAOK"},
                    },
                    "goals": {"home": 2, "away": 1},
                }
            ]
        )
        csv_text = (
            "Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC,HY,AY\n"
            "20/08/2025,AEK,PAOK,2,1,7,4,2,3\n"
        )
        fixtures, statistics = parse_football_data_csv(
            csv_text,
            season=2025,
            as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        result = FootballDataResult(
            fixtures=fixtures,
            statistics=statistics,
            seasons_requested=[2025],
            seasons_loaded=[2025],
            urls_loaded=["test.csv"],
            rows_loaded=1,
            complete_statistics_rows=1,
            warnings=[],
        )
        reconciled = reconcile_and_save_football_data(result)
        self.assertEqual(reconciled.matched_existing_fixtures, 1)
        self.assertEqual(reconciled.inserted_new_fixtures, 0)
        self.assertEqual(reconciled.statistics[0]["fixture_id"], 999)
        with database.get_connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM fixtures").fetchone()["n"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
