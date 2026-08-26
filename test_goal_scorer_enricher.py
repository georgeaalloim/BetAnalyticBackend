from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import database
from database import get_connection, initialize_database, save_fixtures
from goal_scorer_enricher import enrich_goal_scorers


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    def __init__(self, *, incomplete: bool = False, own_goal: bool = False) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.incomplete = incomplete
        self.own_goal = own_goal

    def get(self, url: str, params: dict[str, Any], timeout: int) -> _Response:
        self.calls.append((url, dict(params)))
        if url.endswith("/searchevents.php"):
            if self.own_goal:
                return _Response({
                    "event": [{
                        "idEvent": "evt-ofi",
                        "idLeague": "4336",
                        "dateEvent": "2026-08-23",
                        "strHomeTeam": "OFI Crete",
                        "strAwayTeam": "Volos NFC",
                        "intHomeScore": "2",
                        "intAwayScore": "0",
                    }]
                })
            return _Response({
                "event": [{
                    "idEvent": "evt-oly",
                    "idLeague": "4336",
                    "dateEvent": "2026-08-22",
                    "strHomeTeam": "Olympiacos",
                    "strAwayTeam": "Atromitos",
                    "intHomeScore": "1",
                    "intAwayScore": "0",
                }]
            })
        if url.endswith("/lookuptimeline.php"):
            if self.own_goal:
                return _Response({
                    "timeline": [
                        {
                            "strTimeline": "Goal",
                            "strTimelineDetail": "Own Goal",
                            "strHome": "No",
                            "strPlayer": "Marios Siampanis",
                            "intTime": "6",
                            "strTeam": "Volos NFC",
                        },
                        {
                            "strTimeline": "Goal",
                            "strTimelineDetail": "Penalty",
                            "strHome": "Yes",
                            "strPlayer": "Thiago Nuss",
                            "intTime": "33",
                            "strTeam": "OFI Crete",
                        },
                    ]
                })
            timeline = [{
                "strTimeline": "Goal",
                "strTimelineDetail": "Normal Goal",
                "strHome": "Yes",
                "strPlayer": "David Carmo",
                "intTime": "82",
                "strTeam": "Olympiacos",
            }]
            if self.incomplete:
                timeline = []
            return _Response({"timeline": timeline})
        return _Response({})

    def close(self) -> None:
        return None


class GoalScorerEnricherTests(unittest.TestCase):
    def _with_db(self):
        return tempfile.TemporaryDirectory()

    def test_automatic_scorer_is_persisted_only_when_score_is_complete(self) -> None:
        original = database.DATABASE_PATH
        with self._with_db() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures([{
                    "fixture": {"id": 7001, "date": "2026-08-22T17:00:00Z", "status": {"short": "FT"}, "time_confirmed": True, "source": "test"},
                    "league": {"id": 197, "season": 2026},
                    "teams": {"home": {"id": 553, "name": "Olympiakos Piraeus"}, "away": {"id": 12260, "name": "Atromitos"}},
                    "goals": {"home": 1, "away": 0},
                }])
                result = enrich_goal_scorers(
                    season=2026,
                    recent_days=3650,
                    max_matches=8,
                    api_key="123",
                    session=_Session(),
                )
                self.assertEqual(result.matches_saved, 1)
                self.assertEqual(result.pending_matches, 0)
                with get_connection() as connection:
                    row = connection.execute(
                        "SELECT * FROM fixture_goal_scorers WHERE fixture_id = 7001"
                    ).fetchone()
                self.assertIsNotNone(row)
                scorers = json.loads(row["goal_scorers_json"])
                self.assertEqual(scorers[0]["player_name"], "David Carmo")
                self.assertEqual(scorers[0]["minute"], 82)
                self.assertEqual(scorers[0]["side"], "home")
                self.assertEqual(row["source"], "TheSportsDB API v1")
            finally:
                database.DATABASE_PATH = original

    def test_incomplete_timeline_stays_pending_for_future_retry(self) -> None:
        original = database.DATABASE_PATH
        with self._with_db() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures([{
                    "fixture": {"id": 7002, "date": "2026-08-22T17:00:00Z", "status": {"short": "FT"}, "time_confirmed": True, "source": "test"},
                    "league": {"id": 197, "season": 2026},
                    "teams": {"home": {"id": 553, "name": "Olympiakos Piraeus"}, "away": {"id": 12260, "name": "Atromitos"}},
                    "goals": {"home": 1, "away": 0},
                }])
                result = enrich_goal_scorers(
                    season=2026,
                    recent_days=3650,
                    api_key="123",
                    session=_Session(incomplete=True),
                )
                self.assertEqual(result.matches_saved, 0)
                self.assertEqual(result.pending_matches, 1)
                with get_connection() as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM fixture_goal_scorers"
                    ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                database.DATABASE_PATH = original

    def test_own_goal_side_is_repaired_using_final_score(self) -> None:
        original = database.DATABASE_PATH
        with self._with_db() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures([{
                    "fixture": {"id": 7003, "date": "2026-08-23T17:00:00Z", "status": {"short": "FT"}, "time_confirmed": True, "source": "test"},
                    "league": {"id": 197, "season": 2026},
                    "teams": {"home": {"id": 1124, "name": "OFI"}, "away": {"id": 2110, "name": "Volos NFC"}},
                    "goals": {"home": 2, "away": 0},
                }])
                result = enrich_goal_scorers(
                    season=2026,
                    recent_days=3650,
                    api_key="123",
                    session=_Session(own_goal=True),
                )
                self.assertEqual(result.matches_saved, 1)
                with get_connection() as connection:
                    row = connection.execute(
                        "SELECT goal_scorers_json FROM fixture_goal_scorers WHERE fixture_id = 7003"
                    ).fetchone()
                scorers = json.loads(row["goal_scorers_json"])
                self.assertEqual([item["side"] for item in scorers], ["home", "home"])
                self.assertEqual(scorers[0]["detail"], "Own Goal")
                self.assertEqual(scorers[0]["team_id"], 1124)
            finally:
                database.DATABASE_PATH = original


if __name__ == "__main__":
    unittest.main()
