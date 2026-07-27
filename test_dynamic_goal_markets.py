from __future__ import annotations

import unittest

from market_lines import (
    build_total_market_lines,
    combine_total_market_lines,
    select_strongest_relevant_market,
)


class DynamicGoalMarketsTest(unittest.TestCase):
    def test_build_total_market_lines(self) -> None:
        scores = [
            {"home_goals": 0, "away_goals": 0, "probability": 0.20},
            {"home_goals": 1, "away_goals": 0, "probability": 0.30},
            {"home_goals": 1, "away_goals": 1, "probability": 0.25},
            {"home_goals": 2, "away_goals": 1, "probability": 0.25},
        ]

        lines = build_total_market_lines(scores, lines=(1.5, 2.5))
        line_1_5 = lines[0]
        line_2_5 = lines[1]

        self.assertAlmostEqual(line_1_5["over"], 0.50)
        self.assertAlmostEqual(line_1_5["under"], 0.50)
        self.assertAlmostEqual(line_2_5["over"], 0.25)
        self.assertAlmostEqual(line_2_5["under"], 0.75)

    def test_combine_total_market_lines(self) -> None:
        baseline = [
            {"line": 2.5, "over": 0.60, "under": 0.40},
        ]
        mle = [
            {"line": 2.5, "over": 0.40, "under": 0.60},
        ]

        combined = combine_total_market_lines(
            baseline_lines=baseline,
            mle_lines=mle,
            baseline_weight=0.60,
            mle_weight=0.40,
        )

        self.assertAlmostEqual(combined[0]["over"], 0.52)
        self.assertAlmostEqual(combined[0]["under"], 0.48)

    def test_selects_strongest_relevant_line(self) -> None:
        lines = [
            {"line": 0.5, "over": 0.95, "under": 0.05},
            {"line": 1.5, "over": 0.70, "under": 0.30},
            {"line": 2.5, "over": 0.45, "under": 0.55},
            {"line": 3.5, "over": 0.22, "under": 0.78},
            {"line": 4.5, "over": 0.08, "under": 0.92},
        ]

        selected = select_strongest_relevant_market(
            market_lines=lines,
            expected_total=2.50,
            relevance_window=1.0,
        )

        self.assertEqual(selected["label"], "Under 3.5")
        self.assertNotIn(0.5, selected["candidate_lines"])
        self.assertNotIn(4.5, selected["candidate_lines"])


if __name__ == "__main__":
    unittest.main()
