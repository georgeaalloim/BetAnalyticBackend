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
TRAINING_SEASONS = (2022, 2023)
VALIDATION_SEASON = 2024

MIN_PREVIOUS_LOCATION_MATCHES = 5

BASELINE_PRIOR_MATCHES = 2.0
BASELINE_DIXON_COLES_RHO = 0.0

MLE_L2_REGULARIZATION = 2.0
MLE_REFIT_INTERVAL = 7

RESULT_LABELS = ("HOME", "DRAW", "AWAY")


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
        if int(fixture["home_team_id"]) == home_team_id
    )

    away_matches = sum(
        1
        for fixture in fixtures
        if int(fixture["away_team_id"]) == away_team_id
    )

    return home_matches, away_matches


def normalize_probabilities(
    probabilities: dict[str, float],
) -> dict[str, float]:
    """Κανονικοποιεί τις πιθανότητες ώστε να αθροίζουν σε 1."""

    total_probability = sum(probabilities.values())

    if total_probability <= 0:
        raise ValueError(
            "Το άθροισμα των πιθανοτήτων πρέπει "
            "να είναι μεγαλύτερο από μηδέν."
        )

    return {
        label: probabilities[label] / total_probability
        for label in RESULT_LABELS
    }


def extract_baseline_probabilities(
    prediction: dict[str, Any],
) -> dict[str, float]:
    """Διαβάζει τις πιθανότητες 1-X-2 του βασικού Poisson."""

    result_probabilities = prediction[
        "result_probabilities"
    ]

    if all(
        key in result_probabilities
        for key in (
            "home_win",
            "draw",
            "away_win",
        )
    ):
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

    return normalize_probabilities(probabilities)


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

    return normalize_probabilities(probabilities)


def calculate_brier_score(
    probabilities: dict[str, float],
    actual_result: str,
) -> float:
    """Υπολογίζει multiclass Brier Score για HOME/DRAW/AWAY."""

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


def create_model_accumulator() -> dict[str, Any]:
    """Δημιουργεί κενό συσσωρευτή μετρικών ενός μοντέλου."""

    return {
        "correct_predictions": 0,
        "total_log_loss": 0.0,
        "total_brier_score": 0.0,
        "predicted_result_counts": {
            label: 0
            for label in RESULT_LABELS
        },
        "confusion_matrix": {
            actual: {
                predicted: 0
                for predicted in RESULT_LABELS
            }
            for actual in RESULT_LABELS
        },
    }


def update_model_accumulator(
    accumulator: dict[str, Any],
    probabilities: dict[str, float],
    actual_result: str,
) -> tuple[float, float, str]:
    """Ενημερώνει τις μετρικές ενός μοντέλου για έναν αγώνα."""

    predicted_result = max(
        probabilities,
        key=lambda label: probabilities[label],
    )

    log_loss = -log(
        max(probabilities[actual_result], 1e-15)
    )

    brier_score = calculate_brier_score(
        probabilities=probabilities,
        actual_result=actual_result,
    )

    accumulator["total_log_loss"] += log_loss
    accumulator["total_brier_score"] += brier_score

    accumulator[
        "predicted_result_counts"
    ][predicted_result] += 1

    accumulator[
        "confusion_matrix"
    ][actual_result][predicted_result] += 1

    if predicted_result == actual_result:
        accumulator["correct_predictions"] += 1

    return log_loss, brier_score, predicted_result


