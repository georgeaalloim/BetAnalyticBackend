from __future__ import annotations

from typing import Any

from poisson_mle_model import (
    fit_poisson_mle_model,
    predict_match_mle,
)
from poisson_model import predict_match
from market_lines import (
    combine_total_market_lines,
    select_strongest_relevant_market,
)
from team_analysis import (
    calculate_home_away_statistics,
)
from value_analysis import analyze_1x2_value


RESULT_LABELS = (
    "HOME",
    "DRAW",
    "AWAY",
)

DEFAULT_BASELINE_WEIGHT = 0.60
DEFAULT_MLE_WEIGHT = 0.40

DEFAULT_PRIOR_MATCHES = 2.0
DEFAULT_DIXON_COLES_RHO = 0.0
DEFAULT_L2_REGULARIZATION = 2.0
DEFAULT_MAX_GOALS = 10


def validate_weights(
    baseline_weight: float,
    mle_weight: float,
) -> tuple[float, float]:
    """
    Ελέγχει και κανονικοποιεί τα βάρη
    του βασικού Poisson και του Poisson MLE.
    """

    baseline_value = float(
        baseline_weight
    )

    mle_value = float(
        mle_weight
    )

    if (
        baseline_value < 0
        or mle_value < 0
    ):
        raise ValueError(
            "Τα βάρη του ensemble δεν μπορούν "
            "να είναι αρνητικά."
        )

    total_weight = (
        baseline_value + mle_value
    )

    if total_weight <= 0:
        raise ValueError(
            "Το άθροισμα των βαρών πρέπει "
            "να είναι μεγαλύτερο από μηδέν."
        )

    return (
        baseline_value / total_weight,
        mle_value / total_weight,
    )


def normalize_probabilities(
    probabilities: dict[str, float],
) -> dict[str, float]:
    """
    Κανονικοποιεί πιθανότητες HOME/DRAW/AWAY
    ώστε να αθροίζουν ακριβώς σε 1.
    """

    missing_labels = [
        label
        for label in RESULT_LABELS
        if label not in probabilities
    ]

    if missing_labels:
        missing_text = ", ".join(
            missing_labels
        )

        raise ValueError(
            "Λείπουν πιθανότητες για: "
            f"{missing_text}."
        )

    probability_sum = sum(
        float(probabilities[label])
        for label in RESULT_LABELS
    )

    if probability_sum <= 0:
        raise ValueError(
            "Το άθροισμα των πιθανοτήτων πρέπει "
            "να είναι μεγαλύτερο από μηδέν."
        )

    return {
        label: (
            float(probabilities[label])
            / probability_sum
        )
        for label in RESULT_LABELS
    }


def extract_result_probabilities(
    prediction: dict[str, Any],
) -> dict[str, float]:
    """
    Διαβάζει τις δεκαδικές πιθανότητες 1-X-2
    από μία πρόβλεψη Poisson.
    """

    result_probabilities = prediction[
        "result_probabilities"
    ]

    extracted = {
        "HOME": float(
            result_probabilities[
                "home_win"
            ]
        ),
        "DRAW": float(
            result_probabilities[
                "draw"
            ]
        ),
        "AWAY": float(
            result_probabilities[
                "away_win"
            ]
        ),
    }

    return normalize_probabilities(
        probabilities=extracted,
    )


def extract_goals_probabilities(
    prediction: dict[str, Any],
) -> dict[str, float]:
    """
    Διαβάζει τις πιθανότητες Over/Under 2.5
    και Goal/No Goal από μία πρόβλεψη.
    """

    goals_probabilities = prediction[
        "goals_probabilities"
    ]

    return {
        "OVER_2_5": float(
            goals_probabilities[
                "over_2_5"
            ]
        ),
        "UNDER_2_5": float(
            goals_probabilities[
                "under_2_5"
            ]
        ),
        "BTTS_YES": float(
            goals_probabilities[
                "both_teams_score_yes"
            ]
        ),
        "BTTS_NO": float(
            goals_probabilities[
                "both_teams_score_no"
            ]
        ),
    }


def combine_result_probabilities(
    baseline_probabilities: dict[str, float],
    mle_probabilities: dict[str, float],
    baseline_weight: float,
    mle_weight: float,
) -> dict[str, float]:
    """
    Συνδυάζει γραμμικά τις πιθανότητες 1-X-2
    των δύο μοντέλων.
    """

    combined = {
        label: (
            baseline_weight
            * baseline_probabilities[label]
            + mle_weight
            * mle_probabilities[label]
        )
        for label in RESULT_LABELS
    }

    return normalize_probabilities(
        probabilities=combined,
    )


