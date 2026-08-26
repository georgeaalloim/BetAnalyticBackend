from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import database
from database import get_connection, initialize_database, save_fixtures
from fixtur_es_source import (
    _parse_html_fallback,
    _to_api_fixture,
    replace_source_fixtures,
    resolve_team,
)


class HistoryScheduleRegressionTests(unittest.TestCase):
    def test_visible_result_row_is_parsed_as_completed_match(self) -> None:
        html = """
        <table>
          <tr>
            <td>Sun 23 Aug 2026 19:30 +0300</td>
            <td>OFI - Volos NFC</td>
            <td>2 – 0</td>
          </tr>
        </table>
        """
        fixtures = _parse_html_fallback(
            html,
            datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(fixtures), 1)
        item = fixtures[0]
        self.assertEqual(item["fixture"]["status"]["short"], "FT")
        self.assertEqual(item["goals"], {"home": 2, "away": 0})
        self.assertEqual(item["teams"]["home"]["name"], "OFI")
        self.assertEqual(item["teams"]["away"]["name"], "Volos NFC")

    def test_overnight_placeholder_is_never_marked_as_confirmed_time(self) -> None:
        payload = _to_api_fixture(
            kickoff_local=datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc),
            date_only=False,
            home_name="OFI",
            away_name="Kifisia",
            score=None,
            event_status="",
            source_text="OFI - Kifisia",
            as_of=datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(payload["fixture"]["time_confirmed"])


    def test_levadeiakos_spelling_resolves_to_canonical_team(self) -> None:
        team_id, team_name = resolve_team("Levadeiakos")
        self.assertEqual(team_id, 957)
        self.assertEqual(team_name, "Levadiakos")

    def test_schedule_refresh_preserves_completed_current_season_match(self) -> None:
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "history.db"
            try:
                initialize_database()
                completed = _to_api_fixture(
                    kickoff_local=datetime(2026, 8, 23, 19, 30, tzinfo=timezone.utc),
                    date_only=False,
                    home_name="OFI",
                    away_name="Volos NFC",
                    score=(2, 0),
                    event_status="",
                    source_text="OFI - Volos NFC 2 - 0",
                    as_of=datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc),
                )
                save_fixtures([completed])

                upcoming = _to_api_fixture(
                    kickoff_local=datetime(2026, 8, 30, 19, 30, tzinfo=timezone.utc),
                    date_only=False,
                    home_name="Aris",
                    away_name="OFI",
                    score=None,
                    event_status="",
                    source_text="Aris - OFI",
                    as_of=datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc),
                )
                replace_source_fixtures([upcoming])

                with get_connection() as connection:
                    rows = connection.execute(
                        """
                        SELECT home_team_name, away_team_name, status,
                               home_goals, away_goals
                        FROM fixtures
                        WHERE season = 2026
                        ORDER BY fixture_date
                        """
                    ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["home_team_name"], "OFI")
                self.assertEqual(rows[0]["away_team_name"], "Volos NFC")
                self.assertEqual(rows[0]["status"], "FT")
                self.assertEqual(rows[0]["home_goals"], 2)
                self.assertEqual(rows[0]["away_goals"], 0)
            finally:
                database.DATABASE_PATH = original


if __name__ == "__main__":
    unittest.main()
