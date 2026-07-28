from __future__ import annotations

import unittest

from ensemble_value_service import build_ensemble_context, predict_match_ensemble


class ColdStartPredictionTests(unittest.TestCase):
    def test_promoted_team_uses_limited_neutral_prior(self) -> None:
        fixtures = []
        for index in range(80):
            home = 1 if index % 2 == 0 else 2
            away = 2 if home == 1 else 1
            fixtures.append(
                {
                    "fixture_id": index + 1,
                    "home_team_id": home,
                    "home_team_name": f"Team {home}",
                    "away_team_id": away,
                    "away_team_name": f"Team {away}",
                    "home_goals": 1 + index % 3,
                    "away_goals": index % 2,
                }
            )

        context = build_ensemble_context(fixtures)
        prediction = predict_match_ensemble(
            context,
            home_team_id=1,
            away_team_id=999,
        )

        self.assertEqual(prediction["data_quality"]["level"], "limited")
        self.assertEqual(prediction["data_quality"]["cold_start_team_ids"], [999])
        probabilities = prediction["result_probabilities"]
        self.assertAlmostEqual(
            probabilities["home_win"]
            + probabilities["draw"]
            + probabilities["away_win"],
            1.0,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
