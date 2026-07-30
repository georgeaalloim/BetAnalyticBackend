from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from clean_rebuild import apply_manual_verification, audit_clean_database


SCHEMA = """
CREATE TABLE fixtures (
    fixture_id INTEGER PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    fixture_date TEXT,
    status TEXT,
    home_team_id INTEGER NOT NULL,
    home_team_name TEXT NOT NULL,
    away_team_id INTEGER NOT NULL,
    away_team_name TEXT NOT NULL,
    home_goals INTEGER,
    away_goals INTEGER,
    kickoff_time_confirmed INTEGER NOT NULL DEFAULT 0,
    schedule_source TEXT
);
CREATE TABLE fixture_statistics (
    fixture_id INTEGER PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    fixture_date TEXT,
    home_team_id INTEGER NOT NULL,
    home_team_name TEXT NOT NULL,
    away_team_id INTEGER NOT NULL,
    away_team_name TEXT NOT NULL,
    home_corners INTEGER NOT NULL,
    away_corners INTEGER NOT NULL,
    home_yellow_cards INTEGER NOT NULL,
    away_yellow_cards INTEGER NOT NULL,
    home_red_cards INTEGER,
    away_red_cards INTEGER,
    home_total_shots INTEGER,
    away_total_shots INTEGER,
    home_shots_on_target INTEGER,
    away_shots_on_target INTEGER,
    home_fouls INTEGER,
    away_fouls INTEGER,
    home_offsides INTEGER,
    away_offsides INTEGER,
    referee TEXT,
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    FOREIGN KEY(fixture_id) REFERENCES fixtures(fixture_id) ON DELETE CASCADE
);
CREATE TABLE fixture_history_details (
    fixture_id INTEGER PRIMARY KEY,
    home_total_shots INTEGER,
    away_total_shots INTEGER,
    home_shots_on_target INTEGER,
    away_shots_on_target INTEGER,
    home_fouls INTEGER,
    away_fouls INTEGER,
    home_yellow_cards INTEGER,
    away_yellow_cards INTEGER,
    home_red_cards INTEGER,
    away_red_cards INTEGER,
    home_offsides INTEGER,
    away_offsides INTEGER,
    home_corners INTEGER,
    away_corners INTEGER,
    goal_scorers_json TEXT,
    provider_fixture_id INTEGER,
    score_verified INTEGER NOT NULL DEFAULT 0,
    available_stat_pairs INTEGER NOT NULL DEFAULT 0,
    data_quality TEXT NOT NULL DEFAULT 'unknown',
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    FOREIGN KEY(fixture_id) REFERENCES fixtures(fixture_id) ON DELETE CASCADE
);
"""


def make_dataset(path: Path, fixture_id: int, source: str = "Football-Data.co.uk") -> None:
    record = {
        "fixture_id": fixture_id,
        "league_id": 197,
        "season": 2025,
        "fixture_date": "2025-11-09T19:00:00+00:00",
        "status": "FT",
        "home_team_id": 617,
        "home_team_name": "Panathinaikos",
        "away_team_id": 619,
        "away_team_name": "PAOK",
        "home_corners": 6,
        "away_corners": 6,
        "home_yellow_cards": 4,
        "away_yellow_cards": 3,
        "home_red_cards": 0,
        "away_red_cards": 0,
        "home_total_shots": 16,
        "away_total_shots": 16,
        "home_shots_on_target": 3,
        "away_shots_on_target": 1,
        "home_fouls": 16,
        "away_fouls": 16,
        "home_offsides": 2,
        "away_offsides": 1,
        "source": source,
        "collected_at": "2026-07-30T00:00:00Z",
    }
    path.write_text(
        json.dumps({"schema_version": 3, "fixtures": [record]}),
        encoding="utf-8",
    )


class CleanRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "test.db"
        self.dataset = root / "stats.json"
        self.ledger = root / "ledger.json"
        connection = sqlite3.connect(self.db)
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO fixtures VALUES
            (1, 197, 2025, '2025-11-09T19:00:00+00:00', 'FT',
             617, 'Panathinaikos', 619, 'PAOK', 2, 1, 1, 'test')
            """
        )
        connection.execute(
            """
            INSERT INTO fixture_statistics VALUES
            (1, 197, 2025, '2025-11-09T19:00:00+00:00',
             617, 'Panathinaikos', 619, 'PAOK',
             6, 6, 4, 3, 0, 0, 16, 16, 3, 1, 16, 16, 2, 1,
             NULL, 'Football-Data.co.uk', '2026-07-30T00:00:00Z')
            """
        )
        connection.commit()
        connection.close()
        make_dataset(self.dataset, 1)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manual_mismatch_quarantines_statistics(self) -> None:
        self.ledger.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "verification_id": "official-1",
                            "season": 2025,
                            "date": "2025-11-09",
                            "home_team_id": 617,
                            "away_team_id": 619,
                            "score": {"home": 2, "away": 1},
                            "expected_statistics": {
                                "home_corners": 3,
                                "away_corners": 5,
                            },
                            "action_on_mismatch": "quarantine_statistics",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        report = apply_manual_verification(
            self.db, self.dataset, self.ledger, apply_fixes=True
        )
        self.assertEqual(report.statistics_mismatches, 1)
        self.assertEqual(report.statistics_quarantined, 1)
        connection = sqlite3.connect(self.db)
        count = connection.execute("SELECT COUNT(*) FROM fixture_statistics").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)
        payload = json.loads(self.dataset.read_text(encoding="utf-8"))
        self.assertEqual(payload["fixtures_count"], 0)

    def test_score_mismatch_is_critical_and_keeps_record(self) -> None:
        self.ledger.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "season": 2025,
                            "date": "2025-11-09",
                            "home_team_id": 617,
                            "away_team_id": 619,
                            "score": {"home": 3, "away": 1},
                            "expected_statistics": {},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        report = apply_manual_verification(
            self.db, self.dataset, self.ledger, apply_fixes=True
        )
        self.assertEqual(report.score_mismatches, 1)
        connection = sqlite3.connect(self.db)
        count = connection.execute("SELECT COUNT(*) FROM fixture_statistics").fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_mixed_source_is_quarantined(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE fixture_statistics SET source = 'mixed + provider' WHERE fixture_id = 1"
        )
        connection.commit()
        connection.close()
        report = audit_clean_database(self.db, self.dataset, apply_fixes=True)
        self.assertEqual(report.invalid_statistics_quarantined, 1)
        self.assertFalse(report.critical_errors)


if __name__ == "__main__":
    unittest.main()
