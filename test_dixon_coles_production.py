from __future__ import annotations

import unittest

from ensemble_value_service import (
    DEFAULT_DIXON_COLES_RHO as ENSEMBLE_RHO,
    predict_match_ensemble,
    build_ensemble_context,
)
from poisson_model import (
    DEFAULT_DIXON_COLES_RHO as BASELINE_RHO,
    predict_match,
)
from poisson_mle_model import predict_match_mle
from team_analysis import calculate_home_away_statistics


FIXTURES = [
    {
        "fixture_date": "2026-01-01",
        "home_team_id": 1,
        "home_team_name": "A",
        "away_team_id": 2,
        "away_team_name": "B",
        "home_goals": 1,
        "away_goals": 1,
    },
    {
        "fixture_date": "2026-01-08",
        "home_team_id": 2,
        "home_team_name": "B",
        "away_team_id": 1,
        "away_team_name": "A",
        "home_goals": 0,
        "away_goals": 0,
    },
    {
        "fixture_date": "2026-01-15",
        "home_team_id": 1,
        "home_team_name": "A",
        "away_team_id": 2,
        "away_team_name": "B",
        "home_goals": 2,
        "away_goals": 1,
    },
    {
        "fixture_date": "2026-01-22",
        "home_team_id": 2,
        "home_team_name": "B",
        "away_team_id": 1,
        "away_team_name": "A",
        "home_goals": 1,
        "away_goals": 0,
    },
    {
        "fixture_date": "2026-01-29",
        "home_team_id": 1,
        "home_team_name": "A",
        "away_team_id": 2,
        "away_team_name": "B",
        "home_goals": 1,
        "away_goals": 1,
    },
    {
        "fixture_date": "2026-02-05",
        "home_team_id": 2,
        "home_team_name": "B",
        "away_team_id": 1,
        "away_team_name": "A",
        "home_goals": 1,
        "away_goals": 1,
    },
]


class DixonColesProductionTests(unittest.TestCase):
    def test_tuned_rho_is_the_production_default(self):
        self.assertEqual(BASELINE_RHO, -0.10)
        self.assertEqual(ENSEMBLE_RHO, -0.10)

    def test_negative_rho_increases_baseline_draw_probability(self):
        analysis = calculate_home_away_statistics(FIXTURES)
        independent = predict_match(analysis, 1, 2, rho=0.0)
        corrected = predict_match(analysis, 1, 2, rho=-0.10)
        self.assertGreater(
            corrected["result_probabilities"]["draw"],
            independent["result_probabilities"]["draw"],
        )

    def test_mle_component_uses_dixon_coles(self):
        context = build_ensemble_context(FIXTURES)
        fitted = context["fitted_mle_model"]
        independent = predict_match_mle(fitted, 1, 2, rho=0.0)
        corrected = predict_match_mle(fitted, 1, 2, rho=-0.10)
        self.assertGreater(
            corrected["result_probabilities"]["draw"],
            independent["result_probabilities"]["draw"],
        )
        self.assertEqual(
            corrected["model_parameters"]["dixon_coles_rho"],
            -0.10,
        )

    def test_ensemble_applies_rho_to_both_components(self):
        context = build_ensemble_context(FIXTURES)
        prediction = predict_match_ensemble(context, 1, 2)
        self.assertEqual(
            prediction["parameters"]["baseline_dixon_coles_rho"],
            -0.10,
        )
        self.assertEqual(
            prediction["parameters"]["mle_dixon_coles_rho"],
            -0.10,
        )
        self.assertEqual(
            prediction["component_predictions"]["baseline_poisson"]["model_parameters"]["dixon_coles_rho"],
            -0.10,
        )
        self.assertEqual(
            prediction["component_predictions"]["poisson_mle"]["model_parameters"]["dixon_coles_rho"],
            -0.10,
        )


if __name__ == "__main__":
    unittest.main()
