from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import database
from database import initialize_database, save_fixtures
from static_feed_generator import generate_static_feed


class LiveCatalogFeedTests(unittest.TestCase):
    def test_started_fixture_remains_in_live_candidates_but_not_upcoming(self) -> None:
        original = database.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            database.DATABASE_PATH = Path(temp_dir) / "test.db"
            try:
                initialize_database()
                save_fixtures([{
                    "fixture": {
                        "id": 8801,
                        "date": "2026-08-30T18:00:00Z",
                        "status": {"short": "NS"},
                        "time_confirmed": True,
                        "source": "test",
                    },
                    "league": {"id": 197, "season": 2026},
                    "teams": {
                        "home": {"id": 553, "name": "Olympiakos Piraeus"},
                        "away": {"id": 1123, "name": "Aris Thessalonikis"},
                    },
                    "goals": {"home": None, "away": None},
                }])
                generated = generate_static_feed(
                    output_dir=Path(temp_dir) / "out",
                    league_id=197,
                    league_name="Super League 1",
                    seasons=(2026,),
                    as_of=datetime(2026, 8, 30, 18, 35, tzinfo=timezone.utc),
                    lookahead_days=45,
                    upcoming_statuses=("NS", "TBD"),
                    feed_public_url="feed.json",
                    sync_summary={},
                )
                feed = json.loads(generated.feed_path.read_text(encoding="utf-8"))
                self.assertEqual(0, feed["fixtures_count"])
                self.assertEqual(1, len(feed["live_candidates"]))
                self.assertEqual(8801, feed["live_candidates"][0]["fixture_id"])
            finally:
                database.DATABASE_PATH = original


if __name__ == "__main__":
    unittest.main()
