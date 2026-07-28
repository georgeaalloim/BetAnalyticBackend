from __future__ import annotations

import unittest

from api_football_free_source import parse_api_football_response


class ApiFootballFreeSourceTests(unittest.TestCase):
    def test_parser_normalizes_fixture_and_time(self) -> None:
        payload = {
            "response": [
                {
                    "fixture": {
                        "id": 123,
                        "date": "2026-08-22T20:30:00+03:00",
                        "status": {"short": "NS"},
                    },
                    "league": {"id": 197, "season": 2026},
                    "teams": {
                        "home": {"id": 575, "name": "AEK Athens FC"},
                        "away": {"id": 619, "name": "PAOK"},
                    },
                    "goals": {"home": None, "away": None},
                }
            ]
        }
        fixtures = parse_api_football_response(payload, season=2026)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["fixture"]["id"], 123)
        self.assertTrue(fixtures[0]["fixture"]["time_confirmed"])
        self.assertEqual(fixtures[0]["teams"]["home"]["id"], 575)

    def test_midnight_upcoming_time_is_not_confirmed(self) -> None:
        payload = {
            "response": [
                {
                    "fixture": {
                        "id": 124,
                        "date": "2026-08-22T00:00:00+03:00",
                        "status": {"short": "NS"},
                    },
                    "league": {"id": 197, "season": 2026},
                    "teams": {
                        "home": {"id": 575, "name": "AEK Athens FC"},
                        "away": {"id": 619, "name": "PAOK"},
                    },
                    "goals": {"home": None, "away": None},
                }
            ]
        }
        fixtures = parse_api_football_response(payload, season=2026)
        self.assertFalse(fixtures[0]["fixture"]["time_confirmed"])


if __name__ == "__main__":
    unittest.main()