def normalize_binary_pair(
    first_probability: float,
    second_probability: float,
) -> tuple[float, float]:
    """
    Κανονικοποιεί ένα ζεύγος συμπληρωματικών
    πιθανοτήτων ώστε να αθροίζει σε 1.
    """

    total_probability = (
        first_probability
        + second_probability
    )

    if total_probability <= 0:
        raise ValueError(
            "Το άθροισμα του ζεύγους "
            "πιθανοτήτων πρέπει να είναι θετικό."
        )

    return (
        first_probability
        / total_probability,
        second_probability
        / total_probability,
    )


def combine_goals_probabilities(
    baseline_probabilities: dict[str, float],
    mle_probabilities: dict[str, float],
    baseline_weight: float,
    mle_weight: float,
) -> dict[str, float]:
    """
    Συνδυάζει τις αγορές γκολ των δύο μοντέλων.
    """

    over_2_5 = (
        baseline_weight
        * baseline_probabilities["OVER_2_5"]
        + mle_weight
        * mle_probabilities["OVER_2_5"]
    )

    under_2_5 = (
        baseline_weight
        * baseline_probabilities["UNDER_2_5"]
        + mle_weight
        * mle_probabilities["UNDER_2_5"]
    )

    btts_yes = (
        baseline_weight
        * baseline_probabilities["BTTS_YES"]
        + mle_weight
        * mle_probabilities["BTTS_YES"]
    )

    btts_no = (
        baseline_weight
        * baseline_probabilities["BTTS_NO"]
        + mle_weight
        * mle_probabilities["BTTS_NO"]
    )

    (
        over_2_5,
        under_2_5,
    ) = normalize_binary_pair(
        first_probability=over_2_5,
        second_probability=under_2_5,
    )

    (
        btts_yes,
        btts_no,
    ) = normalize_binary_pair(
        first_probability=btts_yes,
        second_probability=btts_no,
    )

    return {
        "OVER_2_5": over_2_5,
        "UNDER_2_5": under_2_5,
        "BTTS_YES": btts_yes,
        "BTTS_NO": btts_no,
    }


def build_ensemble_context(
    fixtures: list[dict[str, Any]],
    l2_regularization: float = (
        DEFAULT_L2_REGULARIZATION
    ),
) -> dict[str, Any]:
    """
    Δημιουργεί το κοινό ιστορικό περιβάλλον
    του ensemble.

    Η ακριβή εκτίμηση του MLE γίνεται εδώ μία φορά,
    ώστε το ίδιο fitted model να μπορεί να
    χρησιμοποιηθεί για πολλούς επόμενους αγώνες.
    """

    if not fixtures:
        raise ValueError(
            "Δεν δόθηκαν ολοκληρωμένοι αγώνες "
            "για την κατασκευή του ensemble."
        )

    completed_fixtures = [
        fixture
        for fixture in fixtures
        if (
            fixture.get("home_goals")
            is not None
            and fixture.get("away_goals")
            is not None
        )
    ]

    if not completed_fixtures:
        raise ValueError(
            "Δεν υπάρχουν ολοκληρωμένοι αγώνες "
            "με διαθέσιμο τελικό σκορ."
        )

    baseline_analysis = (
        calculate_home_away_statistics(
            completed_fixtures
        )
    )

    fitted_mle_model = (
        fit_poisson_mle_model(
            fixtures=completed_fixtures,
            l2_regularization=(
                l2_regularization
            ),
        )
    )

    return {
        "fixtures_used": len(
            completed_fixtures
        ),
        "baseline_analysis": (
            baseline_analysis
        ),
        "fitted_mle_model": (
            fitted_mle_model
        ),
        "parameters": {
            "baseline_prior_matches": (
                DEFAULT_PRIOR_MATCHES
            ),
            "baseline_dixon_coles_rho": (
                DEFAULT_DIXON_COLES_RHO
            ),
            "mle_l2_regularization": (
                l2_regularization
            ),
        },
    }


