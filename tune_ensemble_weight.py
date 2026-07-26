from __future__ import annotations

import json
from math import log
from typing import Any

from database import get_completed_fixtures
from poisson_mle_model import (
    fit_poisson_mle_model,
    predict_match_mle,
)
from poisson_model import predict_match
from team_analysis import calculate_home_away_statistics


LEAGUE_ID = 197

TRAINING_SEASONS = (
    2022,
    2023,
)

VALIDATION_SEASON = 2024

MIN_PREVIOUS_LOCATION_MATCHES = 5

BASELINE_PRIOR_MATCHES = 2.0
BASELINE_DIXON_COLES_RHO = 0.0

MLE_L2_REGULARIZATION = 2.0
MLE_REFIT_INTERVAL = 7

# Η τιμή είναι το βάρος του βασικού Poisson.
# Το βάρος του MLE είναι πάντα 1 - baseline_weight.
BASELINE_WEIGHTS = (
    0.0,
    0.1,
    0.2,
    0.3,
    0.4,
    0.5,
    0.6,
    0.7,
    0.8,
    0.9,
    1.0,
)

RESULT_LABELS = (
    "HOME",
    "DRAW",
    "AWAY",
)


def get_actual_result(
    home_goals: int,
    away_goals: int,
) -> str:
    """Μετατρέπει το τελικό σκορ σε HOME, DRAW ή AWAY."""

    if home_goals > away_goals:
        return "HOME"

    if home_goals < away_goals:
        return "AWAY"

    return "DRAW"


def count_previous_location_matches(
    fixtures: list[dict[str, Any]],
    home_team_id: int,
    away_team_id: int,
) -> tuple[int, int]:
    """
    Μετρά τους προηγούμενους εντός έδρας αγώνες
    του γηπεδούχου και τους προηγούμενους εκτός
    έδρας αγώνες του φιλοξενούμενου.
    """

    home_matches = sum(
        1
        for fixture in fixtures
        if int(fixture["home_team_id"])
        == home_team_id
    )

    away_matches = sum(
        1
        for fixture in fixtures
        if int(fixture["away_team_id"])
        == away_team_id
    )

    return home_matches, away_matches


def normalize_probabilities(
    probabilities: dict[str, float],
) -> dict[str, float]:
    """Κανονικοποιεί τις πιθανότητες ώστε να αθροίζουν σε 1."""

    total_probability = sum(
        probabilities.values()
    )

    if total_probability <= 0:
        raise ValueError(
            "Το άθροισμα των πιθανοτήτων πρέπει "
            "να είναι μεγαλύτερο από μηδέν."
        )

    return {
        label: (
            probabilities[label]
            / total_probability
        )
        for label in RESULT_LABELS
    }