def calculate_class_metrics(
    confusion_matrix: dict[str, dict[str, int]],
) -> dict[str, dict[str, float | int]]:
    """Υπολογίζει Precision, Recall και F1 για κάθε έκβαση."""

    class_metrics: dict[
        str,
        dict[str, float | int],
    ] = {}

    for label in RESULT_LABELS:
        true_positives = confusion_matrix[label][label]

        actual_total = sum(
            confusion_matrix[label].values()
        )

        predicted_total = sum(
            confusion_matrix[actual][label]
            for actual in RESULT_LABELS
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
            "predicted_matches": predicted_total,
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


def finalize_model_metrics(
    accumulator: dict[str, Any],
    evaluated_matches: int,
) -> dict[str, Any]:
    """Μετατρέπει τους συσσωρευτές σε τελικές μέσες μετρικές."""

    if evaluated_matches <= 0:
        raise ValueError(
            "Δεν υπάρχουν κοινοί αγώνες προς αξιολόγηση."
        )

    class_metrics = calculate_class_metrics(
        confusion_matrix=accumulator[
            "confusion_matrix"
        ],
    )

    balanced_accuracy = sum(
        float(
            class_metrics[label][
                "recall_percent"
            ]
        )
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)

    macro_f1 = sum(
        float(
            class_metrics[label][
                "f1_score_percent"
            ]
        )
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)

    correct_predictions = int(
        accumulator["correct_predictions"]
    )

    return {
        "evaluated_matches": evaluated_matches,
        "correct_predictions": correct_predictions,
        "incorrect_predictions": (
            evaluated_matches - correct_predictions
        ),
        "accuracy_percent": round(
            correct_predictions
            / evaluated_matches
            * 100,
            2,
        ),
        "balanced_accuracy_percent": round(
            balanced_accuracy,
            2,
        ),
        "macro_f1_percent": round(
            macro_f1,
            2,
        ),
        "average_log_loss": round(
            float(accumulator["total_log_loss"])
            / evaluated_matches,
            4,
        ),
        "average_brier_score": round(
            float(
                accumulator[
                    "total_brier_score"
                ]
            )
            / evaluated_matches,
            4,
        ),
        "predicted_result_counts": accumulator[
            "predicted_result_counts"
        ],
        "confusion_matrix": accumulator[
            "confusion_matrix"
        ],
        "class_metrics": class_metrics,
    }


def compare_models_on_season(
    fixtures: list[dict[str, Any]],
    season: int,
) -> dict[str, Any]:
    """
    Συγκρίνει τα δύο μοντέλα στους ακριβώς ίδιους αγώνες.

    Ένας αγώνας αξιολογείται μόνο όταν:
    - ο γηπεδούχος έχει τουλάχιστον 5 προηγούμενους
      εντός έδρας αγώνες,
    - ο φιλοξενούμενος έχει τουλάχιστον 5 προηγούμενους
      εκτός έδρας αγώνες,
    - και τα δύο μοντέλα παράγουν επιτυχώς πρόβλεψη.
    """

    sorted_fixtures = sorted(
        fixtures,
        key=lambda fixture: str(
            fixture.get("fixture_date") or ""
        ),
    )

    baseline_accumulator = (
        create_model_accumulator()
    )

    mle_accumulator = create_model_accumulator()

    actual_result_counts = {
        label: 0
        for label in RESULT_LABELS
    }

    evaluated_matches = 0
    skipped_insufficient_history = 0
    skipped_prediction_failure = 0

    baseline_better_log_loss = 0
    mle_better_log_loss = 0
    equal_log_loss = 0

    baseline_better_brier = 0
    mle_better_brier = 0
    equal_brier = 0

    same_top_prediction = 0
    different_top_prediction = 0

    example_disagreements: list[
        dict[str, Any]
    ] = []

    fitted_mle_model: dict[str, Any] | None = None
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

        actual_result_counts[actual_result] += 1
        evaluated_matches += 1
        mle_evaluations_since_refit += 1

        (
            baseline_log_loss,
            baseline_brier,
            baseline_predicted_result,
        ) = update_model_accumulator(
            accumulator=baseline_accumulator,
            probabilities=baseline_probabilities,
            actual_result=actual_result,
        )

        (
            mle_log_loss,
            mle_brier,
            mle_predicted_result,
        ) = update_model_accumulator(
            accumulator=mle_accumulator,
            probabilities=mle_probabilities,
            actual_result=actual_result,
        )

        tolerance = 1e-12

        if (
            baseline_log_loss
            < mle_log_loss - tolerance
        ):
            baseline_better_log_loss += 1

        elif mle_log_loss < (
            baseline_log_loss - tolerance
        ):
            mle_better_log_loss += 1

        else:
            equal_log_loss += 1

        if baseline_brier < (
            mle_brier - tolerance
        ):
            baseline_better_brier += 1

        elif mle_brier < (
            baseline_brier - tolerance
        ):
            mle_better_brier += 1

        else:
            equal_brier += 1

        if (
            baseline_predicted_result
            == mle_predicted_result
        ):
            same_top_prediction += 1

        else:
            different_top_prediction += 1

            if len(example_disagreements) < 15:
                example_disagreements.append(
                    {
                        "fixture_date": current_fixture[
                            "fixture_date"
                        ],
                        "home_team": current_fixture[
                            "home_team_name"
                        ],
                        "away_team": current_fixture[
                            "away_team_name"
                        ],
                        "actual_score": (
                            f"{current_fixture['home_goals']}"
                            f"-"
                            f"{current_fixture['away_goals']}"
                        ),
                        "actual_result": actual_result,
                        "baseline_predicted_result": (
                            baseline_predicted_result
                        ),
                        "mle_predicted_result": (
                            mle_predicted_result
                        ),
                        "baseline_probabilities_percent": {
                            label: round(
                                baseline_probabilities[
                                    label
                                ]
                                * 100,
                                2,
                            )
                            for label in RESULT_LABELS
                        },
                        "mle_probabilities_percent": {
                            label: round(
                                mle_probabilities[label]
                                * 100,
                                2,
                            )
                            for label in RESULT_LABELS
                        },
                    }
                )

    baseline_metrics = finalize_model_metrics(
        accumulator=baseline_accumulator,
        evaluated_matches=evaluated_matches,
    )

    mle_metrics = finalize_model_metrics(
        accumulator=mle_accumulator,
        evaluated_matches=evaluated_matches,
    )

    return {
        "season": season,
        "total_fixtures": len(sorted_fixtures),
        "common_evaluated_matches": (
            evaluated_matches
        ),
        "skipped_insufficient_history": (
            skipped_insufficient_history
        ),
        "skipped_prediction_failure": (
            skipped_prediction_failure
        ),
        "actual_result_counts": (
            actual_result_counts
        ),
        "baseline_model": {
            "name": (
                "Bayesian-Smoothed Independent "
                "Poisson v0.3"
            ),
            "parameters": {
                "prior_matches": (
                    BASELINE_PRIOR_MATCHES
                ),
                "dixon_coles_rho": (
                    BASELINE_DIXON_COLES_RHO
                ),
                "half_life_days": None,
            },
            "metrics": baseline_metrics,
        },
        "mle_model": {
            "name": "Poisson MLE v0.4",
            "parameters": {
                "l2_regularization": (
                    MLE_L2_REGULARIZATION
                ),
                "refit_interval": (
                    MLE_REFIT_INTERVAL
                ),
            },
            "model_refits": mle_refits,
            "metrics": mle_metrics,
        },
        "paired_comparison": {
            "accuracy_difference_percentage_points_mle_minus_baseline": round(
                float(
                    mle_metrics[
                        "accuracy_percent"
                    ]
                )
                - float(
                    baseline_metrics[
                        "accuracy_percent"
                    ]
                ),
                2,
            ),
            "log_loss_difference_mle_minus_baseline": round(
                float(
                    mle_metrics[
                        "average_log_loss"
                    ]
                )
                - float(
                    baseline_metrics[
                        "average_log_loss"
                    ]
                ),
                4,
            ),
            "brier_difference_mle_minus_baseline": round(
                float(
                    mle_metrics[
                        "average_brier_score"
                    ]
                )
                - float(
                    baseline_metrics[
                        "average_brier_score"
                    ]
                ),
                4,
            ),
            "matches_with_same_top_prediction": (
                same_top_prediction
            ),
            "matches_with_different_top_prediction": (
                different_top_prediction
            ),
            "better_log_loss_match_counts": {
                "baseline": baseline_better_log_loss,
                "mle": mle_better_log_loss,
                "equal": equal_log_loss,
            },
            "better_brier_match_counts": {
                "baseline": baseline_better_brier,
                "mle": mle_better_brier,
                "equal": equal_brier,
            },
        },
        "example_disagreements": (
            example_disagreements
        ),
        "interpretation_rule": (
            "Στα πεδία difference_mle_minus_baseline, "
            "αρνητική τιμή ευνοεί το MLE και θετική "
            "τιμή ευνοεί το βασικό Poisson για Log "
            "Loss και Brier Score."
        ),
    }


def combine_season_results(
    season_results: list[dict[str, Any]],
    seasons: tuple[int, ...],
) -> dict[str, Any]:
    """Συνδυάζει αποτελέσματα πολλών σεζόν με βάρος τους αγώνες."""

    selected_results = [
        result
        for result in season_results
        if int(result["season"]) in seasons
    ]

    total_matches = sum(
        int(result["common_evaluated_matches"])
        for result in selected_results
    )

    if total_matches <= 0:
        raise ValueError(
            "Δεν υπάρχουν αγώνες για τη συνδυασμένη σύγκριση."
        )

    def combine_model(
        model_key: str,
    ) -> dict[str, float | int]:
        total_correct = sum(
            int(
                result[model_key]["metrics"][
                    "correct_predictions"
                ]
            )
            for result in selected_results
        )

        weighted_log_loss = sum(
            float(
                result[model_key]["metrics"][
                    "average_log_loss"
                ]
            )
            * int(result["common_evaluated_matches"])
            for result in selected_results
        ) / total_matches

        weighted_brier = sum(
            float(
                result[model_key]["metrics"][
                    "average_brier_score"
                ]
            )
            * int(result["common_evaluated_matches"])
            for result in selected_results
        ) / total_matches

        return {
            "evaluated_matches": total_matches,
            "correct_predictions": total_correct,
            "accuracy_percent": round(
                total_correct / total_matches * 100,
                2,
            ),
            "average_log_loss": round(
                weighted_log_loss,
                4,
            ),
            "average_brier_score": round(
                weighted_brier,
                4,
            ),
        }

    baseline = combine_model("baseline_model")
    mle = combine_model("mle_model")

    return {
        "seasons": list(seasons),
        "common_evaluated_matches": total_matches,
        "baseline_model": baseline,
        "mle_model": mle,
        "differences_mle_minus_baseline": {
            "accuracy_percentage_points": round(
                float(mle["accuracy_percent"])
                - float(
                    baseline["accuracy_percent"]
                ),
                2,
            ),
            "log_loss": round(
                float(mle["average_log_loss"])
                - float(
                    baseline["average_log_loss"]
                ),
                4,
            ),
            "brier_score": round(
                float(mle["average_brier_score"])
                - float(
                    baseline["average_brier_score"]
                ),
                4,
            ),
        },
    }


def run_comparison() -> dict[str, Any]:
    """Τρέχει τη σύγκριση για 2022, 2023 και 2024."""

    all_seasons = (
        *TRAINING_SEASONS,
        VALIDATION_SEASON,
    )

    season_results: list[dict[str, Any]] = []

    for season in all_seasons:
        print(
            f"Σύγκριση μοντέλων για τη σεζόν {season}...",
            flush=True,
        )

        fixtures = get_completed_fixtures(
            league_id=LEAGUE_ID,
            season=season,
        )

        if not fixtures:
            raise ValueError(
                "Δεν βρέθηκαν αγώνες για τη "
                f"σεζόν {season}."
            )

        season_results.append(
            compare_models_on_season(
                fixtures=fixtures,
                season=season,
            )
        )

    training_summary = combine_season_results(
        season_results=season_results,
        seasons=TRAINING_SEASONS,
    )

    validation_summary = combine_season_results(
        season_results=season_results,
        seasons=(VALIDATION_SEASON,),
    )

    all_seasons_summary = combine_season_results(
        season_results=season_results,
        seasons=all_seasons,
    )

    return {
        "league_id": LEAGUE_ID,
        "comparison_type": (
            "Paired walk-forward comparison on "
            "the exact same historical fixtures"
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
        "selected_parameters": {
            "baseline_prior_matches": (
                BASELINE_PRIOR_MATCHES
            ),
            "baseline_dixon_coles_rho": (
                BASELINE_DIXON_COLES_RHO
            ),
            "baseline_half_life_days": None,
            "mle_l2_regularization": (
                MLE_L2_REGULARIZATION
            ),
            "mle_refit_interval": (
                MLE_REFIT_INTERVAL
            ),
        },
        "training_summary": training_summary,
        "validation_summary": validation_summary,
        "all_seasons_summary": all_seasons_summary,
        "season_results": season_results,
    }


def main() -> None:
    """Εκτελεί τη σύγκριση και αποθηκεύει τα αποτελέσματα σε JSON."""

    try:
        results = run_comparison()

    except ValueError as error:
        print(f"Σφάλμα: {error}")
        return

    output_path = (
        "same_sample_model_comparison.json"
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

    validation = results[
        "validation_summary"
    ]["differences_mle_minus_baseline"]

    print()
    print("Η σύγκριση ολοκληρώθηκε.")
    print(
        "Validation διαφορά Accuracy "
        "(MLE - baseline):",
        validation["accuracy_percentage_points"],
    )
    print(
        "Validation διαφορά Log Loss "
        "(MLE - baseline):",
        validation["log_loss"],
    )
    print(
        "Validation διαφορά Brier "
        "(MLE - baseline):",
        validation["brier_score"],
    )
    print(
        "Τα πλήρη αποτελέσματα αποθηκεύτηκαν στο:",
        output_path,
    )


if __name__ == "__main__":
    main()
