from math import log
from typing import Any

from poisson_model import predict_match
from recency_analysis import (
    calculate_recency_weighted_home_away_statistics,
)
from team_analysis import (
    calculate_home_away_statistics,
)


RESULT_NAMES = {
    "home": "HOME",
    "draw": "DRAW",
    "away": "AWAY",
}

RESULT_LABELS = (
    "HOME",
    "DRAW",
    "AWAY",
)

CALIBRATION_BINS_COUNT = 10


def get_actual_result(
    home_goals: int,
    away_goals: int,
) -> str:
    """
    Μετατρέπει το πραγματικό σκορ σε:
    HOME, DRAW ή AWAY.
    """

    if home_goals > away_goals:
        return RESULT_NAMES["home"]

    if home_goals < away_goals:
        return RESULT_NAMES["away"]

    return RESULT_NAMES["draw"]


def normalize_probabilities(
    home_probability: float,
    draw_probability: float,
    away_probability: float,
) -> tuple[float, float, float]:
    """
    Κανονικοποιεί τις πιθανότητες ώστε
    να αθροίζουν ακριβώς σε 1.
    """

    total_probability = (
        home_probability
        + draw_probability
        + away_probability
    )

    if total_probability <= 0:
        raise ValueError(
            "Το άθροισμα των πιθανοτήτων πρέπει "
            "να είναι μεγαλύτερο από μηδέν."
        )

    return (
        home_probability / total_probability,
        draw_probability / total_probability,
        away_probability / total_probability,
    )


def extract_result_probabilities(
    prediction: dict[str, Any],
) -> tuple[float, float, float]:
    """
    Διαβάζει τις πιθανότητες 1-X-2 από την
    απάντηση του μοντέλου.
    """

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
        home_probability = float(
            result_probabilities["home_win"]
        )

        draw_probability = float(
            result_probabilities["draw"]
        )

        away_probability = float(
            result_probabilities["away_win"]
        )

    else:
        home_probability = (
            float(
                result_probabilities[
                    "home_win_percent"
                ]
            )
            / 100
        )

        draw_probability = (
            float(
                result_probabilities[
                    "draw_percent"
                ]
            )
            / 100
        )

        away_probability = (
            float(
                result_probabilities[
                    "away_win_percent"
                ]
            )
            / 100
        )

    return normalize_probabilities(
        home_probability=home_probability,
        draw_probability=draw_probability,
        away_probability=away_probability,
    )


def get_predicted_result(
    home_probability: float,
    draw_probability: float,
    away_probability: float,
) -> str:
    """
    Επιστρέφει την έκβαση με τη μεγαλύτερη
    πιθανότητα.
    """

    probabilities = {
        "HOME": home_probability,
        "DRAW": draw_probability,
        "AWAY": away_probability,
    }

    return max(
        probabilities,
        key=lambda result: probabilities[result],
    )


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
        if fixture["home_team_id"] == home_team_id
    )

    away_matches = sum(
        1
        for fixture in fixtures
        if fixture["away_team_id"] == away_team_id
    )

    return home_matches, away_matches


def calculate_brier_score(
    home_probability: float,
    draw_probability: float,
    away_probability: float,
    actual_result: str,
) -> float:
    """
    Υπολογίζει το multiclass Brier Score
    για HOME, DRAW και AWAY.
    """

    actual_home = (
        1.0
        if actual_result == "HOME"
        else 0.0
    )

    actual_draw = (
        1.0
        if actual_result == "DRAW"
        else 0.0
    )

    actual_away = (
        1.0
        if actual_result == "AWAY"
        else 0.0
    )

    return (
        (home_probability - actual_home) ** 2
        + (draw_probability - actual_draw) ** 2
        + (away_probability - actual_away) ** 2
    )


