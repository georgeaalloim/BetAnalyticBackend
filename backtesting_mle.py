from __future__ import annotations

from math import log
from typing import Any

from poisson_mle_model import (
    fit_poisson_mle_model,
    predict_match_mle,
)


RESULT_LABELS = ("HOME", "DRAW", "AWAY")


def get_actual_result(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "HOME"
    if home_goals < away_goals:
        return "AWAY"
    return "DRAW"


def calculate_brier_score(
    probabilities: dict[str, float],
    actual_result: str,
) -> float:
    return sum(
        (
            probabilities[label]
            - (1.0 if actual_result == label else 0.0)
        )
        ** 2
        for label in RESULT_LABELS
    )


def count_previous_matches(
    fixtures: list[dict[str, Any]],
    team_id: int,
) -> int:
    return sum(
        1
        for fixture in fixtures
        if int(fixture["home_team_id"]) == team_id
        or int(fixture["away_team_id"]) == team_id
    )


def calculate_class_metrics(
    confusion_matrix: dict[str, dict[str, int]],
) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}

    for label in RESULT_LABELS:
        true_positive = confusion_matrix[label][label]
        actual_total = sum(confusion_matrix[label].values())
        predicted_total = sum(
            confusion_matrix[actual][label]
            for actual in RESULT_LABELS
        )

        recall = true_positive / actual_total if actual_total else 0.0
        precision = (
            true_positive / predicted_total
            if predicted_total
            else 0.0
        )
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )

        metrics[label] = {
            "actual_matches": actual_total,
            "predicted_matches": predicted_total,
            "correct": true_positive,
            "recall_percent": round(recall * 100, 2),
            "precision_percent": round(precision * 100, 2),
            "f1_score_percent": round(f1 * 100, 2),
        }

    return metrics