def predict_match_ensemble(
    context: dict[str, Any],
    home_team_id: int,
    away_team_id: int,
    baseline_weight: float = (
        DEFAULT_BASELINE_WEIGHT
    ),
    mle_weight: float = (
        DEFAULT_MLE_WEIGHT
    ),
    prior_matches: float = (
        DEFAULT_PRIOR_MATCHES
    ),
    dixon_coles_rho: float = (
        DEFAULT_DIXON_COLES_RHO
    ),
    max_goals: int = (
        DEFAULT_MAX_GOALS
    ),
) -> dict[str, Any]:
    """
    Παράγει την τελική πρόβλεψη ensemble:

        60% βασικό Poisson
        40% Poisson MLE

    Τα βάρη μπορούν να αλλάξουν ως παράμετροι,
    αλλά οι προεπιλεγμένες τιμές είναι εκείνες
    που επιλέχθηκαν από τις training seasons.
    """

    (
        normalized_baseline_weight,
        normalized_mle_weight,
    ) = validate_weights(
        baseline_weight=baseline_weight,
        mle_weight=mle_weight,
    )

    if home_team_id == away_team_id:
        raise ValueError(
            "Η γηπεδούχος και η φιλοξενούμενη "
            "ομάδα δεν μπορούν να είναι ίδιες."
        )

    if "baseline_analysis" not in context:
        raise ValueError(
            "Το ensemble context δεν περιέχει "
            "baseline_analysis."
        )

    if "fitted_mle_model" not in context:
        raise ValueError(
            "Το ensemble context δεν περιέχει "
            "fitted_mle_model."
        )

    baseline_prediction = predict_match(
        analysis=context[
            "baseline_analysis"
        ],
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        max_goals=max_goals,
        prior_matches=prior_matches,
        rho=dixon_coles_rho,
    )

    mle_prediction = predict_match_mle(
        fitted_model=context[
            "fitted_mle_model"
        ],
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        max_goals=max_goals,
    )

    baseline_result_probabilities = (
        extract_result_probabilities(
            prediction=baseline_prediction,
        )
    )

    mle_result_probabilities = (
        extract_result_probabilities(
            prediction=mle_prediction,
        )
    )

    ensemble_result_probabilities = (
        combine_result_probabilities(
            baseline_probabilities=(
                baseline_result_probabilities
            ),
            mle_probabilities=(
                mle_result_probabilities
            ),
            baseline_weight=(
                normalized_baseline_weight
            ),
            mle_weight=(
                normalized_mle_weight
            ),
        )
    )

    baseline_goals_probabilities = (
        extract_goals_probabilities(
            prediction=baseline_prediction,
        )
    )

    mle_goals_probabilities = (
        extract_goals_probabilities(
            prediction=mle_prediction,
        )
    )

    ensemble_goals_probabilities = (
        combine_goals_probabilities(
            baseline_probabilities=(
                baseline_goals_probabilities
            ),
            mle_probabilities=(
                mle_goals_probabilities
            ),
            baseline_weight=(
                normalized_baseline_weight
            ),
            mle_weight=(
                normalized_mle_weight
            ),
        )
    )

    baseline_expected_goals = (
        baseline_prediction[
            "expected_goals"
        ]
    )

    mle_expected_goals = (
        mle_prediction[
            "expected_goals"
        ]
    )

    expected_home_goals = (
        normalized_baseline_weight
        * float(
            baseline_expected_goals["home"]
        )
        + normalized_mle_weight
        * float(
            mle_expected_goals["home"]
        )
    )

    expected_away_goals = (
        normalized_baseline_weight
        * float(
            baseline_expected_goals["away"]
        )
        + normalized_mle_weight
        * float(
            mle_expected_goals["away"]
        )
    )

    ensemble_total_goals_lines = (
        combine_total_market_lines(
            baseline_lines=baseline_prediction[
                "total_goals_lines"
            ],
            mle_lines=mle_prediction[
                "total_goals_lines"
            ],
            baseline_weight=(
                normalized_baseline_weight
            ),
            mle_weight=normalized_mle_weight,
        )
    )

    selected_total_goals_market = (
        select_strongest_relevant_market(
            market_lines=(
                ensemble_total_goals_lines
            ),
            expected_total=(
                expected_home_goals
                + expected_away_goals
            ),
        )
    )

    predicted_result = max(
        RESULT_LABELS,
        key=lambda label: (
            ensemble_result_probabilities[
                label
            ]
        ),
    )

    return {
        "model": (
            "Probability Ensemble v0.5"
        ),
        "fixtures_used": context.get(
            "fixtures_used"
        ),
        "weights": {
            "baseline_poisson": round(
                normalized_baseline_weight,
                4,
            ),
            "poisson_mle": round(
                normalized_mle_weight,
                4,
            ),
        },
        "parameters": {
            "baseline_prior_matches": (
                prior_matches
            ),
            "baseline_dixon_coles_rho": (
                dixon_coles_rho
            ),
            "mle_l2_regularization": (
                context[
                    "fitted_mle_model"
                ][
                    "parameters"
                ][
                    "l2_regularization"
                ]
            ),
            "max_goals": max_goals,
        },
        "home_team": (
            baseline_prediction["home_team"]
        ),
        "away_team": (
            baseline_prediction["away_team"]
        ),
        "predicted_result": (
            predicted_result
        ),
        "expected_goals": {
            "home": round(
                expected_home_goals,
                3,
            ),
            "away": round(
                expected_away_goals,
                3,
            ),
            "total": round(
                expected_home_goals
                + expected_away_goals,
                3,
            ),
        },
        "result_probabilities": {
            "home_win": round(
                ensemble_result_probabilities[
                    "HOME"
                ],
                8,
            ),
            "draw": round(
                ensemble_result_probabilities[
                    "DRAW"
                ],
                8,
            ),
            "away_win": round(
                ensemble_result_probabilities[
                    "AWAY"
                ],
                8,
            ),
            "home_win_percent": round(
                ensemble_result_probabilities[
                    "HOME"
                ]
                * 100.0,
                2,
            ),
            "draw_percent": round(
                ensemble_result_probabilities[
                    "DRAW"
                ]
                * 100.0,
                2,
            ),
            "away_win_percent": round(
                ensemble_result_probabilities[
                    "AWAY"
                ]
                * 100.0,
                2,
            ),
        },
        "total_goals_market": {
            "selected": (
                selected_total_goals_market
            ),
            "all_lines": (
                ensemble_total_goals_lines
            ),
        },
        "goals_probabilities": {
            "over_2_5": round(
                ensemble_goals_probabilities[
                    "OVER_2_5"
                ],
                8,
            ),
            "under_2_5": round(
                ensemble_goals_probabilities[
                    "UNDER_2_5"
                ],
                8,
            ),
            "both_teams_score_yes": round(
                ensemble_goals_probabilities[
                    "BTTS_YES"
                ],
                8,
            ),
            "both_teams_score_no": round(
                ensemble_goals_probabilities[
                    "BTTS_NO"
                ],
                8,
            ),
            "over_2_5_percent": round(
                ensemble_goals_probabilities[
                    "OVER_2_5"
                ]
                * 100.0,
                2,
            ),
            "under_2_5_percent": round(
                ensemble_goals_probabilities[
                    "UNDER_2_5"
                ]
                * 100.0,
                2,
            ),
            "both_teams_score_yes_percent": round(
                ensemble_goals_probabilities[
                    "BTTS_YES"
                ]
                * 100.0,
                2,
            ),
            "both_teams_score_no_percent": round(
                ensemble_goals_probabilities[
                    "BTTS_NO"
                ]
                * 100.0,
                2,
            ),
        },
        "component_predictions": {
            "baseline_poisson": {
                "model": (
                    baseline_prediction[
                        "model"
                    ]
                ),
                "result_probabilities": (
                    baseline_prediction[
                        "result_probabilities"
                    ]
                ),
                "expected_goals": (
                    baseline_prediction[
                        "expected_goals"
                    ]
                ),
                "total_goals_lines": (
                    baseline_prediction[
                        "total_goals_lines"
                    ]
                ),
            },
            "poisson_mle": {
                "model": (
                    mle_prediction["model"]
                ),
                "result_probabilities": (
                    mle_prediction[
                        "result_probabilities"
                    ]
                ),
                "expected_goals": (
                    mle_prediction[
                        "expected_goals"
                    ]
                ),
                "total_goals_lines": (
                    mle_prediction[
                        "total_goals_lines"
                    ]
                ),
            },
        },
    }


