from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import database
from api_football_history_enricher import enrich_history
from database import get_connection, initialize_database, save_fixtures


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.headers = {"x-ratelimit-requests-remaining": "90"}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, Any], timeout: int) -> _Response:
        if url.endswith("/fixtures/statistics"):
            return _Response({
                "errors": [],
                "response": [
                    {"team": {"id": 100}, "statistics": [
                        {"type": "Total Shots", "value": 12},
                        {"type": "Shots on Goal", "value": 5},
                        {"type": "Fouls", "value": 10},
                        {"type": "Corner Kicks", "value": 7},
                        {"type": "Yellow Cards", "value": 2},
                        {"type": "Red Cards", "value": 0},
                        {"type": "Offsides", "value": 1},
                    ]},
                    {"team": {"id": 200}, "statistics": [
                        {"type": "Total Shots", "value": 8},
                        {"type": "Shots on Goal", "value": 2},
                        {"type": "Fouls", "value": 13},
                        {"type": "Corner Kicks", "value": 3},
                        {"type": "Yellow Cards", "value": 4},
                        {"type": "Red Cards", "value": 0},
                        {"type": "Offsides", "value": 2},
                    ]},
                ],
            })
        if url.endswith("/fixtures/events"):
            return _Response({
                "errors": [],
                "response": [{
                    "time": {"elapsed": 22, "extra": None},
                    "team": {"id": 100, "name": "PAOK"},
                    "player": {"id": 1, "name": "Test Scorer"},
                    "type": "Goal",
                    "detail": "Normal Goal",
                }],
            })
        season = int(params["season"])
        if season == 2025:
            return _Response({
                "errors": [],
                "response": [{
                    "fixture": {"id": 999, "date": "2025-09-01T17:00:00+00:00", "status": {"short": "FT"}},
                    "league": {"id": 197, "season": 2025},
                    "teams": {
                        "home": {"id": 100, "name": "PAOK"},
                        "away": {"id": 200, "name": "Levadiakos"},
                    },
                    "goals": {"home": 1, "away": 0},
                }],
            })
        return _Response({"errors": [], "response": []})

    def close(self) -> None:
        return None


class ApiFootballHistoryEnricherTests(unittest.TestCase):
    def test_enriches_stats_and_scorers_using_canonical_fixture_id(self) -> None:
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures([{
                    "fixture": {"id": 555, "date": "2025-09-01T17:00:00Z", "status": {"short": "FT"}, "time_confirmed": True, "source": "test"},
                    "league": {"id": 197, "season": 2025},
                    "teams": {"home": {"id": 619, "name": "PAOK"}, "away": {"id": 957, "name": "Levadiakos"}},
                    "goals": {"home": 1, "away": 0},
                }])
                result = enrich_history(seasons=(2025, 2026), api_key="test", session=_Session())
                self.assertEqual(result.matches_enriched, 1)
                with get_connection() as connection:
                    row = connection.execute(
                        "SELECT * FROM fixture_history_details WHERE fixture_id = 555"
                    ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["home_total_shots"], 12)
                scorers = json.loads(row["goal_scorers_json"])
                self.assertEqual(scorers[0]["player_name"], "Test Scorer")
                self.assertEqual(scorers[0]["side"], "home")
            finally:
                database.DATABASE_PATH = original


if __name__ == "__main__":
    unittest.main()