def backtest_poisson_mle_model(
    fixtures: list[dict[str, Any]],
    min_previous_team_matches: int = 5,
    l2_regularization: float = 1.0,
    refit_interval: int = 7,
) -> dict[str, Any]:
    """
    Walk-forward backtest του Poisson MLE.

    Το μοντέλο επανεκτιμάται κάθε refit_interval αξιολογημένους αγώνες.
    Σε κάθε επανεκτίμηση χρησιμοποιούνται μόνο παλαιότερα αποτελέσματα.
    """

    if not fixtures:
        raise ValueError("Δεν δόθηκαν αγώνες για backtesting.")
    if min_previous_team_matches < 1:
        raise ValueError("Το ελάχιστο ιστορικό πρέπει να είναι τουλάχιστον 1.")
    if l2_regularization < 0:
        raise ValueError("Το l2_regularization δεν μπορεί να είναι αρνητικό.")
    if refit_interval < 1:
        raise ValueError("Το refit_interval πρέπει να είναι τουλάχιστον 1.")

    sorted_fixtures = sorted(
        fixtures,
        key=lambda fixture: str(fixture.get("fixture_date") or ""),
    )

    evaluated_matches = 0
    skipped_matches = 0
    correct_predictions = 0
    total_log_loss = 0.0
    total_brier_score = 0.0
    refits = 0

    actual_result_counts = {label: 0 for label in RESULT_LABELS}
    predicted_result_counts = {label: 0 for label in RESULT_LABELS}
    confusion_matrix = {
        actual: {predicted: 0 for predicted in RESULT_LABELS}
        for actual in RESULT_LABELS
    }

    probability_sums = {label: 0.0 for label in RESULT_LABELS}
    example_predictions: list[dict[str, Any]] = []

    fitted_model: dict[str, Any] | None = None
    evaluations_since_refit = refit_interval

    for current_index, current_fixture in enumerate(sorted_fixtures):
        previous_fixtures = sorted_fixtures[:current_index]
        home_team_id = int(current_fixture["home_team_id"])
        away_team_id = int(current_fixture["away_team_id"])

        home_history = count_previous_matches(
            previous_fixtures,
            home_team_id,
        )
        away_history = count_previous_matches(
            previous_fixtures,
            away_team_id,
        )

        if (
            home_history < min_previous_team_matches
            or away_history < min_previous_team_matches
        ):
            skipped_matches += 1
            continue

        model_team_ids = (
            {
                int(team["team_id"])
                for team in fitted_model["teams"]
            }
            if fitted_model is not None
            else set()
        )

        must_refit = (
            fitted_model is None
            or evaluations_since_refit >= refit_interval
            or home_team_id not in model_team_ids
            or away_team_id not in model_team_ids
        )

        if must_refit:
            try:
                fitted_model = fit_poisson_mle_model(
                    previous_fixtures,
                    l2_regularization=l2_regularization,
                )
            except ValueError:
                skipped_matches += 1
                fitted_model = None
                continue

            refits += 1
            evaluations_since_refit = 0

        if fitted_model is None:
            skipped_matches += 1
            continue

        try:
            prediction = predict_match_mle(
                fitted_model,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )
        except ValueError:
            skipped_matches += 1
            continue

        result_probabilities = prediction["result_probabilities"]
        probabilities = {
            "HOME": float(result_probabilities["home_win"]),
            "DRAW": float(result_probabilities["draw"]),
            "AWAY": float(result_probabilities["away_win"]),
        }

        total_probability = sum(probabilities.values())
        if total_probability <= 0:
            skipped_matches += 1
            continue

        probabilities = {
            label: probability / total_probability
            for label, probability in probabilities.items()
        }

        predicted_result = max(
            probabilities,
            key=lambda label: probabilities[label],
        )
        actual_result = get_actual_result(
            int(current_fixture["home_goals"]),
            int(current_fixture["away_goals"]),
        )

        evaluated_matches += 1
        evaluations_since_refit += 1
        actual_result_counts[actual_result] += 1
        predicted_result_counts[predicted_result] += 1
        confusion_matrix[actual_result][predicted_result] += 1

        for label in RESULT_LABELS:
            probability_sums[label] += probabilities[label]

        if actual_result == predicted_result:
            correct_predictions += 1

        total_log_loss += -log(
            max(probabilities[actual_result], 1e-15)
        )
        total_brier_score += calculate_brier_score(
            probabilities,
            actual_result,
        )

        if len(example_predictions) < 10:
            example_predictions.append(
                {
                    "fixture_date": current_fixture["fixture_date"],
                    "home_team": current_fixture["home_team_name"],
                    "away_team": current_fixture["away_team_name"],
                    "actual_score": (
                        f"{current_fixture['home_goals']}-"
                        f"{current_fixture['away_goals']}"
                    ),
                    "actual_result": actual_result,
                    "predicted_result": predicted_result,
                    "probabilities": {
                        label.lower(): round(
                            probabilities[label] * 100,
                            2,
                        )
                        for label in RESULT_LABELS
                    },
                }
            )

    if evaluated_matches == 0:
        raise ValueError("Δεν αξιολογήθηκε κανένας αγώνας.")

    class_metrics = calculate_class_metrics(confusion_matrix)
    balanced_accuracy = sum(
        float(class_metrics[label]["recall_percent"])
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)
    macro_f1 = sum(
        float(class_metrics[label]["f1_score_percent"])
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)

    probability_summary = {
        label: {
            "average_predicted_percent": round(
                probability_sums[label] / evaluated_matches * 100,
                2,
            ),
            "actual_frequency_percent": round(
                actual_result_counts[label] / evaluated_matches * 100,
                2,
            ),
        }
        for label in RESULT_LABELS
    }

    return {
        "model": "Poisson MLE v0.4",
        "model_parameters": {
            "l2_regularization": l2_regularization,
            "minimum_previous_team_matches": min_previous_team_matches,
            "refit_interval": refit_interval,
        },
        "total_fixtures": len(sorted_fixtures),
        "evaluated_matches": evaluated_matches,
        "skipped_matches": skipped_matches,
        "model_refits": refits,
        "correct_predictions": correct_predictions,
        "incorrect_predictions": evaluated_matches - correct_predictions,
        "accuracy_percent": round(
            correct_predictions / evaluated_matches * 100,
            2,
        ),
        "balanced_accuracy_percent": round(balanced_accuracy, 2),
        "macro_f1_percent": round(macro_f1, 2),
        "average_log_loss": round(
            total_log_loss / evaluated_matches,
            4,
        ),
        "average_brier_score": round(
            total_brier_score / evaluated_matches,
            4,
        ),
        "actual_result_counts": actual_result_counts,
        "predicted_result_counts": predicted_result_counts,
        "probability_summary": probability_summary,
        "confusion_matrix": confusion_matrix,
        "class_metrics": class_metrics,
        "example_predictions": example_predictions,
    }