def analyze_match_value_1x2(
    context: dict[str, Any],
    home_team_id: int,
    away_team_id: int,
    home_odds: float,
    draw_odds: float,
    away_odds: float,
    min_edge_percent: float = 3.0,
    min_expected_value_percent: float = 3.0,
    kelly_multiplier: float = 0.25,
    max_bankroll_fraction: float = 0.02,
) -> dict[str, Any]:
    """
    Παράγει ensemble πρόβλεψη και αμέσως μετά
    αναλύει το value στις αποδόσεις 1-X-2.
    """

    ensemble_prediction = (
        predict_match_ensemble(
            context=context,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    )

    result_probabilities = (
        ensemble_prediction[
            "result_probabilities"
        ]
    )

    model_probabilities = {
        "HOME": float(
            result_probabilities[
                "home_win"
            ]
        ),
        "DRAW": float(
            result_probabilities[
                "draw"
            ]
        ),
        "AWAY": float(
            result_probabilities[
                "away_win"
            ]
        ),
    }

    decimal_odds = {
        "HOME": home_odds,
        "DRAW": draw_odds,
        "AWAY": away_odds,
    }

    value_analysis = analyze_1x2_value(
        model_probabilities=(
            model_probabilities
        ),
        decimal_odds=decimal_odds,
        min_edge_percent=min_edge_percent,
        min_expected_value_percent=(
            min_expected_value_percent
        ),
        kelly_multiplier=kelly_multiplier,
        max_bankroll_fraction=(
            max_bankroll_fraction
        ),
    )

    return {
        "match": {
            "home_team": (
                ensemble_prediction[
                    "home_team"
                ]
            ),
            "away_team": (
                ensemble_prediction[
                    "away_team"
                ]
            ),
        },
        "ensemble_prediction": (
            ensemble_prediction
        ),
        "value_analysis_1x2": (
            value_analysis
        ),
    }