def calculate_class_metrics(
    confusion_matrix: dict[str, dict[str, int]],
) -> dict[str, dict[str, float | int]]:
    """
    Υπολογίζει Precision, Recall και F1-score
    για κάθε κατηγορία αποτελέσματος.
    """

    class_metrics: dict[
        str,
        dict[str, float | int],
    ] = {}

    for label in RESULT_LABELS:
        true_positives = confusion_matrix[
            label
        ][
            label
        ]

        actual_total = sum(
            confusion_matrix[label].values()
        )

        predicted_total = sum(
            confusion_matrix[
                actual_label
            ][
                label
            ]
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


def create_calibration_bins() -> dict[
    str,
    list[dict[str, float | int]],
]:
    """
    Δημιουργεί τις περιοχές πιθανότητας:
    0%-10%, 10%-20%, ..., 90%-100%.
    """

    calibration_bins: dict[
        str,
        list[dict[str, float | int]],
    ] = {}

    for label in RESULT_LABELS:
        calibration_bins[label] = []

        for index in range(
            CALIBRATION_BINS_COUNT
        ):
            lower_bound = (
                index
                / CALIBRATION_BINS_COUNT
            )

            upper_bound = (
                (index + 1)
                / CALIBRATION_BINS_COUNT
            )

            calibration_bins[label].append(
                {
                    "lower_bound": lower_bound,
                    "upper_bound": upper_bound,
                    "count": 0,
                    "probability_sum": 0.0,
                    "actual_occurrences": 0,
                }
            )

    return calibration_bins


def update_calibration_bins(
    calibration_bins: dict[
        str,
        list[dict[str, float | int]],
    ],
    probabilities: dict[str, float],
    actual_result: str,
) -> None:
    """
    Ενημερώνει τις περιοχές calibration
    με μία πρόβλεψη.
    """

    for label in RESULT_LABELS:
        probability = probabilities[label]

        bin_index = min(
            int(
                probability
                * CALIBRATION_BINS_COUNT
            ),
            CALIBRATION_BINS_COUNT - 1,
        )

        selected_bin = calibration_bins[
            label
        ][
            bin_index
        ]

        selected_bin["count"] += 1

        selected_bin[
            "probability_sum"
        ] += probability

        if actual_result == label:
            selected_bin[
                "actual_occurrences"
            ] += 1


def finalize_calibration_bins(
    calibration_bins: dict[
        str,
        list[dict[str, float | int]],
    ],
) -> dict[str, list[dict[str, Any]]]:
    """
    Μετατρέπει τα προσωρινά calibration δεδομένα
    σε τελική αναφορά.
    """

    calibration_report: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for label in RESULT_LABELS:
        calibration_report[label] = []

        for item in calibration_bins[label]:
            count = int(item["count"])

            if count == 0:
                continue

            probability_sum = float(
                item["probability_sum"]
            )

            actual_occurrences = int(
                item["actual_occurrences"]
            )

            average_probability = (
                probability_sum / count
            )

            actual_frequency = (
                actual_occurrences / count
            )

            lower_percent = round(
                float(
                    item["lower_bound"]
                )
                * 100
            )

            upper_percent = round(
                float(
                    item["upper_bound"]
                )
                * 100
            )

            calibration_error = (
                average_probability
                - actual_frequency
            )

            calibration_report[label].append(
                {
                    "range_percent": (
                        f"{lower_percent}"
                        f"-"
                        f"{upper_percent}"
                    ),
                    "predictions_count": count,
                    "average_predicted_percent": round(
                        average_probability * 100,
                        2,
                    ),
                    "actual_occurrences": (
                        actual_occurrences
                    ),
                    "actual_frequency_percent": round(
                        actual_frequency * 100,
                        2,
                    ),
                    "calibration_error_percent": round(
                        calibration_error * 100,
                        2,
                    ),
                }
            )

    return calibration_report


def calculate_probability_summary(
    probability_sums: dict[str, float],
    actual_result_counts: dict[str, int],
    evaluated_matches: int,
) -> dict[str, dict[str, float]]:
    """
    Συγκρίνει τη μέση πιθανότητα του μοντέλου
    με την πραγματική συχνότητα κάθε έκβασης.
    """

    summary: dict[
        str,
        dict[str, float],
    ] = {}

    for label in RESULT_LABELS:
        average_predicted = (
            probability_sums[label]
            / evaluated_matches
        )

        actual_frequency = (
            actual_result_counts[label]
            / evaluated_matches
        )

        summary[label] = {
            "average_predicted_percent": round(
                average_predicted * 100,
                2,
            ),
            "actual_frequency_percent": round(
                actual_frequency * 100,
                2,
            ),
            "bias_percent": round(
                (
                    average_predicted
                    - actual_frequency
                )
                * 100,
                2,
            ),
        }

    return summary


def calculate_empirical_baseline_scores(
    actual_result_counts: dict[str, int],
    evaluated_matches: int,
) -> dict[str, float]:
    """
    Υπολογίζει baseline που προβλέπει συνεχώς
    τις συνολικές συχνότητες HOME/DRAW/AWAY.
    """

    frequencies = {
        label: (
            actual_result_counts[label]
            / evaluated_matches
        )
        for label in RESULT_LABELS
    }

    empirical_log_loss = -sum(
        frequency * log(frequency)
        for frequency in frequencies.values()
        if frequency > 0
    )

    empirical_brier_score = (
        1
        - sum(
            frequency ** 2
            for frequency in frequencies.values()
        )
    )

    return {
        "log_loss": round(
            empirical_log_loss,
            4,
        ),
        "brier_score": round(
            empirical_brier_score,
            4,
        ),
    }


def build_match_analysis(
    previous_fixtures: list[dict[str, Any]],
    current_fixture_date: str,
    half_life_days: float | None,
) -> dict[str, Any]:
    """
    Δημιουργεί την ανάλυση που θα χρησιμοποιηθεί
    για την πρόβλεψη ενός αγώνα.

    half_life_days=None:
        Όλοι οι παλαιότεροι αγώνες έχουν ίσο βάρος.

    half_life_days>0:
        Οι πρόσφατοι αγώνες έχουν μεγαλύτερο βάρος.
    """

    if half_life_days is None:
        return calculate_home_away_statistics(
            previous_fixtures
        )

    return (
        calculate_recency_weighted_home_away_statistics(
            fixtures=previous_fixtures,
            reference_date=current_fixture_date,
            half_life_days=half_life_days,
        )
    )


def backtest_poisson_model(
    fixtures: list[dict[str, Any]],
    min_previous_location_matches: int = 5,
    prior_matches: float = 2.0,
    half_life_days: float | None = None,
    dixon_coles_rho: float = 0.0,
) -> dict[str, Any]:
    """
    Εκτελεί χρονικά σωστό walk-forward backtesting.

    prior_matches:
        Ισχύς Bayesian smoothing.

    half_life_days:
        None σημαίνει χωρίς χρονική στάθμιση.

        Παράδειγμα half_life_days=90:
        ένας αγώνας πριν από 90 ημέρες έχει βάρος 0.5.

    dixon_coles_rho:
        Παράμετρος διόρθωσης των χαμηλών σκορ.
        Η τιμή 0 σημαίνει ανεξάρτητο Poisson.
    """

    if min_previous_location_matches < 1:
        raise ValueError(
            "Το ελάχιστο ιστορικό πρέπει "
            "να είναι τουλάχιστον 1."
        )

    if prior_matches < 0:
        raise ValueError(
            "Το prior_matches δεν μπορεί "
            "να είναι αρνητικό."
        )

    if (
        half_life_days is not None
        and half_life_days <= 0
    ):
        raise ValueError(
            "Το half_life_days πρέπει να είναι "
            "θετικό ή None."
        )

    if (
        dixon_coles_rho < -0.25
        or dixon_coles_rho > 0.25
    ):
        raise ValueError(
            "Το dixon_coles_rho πρέπει να βρίσκεται "
            "στο διάστημα από -0.25 έως 0.25."
        )

    if not fixtures:
        raise ValueError(
            "Δεν δόθηκαν αγώνες για backtesting."
        )

    evaluated_matches = 0
    correct_predictions = 0
    skipped_matches = 0

    total_log_loss = 0.0
    total_brier_score = 0.0

    actual_result_counts = {
        "HOME": 0,
        "DRAW": 0,
        "AWAY": 0,
    }

    predicted_result_counts = {
        "HOME": 0,
        "DRAW": 0,
        "AWAY": 0,
    }

    probability_sums = {
        "HOME": 0.0,
        "DRAW": 0.0,
        "AWAY": 0.0,
    }

    confusion_matrix = {
        actual_result: {
            predicted_result: 0
            for predicted_result in RESULT_LABELS
        }
        for actual_result in RESULT_LABELS
    }

    calibration_bins = create_calibration_bins()

    example_predictions: list[
        dict[str, Any]
    ] = []

    sorted_fixtures = sorted(
        fixtures,
        key=lambda fixture: (
            fixture.get("fixture_date") or ""
        ),
    )

    for current_index, current_fixture in enumerate(
        sorted_fixtures
    ):
        previous_fixtures = sorted_fixtures[
            :current_index
        ]

        home_team_id = current_fixture[
            "home_team_id"
        ]

        away_team_id = current_fixture[
            "away_team_id"
        ]

        current_fixture_date = str(
            current_fixture["fixture_date"]
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
            < min_previous_location_matches
            or away_history
            < min_previous_location_matches
        ):
            skipped_matches += 1
            continue

        try:
            analysis = build_match_analysis(
                previous_fixtures=previous_fixtures,
                current_fixture_date=(
                    current_fixture_date
                ),
                half_life_days=half_life_days,
            )

            prediction = predict_match(
                analysis=analysis,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                prior_matches=prior_matches,
                rho=dixon_coles_rho,
            )

        except ValueError:
            skipped_matches += 1
            continue

        (
            home_probability,
            draw_probability,
            away_probability,
        ) = extract_result_probabilities(
            prediction=prediction,
        )

        probabilities = {
            "HOME": home_probability,
            "DRAW": draw_probability,
            "AWAY": away_probability,
        }

        actual_result = get_actual_result(
            home_goals=int(
                current_fixture["home_goals"]
            ),
            away_goals=int(
                current_fixture["away_goals"]
            ),
        )

        predicted_result = get_predicted_result(
            home_probability=home_probability,
            draw_probability=draw_probability,
            away_probability=away_probability,
        )

        evaluated_matches += 1

        actual_result_counts[actual_result] += 1
        predicted_result_counts[predicted_result] += 1

        confusion_matrix[
            actual_result
        ][
            predicted_result
        ] += 1

        for label in RESULT_LABELS:
            probability_sums[label] += (
                probabilities[label]
            )

        update_calibration_bins(
            calibration_bins=calibration_bins,
            probabilities=probabilities,
            actual_result=actual_result,
        )

        if predicted_result == actual_result:
            correct_predictions += 1

        actual_probability = max(
            probabilities[actual_result],
            1e-15,
        )

        total_log_loss += -log(
            actual_probability
        )

        total_brier_score += calculate_brier_score(
            home_probability=home_probability,
            draw_probability=draw_probability,
            away_probability=away_probability,
            actual_result=actual_result,
        )

        if len(example_predictions) < 10:
            example_predictions.append(
                {
                    "fixture_date": (
                        current_fixture_date
                    ),
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
                    "predicted_result": predicted_result,
                    "probabilities": {
                        "home": round(
                            home_probability * 100,
                            2,
                        ),
                        "draw": round(
                            draw_probability * 100,
                            2,
                        ),
                        "away": round(
                            away_probability * 100,
                            2,
                        ),
                    },
                }
            )

    if evaluated_matches == 0:
        raise ValueError(
            "Δεν αξιολογήθηκε κανένας αγώνας. "
            "Μείωσε το ελάχιστο απαιτούμενο ιστορικό."
        )

    class_metrics = calculate_class_metrics(
        confusion_matrix=confusion_matrix,
    )

    calibration_report = finalize_calibration_bins(
        calibration_bins=calibration_bins,
    )

    probability_summary = (
        calculate_probability_summary(
            probability_sums=probability_sums,
            actual_result_counts=actual_result_counts,
            evaluated_matches=evaluated_matches,
        )
    )

    empirical_baseline = (
        calculate_empirical_baseline_scores(
            actual_result_counts=actual_result_counts,
            evaluated_matches=evaluated_matches,
        )
    )

    majority_class_count = max(
        actual_result_counts.values()
    )

    majority_baseline_accuracy = (
        majority_class_count
        / evaluated_matches
        * 100
    )

    macro_f1_percent = sum(
        float(
            class_metrics[label][
                "f1_score_percent"
            ]
        )
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)

    balanced_accuracy_percent = sum(
        float(
            class_metrics[label][
                "recall_percent"
            ]
        )
        for label in RESULT_LABELS
    ) / len(RESULT_LABELS)

    if (
        half_life_days is not None
        and dixon_coles_rho != 0
    ):
        model_name = (
            "Recency-Weighted Bayesian "
            "Dixon-Coles Poisson experimental"
        )

    elif half_life_days is not None:
        model_name = (
            "Recency-Weighted Bayesian "
            "Poisson experimental"
        )

    elif dixon_coles_rho != 0:
        model_name = (
            "Bayesian-Smoothed Dixon-Coles "
            "Poisson v0.4"
        )

    else:
        model_name = (
            "Bayesian-Smoothed Independent "
            "Poisson v0.3"
        )

    return {
        "model": model_name,
        "model_parameters": {
            "prior_matches": prior_matches,
            "half_life_days": half_life_days,
            "dixon_coles_rho": (
                dixon_coles_rho
            ),
            "minimum_previous_location_matches": (
                min_previous_location_matches
            ),
        },
        "total_fixtures": len(sorted_fixtures),
        "evaluated_matches": evaluated_matches,
        "skipped_matches": skipped_matches,
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
            total_log_loss / evaluated_matches,
            4,
        ),
        "average_brier_score": round(
            total_brier_score / evaluated_matches,
            4,
        ),
        "correct_predictions": correct_predictions,
        "incorrect_predictions": (
            evaluated_matches
            - correct_predictions
        ),
        "baselines": {
            "majority_class_accuracy_percent": round(
                majority_baseline_accuracy,
                2,
            ),
            "uniform_log_loss": round(
                log(3),
                4,
            ),
            "uniform_brier_score": round(
                2 / 3,
                4,
            ),
            "empirical_constant_log_loss": (
                empirical_baseline["log_loss"]
            ),
            "empirical_constant_brier_score": (
                empirical_baseline[
                    "brier_score"
                ]
            ),
        },
        "actual_result_counts": actual_result_counts,
        "predicted_result_counts": (
            predicted_result_counts
        ),
        "probability_summary": probability_summary,
        "confusion_matrix": confusion_matrix,
        "class_metrics": class_metrics,
        "calibration_report": calibration_report,
        "example_predictions": example_predictions,
    }