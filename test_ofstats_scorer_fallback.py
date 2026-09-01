from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import database
from database import get_connection, initialize_database, save_fixtures
from ofstats_scorer_fallback import (
    SOURCE_NAME,
    _page_matches_fixture,
    _parse_scorers,
    enrich_goal_scorers_from_ofstats,
)


ROW = {
    "fixture_id": 88001,
    "season": 2026,
    "fixture_date": "2026-08-30T18:00:00Z",
    "home_team_id": 5050,
    "home_team_name": "Kifisia",
    "away_team_id": 575,
    "away_team_name": "AEK Athens FC",
    "home_goals": 1,
    "away_goals": 1,
}

MATCH_HTML = """
<html><body>
<a href="/team/kifisia">Kifisia</a>
<div>30.08.2026</div><div>21:00</div><div>1 : 1</div><div>Finished</div>
<a href="/team/aek-athens">AEK Athens</a>
<table>
<tr><td>90+7'</td><td>Goal! Bernardo Martins scores to make it 1-1, assisted by Clement Jolibois.</td></tr>
<tr><td>63'</td><td>Goal! Barnabas Varga scores to make it 0-1, assisted by Razvan Marin.</td></tr>
</table>
</body></html>
"""


class _Response:
    def __init__(self, text: str, url: str, status_code: int = 200) -> None:
        self.text = text
        self.url = url
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, timeout: int) -> _Response:
        self.calls.append(url)
        # Real provider currently retains the originally scheduled 29/08 date
        # in this URL while the match page itself displays 30/08.
        if url.endswith("/matches/view/kifisia-aek-athens-2026-08-29"):
            return _Response(MATCH_HTML, url)
        return _Response("", url, 404)

    def close(self) -> None:
        return None


class OFStatsScorerFallbackTests(unittest.TestCase):
    def test_page_requires_matching_date_teams_and_score(self) -> None:
        self.assertTrue(_page_matches_fixture(MATCH_HTML, ROW))

    def test_goal_progression_identifies_sides_and_added_time(self) -> None:
        scorers = _parse_scorers(MATCH_HTML, ROW)
        self.assertEqual(2, len(scorers))
        self.assertEqual("Barnabas Varga", scorers[0]["player_name"])
        self.assertEqual("away", scorers[0]["side"])
        self.assertEqual(63, scorers[0]["minute"])
        self.assertEqual("Bernardo Martins", scorers[1]["player_name"])
        self.assertEqual("home", scorers[1]["side"])
        self.assertEqual(90, scorers[1]["minute"])
        self.assertEqual(7, scorers[1]["extra_minute"])

    def test_verified_fallback_is_persisted(self) -> None:
        original_path = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures(
                    [
                        {
                            "fixture": {
                                "id": ROW["fixture_id"],
                                "date": ROW["fixture_date"],
                                "status": {"short": "FT"},
                                "time_confirmed": True,
                                "source": "test",
                            },
                            "league": {"id": 197, "season": 2026},
                            "teams": {
                                "home": {"id": 5050, "name": "Kifisia"},
                                "away": {"id": 575, "name": "AEK Athens FC"},
                            },
                            "goals": {"home": 1, "away": 1},
                        }
                    ]
                )

                result = enrich_goal_scorers_from_ofstats(
                    season=2026,
                    recent_days=3650,
                    max_matches=5,
                    as_of=datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc),
                    session=_Session(),
                )
                self.assertEqual(1, result.matches_saved)
                self.assertEqual(0, result.pending_matches)

                with get_connection() as connection:
                    saved = connection.execute(
                        "SELECT * FROM fixture_goal_scorers WHERE fixture_id = ?",
                        (ROW["fixture_id"],),
                    ).fetchone()
                self.assertIsNotNone(saved)
                self.assertEqual(SOURCE_NAME, saved["source"])
                scorers = json.loads(saved["goal_scorers_json"])
                self.assertEqual(["away", "home"], [item["side"] for item in scorers])
            finally:
                database.DATABASE_PATH = original_path


if __name__ == "__main__":
    unittest.main()
