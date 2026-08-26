from __future__ import annotations

import unittest

from football_data_latest_fixtures import parse_latest_fixtures_csv


class FootballDataLatestFixturesTests(unittest.TestCase):
    def test_filters_greece_and_preserves_explicit_kickoff(self) -> None:
        text = (
            "Div,Date,Time,HomeTeam,AwayTeam\n"
            "E0,29/08/2026,17:30,Arsenal,Chelsea\n"
            "G1,29/08/2026,19:30,Volos NFC,Iraklis\n"
        )
        fixtures = parse_latest_fixtures_csv(text)
        self.assertEqual(len(fixtures), 1)
        item = fixtures[0]
        self.assertEqual(item["league"]["season"], 2026)
        self.assertEqual(item["teams"]["home"]["name"], "Volos NFC")
        self.assertTrue(item["fixture"]["time_confirmed"])
        # 19:30 Athens in August = 16:30 UTC.
        self.assertTrue(item["fixture"]["date"].startswith("2026-08-29T16:30:00"))

    def test_requires_explicit_time(self) -> None:
        text = "Div,Date,Time,HomeTeam,AwayTeam\nG1,29/08/2026,,Volos NFC,Iraklis\n"
        self.assertEqual(parse_latest_fixtures_csv(text), [])


if __name__ == "__main__":
    unittest.main()
