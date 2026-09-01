from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import database
from database import (
    get_connection,
    initialize_database,
    save_fixtures,
)
from sofascore_scorer_fallback import (
    SOURCE_NAME,
    enrich_goal_scorers_from_sofascore,
)


class _Response:
    def __init__(
        self,
        payload: dict[str, Any],
        url: str,
        status_code: int = 200,
    ) -> None:
        self._payload = payload
        self.url = url
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> dict[str, Any]:
        return self._payload


class _Session:
    def __init__(
        self,
        *,
        incomplete: bool = False,
    ) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []
        self.incomplete = incomplete

    def get(self, url: str, timeout: int) -> _Response:
        self.calls.append(url)

        if "/scheduled-events/2026-08-30" in url:
            return _Response(
                {
                    "events": [
                        {
                            "id": 99001,
                            "startTimestamp": 1788112800,
                            "status": {
                                "type": "finished",
                                "description": "Ended",
                            },
                            "tournament": {
                                "name": "Stoiximan Super League",
                                "uniqueTournament": {
                                    "id": 185,
                                    "name": "Stoiximan Super League",
                                },
                                "category": {
                                    "name": "Greece",
                                },
                            },
                            "homeTeam": {
                                "name": "Kifisia",
                            },
                            "awayTeam": {
                                "name": "AEK Athens",
                            },
                            "homeScore": {
                                "current": 1,
                            },
                            "awayScore": {
                                "current": 1,
                            },
                        }
                    ]
                },
                url,
            )

        if url.endswith("/event/99001/incidents"):
            incidents = [
                {
                    "id": 2,
                    "homeScore": 1,
                    "awayScore": 1,
                    "time": 90,
                    "addedTime": 7,
                    "incidentType": "goal",
                    "incidentClass": "regular",
                    "player": {
                        "name": "Home Scorer",
                    },
                },
                {
                    "id": 1,
                    "homeScore": 0,
                    "awayScore": 1,
                    "time": 63,
                    "incidentType": "goal",
                    "incidentClass": "regular",
                    "player": {
                        "name": "Away Scorer",
                    },
                },
            ]
            if self.incomplete:
                incidents = incidents[:1]
            return _Response(
                {"incidents": incidents},
                url,
            )

        return _Response({}, url, 404)

    def close(self) -> None:
        return None


class SofascoreScorerFallbackTests(unittest.TestCase):
    def _create_fixture(self) -> None:
        initialize_database()
        save_fixtures(
            [
                {
                    "fixture": {
                        "id": 88001,
                        "date": "2026-08-30T18:00:00Z",
                        "status": {"short": "FT"},
                        "time_confirmed": True,
                        "source": "test",
                    },
                    "league": {
                        "id": 197,
                        "season": 2026,
                    },
                    "teams": {
                        "home": {
                            "id": 5050,
                            "name": "Kifisia",
                        },
                        "away": {
                            "id": 575,
                            "name": "AEK Athens FC",
                        },
                    },
                    "goals": {
                        "home": 1,
                        "away": 1,
                    },
                }
            ]
        )

    def test_complete_incidents_are_saved_automatically(self) -> None:
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = (
                Path(temp_dir) / "test.db"
            )
            try:
                self._create_fixture()
                result = (
                    enrich_goal_scorers_from_sofascore(
                        season=2026,
                        recent_days=3650,
                        max_matches=20,
                        as_of=datetime(
                            2026,
                            8,
                            31,
                            10,
                            0,
                            tzinfo=timezone.utc,
                        ),
                        session=_Session(),
                    )
                )
                self.assertEqual(
                    result.matches_saved,
                    1,
                )
                self.assertEqual(
                    result.pending_matches,
                    0,
                )

                with get_connection() as connection:
                    row = connection.execute(
                        """
                        SELECT *
                        FROM fixture_goal_scorers
                        WHERE fixture_id = 88001
                        """
                    ).fetchone()

                self.assertIsNotNone(row)
                self.assertEqual(
                    row["source"],
                    SOURCE_NAME,
                )
                scorers = json.loads(
                    row["goal_scorers_json"]
                )
                self.assertEqual(
                    [item["side"] for item in scorers],
                    ["away", "home"],
                )
                self.assertEqual(
                    scorers[0]["minute"],
                    63,
                )
                self.assertEqual(
                    scorers[1]["minute"],
                    90,
                )
                self.assertEqual(
                    scorers[1]["extra_minute"],
                    7,
                )
            finally:
                database.DATABASE_PATH = original

    def test_incomplete_incidents_stay_pending(self) -> None:
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = (
                Path(temp_dir) / "test.db"
            )
            try:
                self._create_fixture()
                result = (
                    enrich_goal_scorers_from_sofascore(
                        season=2026,
                        recent_days=3650,
                        max_matches=20,
                        as_of=datetime(
                            2026,
                            8,
                            31,
                            10,
                            0,
                            tzinfo=timezone.utc,
                        ),
                        session=_Session(
                            incomplete=True
                        ),
                    )
                )
                self.assertEqual(
                    result.matches_saved,
                    0,
                )
                self.assertEqual(
                    result.pending_matches,
                    1,
                )
                with get_connection() as connection:
                    count = connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM fixture_goal_scorers
                        """
                    ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                database.DATABASE_PATH = original


if __name__ == "__main__":
    unittest.main()
