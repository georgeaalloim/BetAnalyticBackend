from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from count_market_model import (
    build_count_market_context,
    calculate_market_lines,
    predict_count_market,
    select_most_probable_relevant_line,
    walk_forward_backtest,
)


def synthetic_records(total: int = 180) -> list[dict]:
    teams = [1, 2, 3, 4, 5, 6]
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    records: list[dict] = []
    for index in range(total):
        home = teams[index % len(teams)]
        away = teams[(index * 2 + 1) % len(teams)]
        if home == away:
            away = teams[(teams.index(away) + 1) % len(teams)]
        records.append(
            {
                "fixture_id": index + 1,
                "fixture_date": (start + timedelta(days=index)).isoformat(),
                "home_team_id": home,
                "away_team_id": away,
                "home_corners": 4 + (home + index) % 5,
                "away_corners": 3 + (away + index) % 5,
                "home_yellow_cards": 1 + (home + index) % 4,
                "away_yellow_cards": 1 + (away + index * 2) % 4,
            }
        )
    return records


class CountMarketModelTests(unittest.TestCase):
    def test_lines_are_probabilities(self) -> None:
        rows = calculate_market_lines(9.2, [7.5, 8.5, 9.5])
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertAlmostEqual(row["over"] + row["under"], 1.0, places=7)

    def test_selected_line_is_relevant_not_extreme(self) -> None:
        rows = calculate_market_lines(9.2, [5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5, 12.5, 13.5])
        selected = select_most_probable_relevant_line(
            expected_total=9.2,
            all_lines=rows,
            relevant_distance=1.5,
        )
        self.assertIn(selected["line"], {8.5, 9.5, 10.5})
        self.assertIn(selected["side"], {"OVER", "UNDER"})

    def test_predict_corners_and_cards(self) -> None:
        records = synthetic_records()
        corners = build_count_market_context(records, market="corners")
        cards = build_count_market_context(records, market="yellow_cards")
        corner_prediction = predict_count_market(corners, home_team_id=1, away_team_id=2)
        card_prediction = predict_count_market(cards, home_team_id=1, away_team_id=2)
        self.assertEqual(corner_prediction["status"], "ready")
        self.assertEqual(card_prediction["status"], "ready")
        self.assertGreater(corner_prediction["selected"]["probability_percent"], 50)
        self.assertGreater(card_prediction["selected"]["probability_percent"], 50)

    def test_walk_forward_is_evaluated(self) -> None:
        report = walk_forward_backtest(synthetic_records(220), market="corners")
        self.assertGreater(report["predictions_evaluated"], 0)
        self.assertIsNotNone(report["accuracy_percent"])
        self.assertIsNotNone(report["brier_score"])


if __name__ == "__main__":
    unittest.main()