def extract_baseline_probabilities(
    prediction: dict[str, Any],
) -> dict[str, float]:
    """Διαβάζει τις πιθανότητες 1-X-2 του βασικού Poisson."""

    result_probabilities = prediction[
        "result_probabilities"
    ]

    has_raw_probabilities = all(
        key in result_probabilities
        for key in (
            "home_win",
            "draw",
            "away_win",
        )
    )

    if has_raw_probabilities:
        probabilities = {
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

    else:
        probabilities = {
            "HOME": (
                float(
                    result_probabilities[
                        "home_win_percent"
                    ]
                )
                / 100
            ),
            "DRAW": (
                float(
                    result_probabilities[
                        "draw_percent"
                    ]
                )
                / 100
            ),
            "AWAY": (
                float(
                    result_probabilities[
                        "away_win_percent"
                    ]
                )
                / 100
            ),
        }

    return normalize_probabilities(
        probabilities
    )


def extract_mle_probabilities(
    prediction: dict[str, Any],
) -> dict[str, float]:
    """Διαβάζει τις πιθανότητες 1-X-2 του Poisson MLE."""

    result_probabilities = prediction[
        "result_probabilities"
    ]

    probabilities = {
        "HOME": float(
            result_probabilities["home_win"]
        ),
        "DRAW": float(
            result_probabilities["draw"]
        ),
        "AWAY": float(
            result_probabilities["away_win"]
        ),
    }

    return normalize_probabilities(
        probabilities
    )


def blend_probabilities(
    baseline_probabilities: dict[str, float],
    mle_probabilities: dict[str, float],
    baseline_weight: float,
) -> dict[str, float]:
    """
    Συνδυάζει γραμμικά τις πιθανότητες των δύο μοντέλων.

    baseline_weight = 1.0:
        Χρησιμοποιείται μόνο το βασικό Poisson.

    baseline_weight = 0.0:
        Χρησιμοποιείται μόνο το Poisson MLE.

    baseline_weight = 0.5:
        Τα δύο μοντέλα έχουν ίσο βάρος.
    """

    if not 0 <= baseline_weight <= 1:
        raise ValueError(
            "Το baseline_weight πρέπει "
            "να βρίσκεται από 0 έως 1."
        )

    mle_weight = 1.0 - baseline_weight

    blended = {
        label: (
            baseline_weight
            * baseline_probabilities[label]
            + mle_weight
            * mle_probabilities[label]
        )
        for label in RESULT_LABELS
    }

    return normalize_probabilities(
        blended
    )


def calculate_brier_score(
    probabilities: dict[str, float],
    actual_result: str,
) -> float:
    """Υπολογίζει multiclass Brier Score για 1-X-2."""

    return sum(
        (
            probabilities[label]
            - (
                1.0
                if actual_result == label
                else 0.0
            )
        )
        ** 2
        for label in RESULT_LABELS
    )


def calculate_class_metrics(
    confusion_matrix: dict[
        str,
        dict[str, int],
    ],
) -> dict[str, dict[str, float | int]]:
    """Υπολογίζει Precision, Recall και F1 για κάθε έκβαση."""

    class_metrics: dict[
        str,
        dict[str, float | int],
    ] = {}

    for label in RESULT_LABELS:
        true_positives = (
            confusion_matrix[label][label]
        )

        actual_total = sum(
            confusion_matrix[label].values()
        )

        predicted_total = sum(
            confusion_matrix[
                actual_label
            ][label]
            for actual_label in RESULT_LABELS
        )

        recall = (
            true_positives / actual_total
            if actual_total > 0
            else 0.0
        )

        precision = (
            true_positives / predicted_total
            if predicted_total > 0
            else 0.0
        )

        f1_score = (
            2 * precision * recall
            / (precision + recall)
            if precision + recall > 0
            else 0.0
        )

        class_metrics[label] = {
            "actual_matches": actual_total,
            "predicted_matches": (
                predicted_total
            ),
            "correct": true_positives,
            "recall_percent": round(
                recall * 100,
                2,
            ),
            "precision_percent": round(
                precision * 100,
                2,
            ),
            "f1_score_percent": round(
                f1_score * 100,
                2,
            ),
        }

    return class_metrics


def collect_paired_predictions(
    fixtures: list[dict[str, Any]],
    season: int,
) -> dict[str, Any]:
    """
    Παράγει τις προβλέψεις και των δύο μοντέλων
    στους ακριβώς ίδιους ιστορικούς αγώνες.

    Οι προβλέψεις παράγονται μία φορά και μετά
    επαναχρησιμοποιούνται για όλα τα ensemble weights.
    """

    sorted_fixtures = sorted(
        fixtures,
        key=lambda fixture: str(
            fixture.get("fixture_date") or ""
        ),
    )

    paired_predictions: list[
        dict[str, Any]
    ] = []

    skipped_insufficient_history = 0
    skipped_prediction_failure = 0

    fitted_mle_model: (
        dict[str, Any] | None
    ) = None

    mle_evaluations_since_refit = (
        MLE_REFIT_INTERVAL
    )

    mle_refits = 0

    for current_index, current_fixture in enumerate(
        sorted_fixtures
    ):
        previous_fixtures = sorted_fixtures[
            :current_index
        ]

        home_team_id = int(
            current_fixture["home_team_id"]
        )

        away_team_id = int(
            current_fixture["away_team_id"]
        )

        home_history, away_history = (
            count_previous_location_matches(
                fixtures=previous_fixtures,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )
        )

        if (
            home_history
            < MIN_PREVIOUS_LOCATION_MATCHES
            or away_history
            < MIN_PREVIOUS_LOCATION_MATCHES
        ):
            skipped_insufficient_history += 1
            continue

        try:
            baseline_analysis = (
                calculate_home_away_statistics(
                    previous_fixtures
                )
            )

            baseline_prediction = predict_match(
                analysis=baseline_analysis,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                prior_matches=(
                    BASELINE_PRIOR_MATCHES
                ),
                rho=(
                    BASELINE_DIXON_COLES_RHO
                ),
            )

            baseline_probabilities = (
                extract_baseline_probabilities(
                    baseline_prediction
                )
            )

            fitted_team_ids = (
                {
                    int(team["team_id"])
                    for team in fitted_mle_model[
                        "teams"
                    ]
                }
                if fitted_mle_model is not None
                else set()
            )

            must_refit_mle = (
                fitted_mle_model is None
                or mle_evaluations_since_refit
                >= MLE_REFIT_INTERVAL
                or home_team_id
                not in fitted_team_ids
                or away_team_id
                not in fitted_team_ids
            )

            if must_refit_mle:
                fitted_mle_model = (
                    fit_poisson_mle_model(
                        previous_fixtures,
                        l2_regularization=(
                            MLE_L2_REGULARIZATION
                        ),
                    )
                )

                mle_refits += 1
                mle_evaluations_since_refit = 0

            if fitted_mle_model is None:
                raise ValueError(
                    "Δεν δημιουργήθηκε MLE μοντέλο."
                )

            mle_prediction = predict_match_mle(
                fitted_model=fitted_mle_model,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )

            mle_probabilities = (
                extract_mle_probabilities(
                    mle_prediction
                )
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            skipped_prediction_failure += 1
            continue

        actual_result = get_actual_result(
            home_goals=int(
                current_fixture["home_goals"]
            ),
            away_goals=int(
                current_fixture["away_goals"]
            ),
        )

        paired_predictions.append(
            {
                "fixture_date": (
                    current_fixture[
                        "fixture_date"
                    ]
                ),
                "home_team": (
                    current_fixture[
                        "home_team_name"
                    ]
                ),
                "away_team": (
                    current_fixture[
                        "away_team_name"
                    ]
                ),
                "actual_score": (
                    f"{current_fixture['home_goals']}"
                    f"-"
                    f"{current_fixture['away_goals']}"
                ),
                "actual_result": actual_result,
                "baseline_probabilities": (
                    baseline_probabilities
                ),
                "mle_probabilities": (
                    mle_probabilities
                ),
            }
        )

        mle_evaluations_since_refit += 1

    if not paired_predictions:
        raise ValueError(
            f"Δεν δημιουργήθηκαν κοινές προβλέψεις "
            f"για τη σεζόν {season}."
        )

    return {
        "season": season,
        "total_fixtures": len(
            sorted_fixtures
        ),
        "common_evaluated_matches": len(
            paired_predictions
        ),
        "skipped_insufficient_history": (
            skipped_insufficient_history
        ),
        "skipped_prediction_failure": (
            skipped_prediction_failure
        ),
        "mle_refits": mle_refits,
        "predictions": paired_predictions,
    }


def evaluate_ensemble_weight(
    paired_predictions: list[dict[str, Any]],
    baseline_weight: float,
) -> dict[str, Any]:
    """Αξιολογεί ένα συγκεκριμένο ensemble weight."""

    correct_predictions = 0
    total_log_loss = 0.0
    total_brier_score = 0.0

    predicted_result_counts = {
        label: 0
        for label in RESULT_LABELS
    }

    confusion_matrix = {
        actual: {
            predicted: 0
            for predicted in RESULT_LABELS
        }
        for actual in RESULT_LABELS
    }

    example_predictions: list[
        dict[str, Any]
    ] = []

    for item in paired_predictions:
        actual_result = str(
            item["actual_result"]
        )

        ensemble_probabilities = (
            blend_probabilities(
                baseline_probabilities=(
                    item[
                        "baseline_probabilities"
                    ]
                ),
                mle_probabilities=(
                    item[
                        "mle_probabilities"
                    ]
                ),
                baseline_weight=(
                    baseline_weight
                ),
            )
        )

        predicted_result = max(
            ensemble_probabilities,
            key=lambda label: (
                ensemble_probabilities[label]
            ),
        )

        predicted_result_counts[
            predicted_result
        ] += 1

        confusion_matrix[
            actual_result
        ][
            predicted_result
        ] += 1

        if predicted_result == actual_result:
            correct_predictions += 1

        actual_probability = max(
            ensemble_probabilities[
                actual_result
            ],
            1e-15,
        )

        total_log_loss += -log(
            actual_probability
        )

        total_brier_score += (
            calculate_brier_score(
                probabilities=(
                    ensemble_probabilities
                ),
                actual_result=actual_result,
            )
        )

        if len(example_predictions) < 5:
            example_predictions.append(
                {
                    "fixture_date": item[
                        "fixture_date"
                    ],
                    "home_team": item[
                        "home_team"
                    ],
                    "away_team": item[
                        "away_team"
                    ],
                    "actual_score": item[
                        "actual_score"
                    ],
                    "actual_result": (
                        actual_result
                    ),
                    "predicted_result": (
                        predicted_result
                    ),
                    "ensemble_probabilities_percent": {
                        label: round(
                            ensemble_probabilities[
                                label
                            ]
                            * 100,
                            2,
                        )
                        for label in RESULT_LABELS
                    },
                }
            )

    evaluated_matches = len(
        paired_predictions
    )

    class_metrics = calculate_class_metrics(
        confusion_matrix=confusion_matrix,
    )

    balanced_accuracy_percent = sum(
        float(
            class_metrics[label][
                "recall_percent"
            ]
        )
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)

    macro_f1_percent = sum(
        float(
            class_metrics[label][
                "f1_score_percent"
            ]
        )
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)

    return {
        "evaluated_matches": evaluated_matches,
        "correct_predictions": (
            correct_predictions
        ),
        "accuracy_percent": round(
            correct_predictions
            / evaluated_matches
            * 100,
            2,
        ),
        "balanced_accuracy_percent": round(
            balanced_accuracy_percent,
            2,
        ),
        "macro_f1_percent": round(
            macro_f1_percent,
            2,
        ),
        "average_log_loss": round(
            total_log_loss
            / evaluated_matches,
            6,
        ),
        "average_brier_score": round(
            total_brier_score
            / evaluated_matches,
            6,
        ),
        "predicted_result_counts": (
            predicted_result_counts
        ),
        "confusion_matrix": (
            confusion_matrix
        ),
        "class_metrics": class_metrics,
        "example_predictions": (
            example_predictions
        ),
    }


def summarize_seasons(
    season_metrics: dict[
        int,
        dict[str, Any],
    ],
) -> dict[str, float | int]:
    """Δημιουργεί σταθμισμένη σύνοψη πολλών σεζόν."""

    total_matches = sum(
        int(
            metrics[
                "evaluated_matches"
            ]
        )
        for metrics in season_metrics.values()
    )

    total_correct = sum(
        int(
            metrics[
                "correct_predictions"
            ]
        )
        for metrics in season_metrics.values()
    )

    if total_matches <= 0:
        raise ValueError(
            "Δεν υπάρχουν αξιολογημένοι αγώνες."
        )

    def weighted_average(
        metric_name: str,
    ) -> float:
        return sum(
            float(metrics[metric_name])
            * int(
                metrics[
                    "evaluated_matches"
                ]
            )
            for metrics in season_metrics.values()
        ) / total_matches

    return {
        "evaluated_matches": total_matches,
        "correct_predictions": total_correct,
        "accuracy_percent": round(
            total_correct
            / total_matches
            * 100,
            2,
        ),
        "balanced_accuracy_percent": round(
            weighted_average(
                "balanced_accuracy_percent"
            ),
            2,
        ),
        "macro_f1_percent": round(
            weighted_average(
                "macro_f1_percent"
            ),
            2,
        ),
        "average_log_loss": round(
            weighted_average(
                "average_log_loss"
            ),
            6,
        ),
        "average_brier_score": round(
            weighted_average(
                "average_brier_score"
            ),
            6,
        ),
    }


def load_season_fixtures(
    season: int,
) -> list[dict[str, Any]]:
    """Διαβάζει τους αγώνες μιας σεζόν από τη SQLite."""

    fixtures = get_completed_fixtures(
        league_id=LEAGUE_ID,
        season=season,
    )

    if not fixtures:
        raise ValueError(
            f"Δεν βρέθηκαν αγώνες "
            f"για τη σεζόν {season}."
        )

    return fixtures


def run_tuning() -> dict[str, Any]:
    """
    Επιλέγει το βάρος ensemble μόνο από τις σεζόν
    2022 και 2023 και ελέγχει τη γενίκευση στο 2024.
    """

    all_seasons = (
        *TRAINING_SEASONS,
        VALIDATION_SEASON,
    )

    paired_by_season: dict[
        int,
        dict[str, Any],
    ] = {}

    for season in all_seasons:
        print(
            f"Παραγωγή κοινών προβλέψεων "
            f"για τη σεζόν {season}...",
            flush=True,
        )

        fixtures = load_season_fixtures(
            season
        )

        paired_by_season[season] = (
            collect_paired_predictions(
                fixtures=fixtures,
                season=season,
            )
        )

    candidates: list[dict[str, Any]] = []

    for baseline_weight in BASELINE_WEIGHTS:
        mle_weight = 1.0 - baseline_weight

        print(
            "Δοκιμή ensemble: "
            f"baseline_weight={baseline_weight:.1f}, "
            f"mle_weight={mle_weight:.1f}...",
            flush=True,
        )

        training_season_metrics: dict[
            int,
            dict[str, Any],
        ] = {}

        for season in TRAINING_SEASONS:
            training_season_metrics[season] = (
                evaluate_ensemble_weight(
                    paired_predictions=(
                        paired_by_season[
                            season
                        ]["predictions"]
                    ),
                    baseline_weight=(
                        baseline_weight
                    ),
                )
            )

        training_summary = (
            summarize_seasons(
                training_season_metrics
            )
        )

        validation_metrics = (
            evaluate_ensemble_weight(
                paired_predictions=(
                    paired_by_season[
                        VALIDATION_SEASON
                    ]["predictions"]
                ),
                baseline_weight=(
                    baseline_weight
                ),
            )
        )

        candidates.append(
            {
                "baseline_weight": (
                    baseline_weight
                ),
                "mle_weight": mle_weight,
                "training_seasons": {
                    str(season): (
                        training_season_metrics[
                            season
                        ]
                    )
                    for season in TRAINING_SEASONS
                },
                "training_summary": (
                    training_summary
                ),
                "validation_season": (
                    VALIDATION_SEASON
                ),
                "validation_metrics": (
                    validation_metrics
                ),
            }
        )

    candidates.sort(
        key=lambda candidate: (
            float(
                candidate[
                    "training_summary"
                ]["average_log_loss"]
            ),
            float(
                candidate[
                    "training_summary"
                ]["average_brier_score"]
            ),
        )
    )

    best_candidate = candidates[0]

    pure_baseline_candidate = next(
        candidate
        for candidate in candidates
        if float(
            candidate["baseline_weight"]
        )
        == 1.0
    )

    pure_mle_candidate = next(
        candidate
        for candidate in candidates
        if float(
            candidate["baseline_weight"]
        )
        == 0.0
    )

    return {
        "league_id": LEAGUE_ID,
        "model": (
            "Linear probability ensemble "
            "of Poisson v0.3 and Poisson MLE v0.4"
        ),
        "training_seasons": list(
            TRAINING_SEASONS
        ),
        "validation_season": (
            VALIDATION_SEASON
        ),
        "common_sample_rule": {
            "minimum_previous_home_matches_for_home_team": (
                MIN_PREVIOUS_LOCATION_MATCHES
            ),
            "minimum_previous_away_matches_for_away_team": (
                MIN_PREVIOUS_LOCATION_MATCHES
            ),
            "both_models_must_produce_prediction": True,
        },
        "fixed_model_parameters": {
            "baseline_prior_matches": (
                BASELINE_PRIOR_MATCHES
            ),
            "baseline_dixon_coles_rho": (
                BASELINE_DIXON_COLES_RHO
            ),
            "mle_l2_regularization": (
                MLE_L2_REGULARIZATION
            ),
            "mle_refit_interval": (
                MLE_REFIT_INTERVAL
            ),
        },
        "tested_baseline_weights": list(
            BASELINE_WEIGHTS
        ),
        "selection_rule": (
            "Χαμηλότερο σταθμισμένο Log Loss "
            "στις σεζόν 2022 και 2023. "
            "Σε ισοβαθμία χρησιμοποιείται το "
            "χαμηλότερο Brier Score. Η σεζόν 2024 "
            "χρησιμοποιείται μόνο για validation."
        ),
        "recommended_weights": {
            "baseline_weight": (
                best_candidate[
                    "baseline_weight"
                ]
            ),
            "mle_weight": (
                best_candidate[
                    "mle_weight"
                ]
            ),
        },
        "reference_models": {
            "pure_baseline": {
                "training_summary": (
                    pure_baseline_candidate[
                        "training_summary"
                    ]
                ),
                "validation_metrics": (
                    pure_baseline_candidate[
                        "validation_metrics"
                    ]
                ),
            },
            "pure_mle": {
                "training_summary": (
                    pure_mle_candidate[
                        "training_summary"
                    ]
                ),
                "validation_metrics": (
                    pure_mle_candidate[
                        "validation_metrics"
                    ]
                ),
            },
        },
        "paired_data_by_season": {
            str(season): {
                key: value
                for key, value
                in paired_by_season[
                    season
                ].items()
                if key != "predictions"
            }
            for season in all_seasons
        },
        "candidates_ranked": candidates,
    }


def main() -> None:
    """Εκτελεί το tuning και αποθηκεύει τα αποτελέσματα."""

    try:
        results = run_tuning()

    except ValueError as error:
        print(
            f"Σφάλμα: {error}"
        )
        return

    output_path = (
        "ensemble_weight_tuning_results.json"
    )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as output_file:
        json.dump(
            results,
            output_file,
            ensure_ascii=False,
            indent=2,
        )

    recommended = results[
        "recommended_weights"
    ]

    print()
    print("Η διαδικασία ολοκληρώθηκε.")
    print(
        "Προτεινόμενο βάρος βασικού Poisson:",
        recommended["baseline_weight"],
    )
    print(
        "Προτεινόμενο βάρος Poisson MLE:",
        recommended["mle_weight"],
    )
    print(
        "Τα πλήρη αποτελέσματα αποθηκεύτηκαν στο:",
        output_path,
    )


if __name__ == "__main__":
    main()
