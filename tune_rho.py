import json
from typing import Any

from backtesting import backtest_poisson_model
from database import get_completed_fixtures


LEAGUE_ID = 197

TRAINING_SEASONS = (
    2022,
    2023,
)

VALIDATION_SEASON = 2024

PRIOR_MATCHES = 2.0
HALF_LIFE_DAYS = None

RHO_VALUES = (
    -0.10,
    -0.075,
    -0.05,
    -0.025,
    0.0,
    0.025,
    0.05,
)

MINIMUM_PREVIOUS_LOCATION_MATCHES = 5


def extract_metrics(
    backtest_result: dict[str, Any],
) -> dict[str, float | int]:
    """
    Κρατά τις βασικές μετρικές
    από ένα πλήρες backtest.
    """

    return {
        "evaluated_matches": (
            backtest_result[
                "evaluated_matches"
            ]
        ),
        "correct_predictions": (
            backtest_result[
                "correct_predictions"
            ]
        ),
        "accuracy_percent": (
            backtest_result[
                "accuracy_percent"
            ]
        ),
        "balanced_accuracy_percent": (
            backtest_result[
                "balanced_accuracy_percent"
            ]
        ),
        "macro_f1_percent": (
            backtest_result[
                "macro_f1_percent"
            ]
        ),
        "average_log_loss": (
            backtest_result[
                "average_log_loss"
            ]
        ),
        "average_brier_score": (
            backtest_result[
                "average_brier_score"
            ]
        ),
    }


def calculate_weighted_average(
    season_results: dict[
        int,
        dict[str, float | int],
    ],
    metric_name: str,
) -> float:
    """
    Υπολογίζει σταθμισμένο μέσο όρο
    με βάρος τους αξιολογημένους αγώνες.
    """

    total_matches = sum(
        int(result["evaluated_matches"])
        for result in season_results.values()
    )

    if total_matches <= 0:
        raise ValueError(
            "Δεν υπάρχουν αξιολογημένοι αγώνες."
        )

    weighted_sum = sum(
        float(result[metric_name])
        * int(result["evaluated_matches"])
        for result in season_results.values()
    )

    return weighted_sum / total_matches


def summarize_training_results(
    season_results: dict[
        int,
        dict[str, float | int],
    ],
) -> dict[str, float | int]:
    """
    Δημιουργεί συνοπτικά αποτελέσματα
    για τις σεζόν εκπαίδευσης.
    """

    total_matches = sum(
        int(result["evaluated_matches"])
        for result in season_results.values()
    )

    total_correct = sum(
        int(result["correct_predictions"])
        for result in season_results.values()
    )

    if total_matches <= 0:
        raise ValueError(
            "Δεν υπάρχουν αξιολογημένοι αγώνες."
        )

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
            calculate_weighted_average(
                season_results,
                "balanced_accuracy_percent",
            ),
            2,
        ),
        "macro_f1_percent": round(
            calculate_weighted_average(
                season_results,
                "macro_f1_percent",
            ),
            2,
        ),
        "average_log_loss": round(
            calculate_weighted_average(
                season_results,
                "average_log_loss",
            ),
            4,
        ),
        "average_brier_score": round(
            calculate_weighted_average(
                season_results,
                "average_brier_score",
            ),
            4,
        ),
    }


def load_season_fixtures(
    season: int,
) -> list[dict[str, Any]]:
    """
    Διαβάζει τους ολοκληρωμένους αγώνες
    μιας σεζόν από τη SQLite.
    """

    fixtures = get_completed_fixtures(
        league_id=LEAGUE_ID,
        season=season,
    )

    if not fixtures:
        raise ValueError(
            f"Δεν βρέθηκαν αγώνες για τη σεζόν {season}."
        )

    return fixtures


def run_tuning() -> dict[str, Any]:
    """
    Δοκιμάζει διαφορετικές τιμές rho
    για τη διόρθωση Dixon-Coles.

    Οι σεζόν 2022 και 2023 χρησιμοποιούνται
    για την επιλογή της παραμέτρου.

    Η σεζόν 2024 χρησιμοποιείται μόνο
    για ανεξάρτητο validation.
    """

    all_seasons = (
        *TRAINING_SEASONS,
        VALIDATION_SEASON,
    )

    fixtures_by_season = {
        season: load_season_fixtures(season)
        for season in all_seasons
    }

    candidates: list[dict[str, Any]] = []

    for rho in RHO_VALUES:
        print(
            f"Δοκιμή Dixon-Coles rho={rho}...",
            flush=True,
        )

        training_season_results: dict[
            int,
            dict[str, float | int],
        ] = {}

        for season in TRAINING_SEASONS:
            full_result = backtest_poisson_model(
                fixtures=fixtures_by_season[season],
                min_previous_location_matches=(
                    MINIMUM_PREVIOUS_LOCATION_MATCHES
                ),
                prior_matches=PRIOR_MATCHES,
                half_life_days=HALF_LIFE_DAYS,
                dixon_coles_rho=rho,
            )

            training_season_results[season] = (
                extract_metrics(full_result)
            )

        training_summary = (
            summarize_training_results(
                training_season_results
            )
        )

        validation_full_result = (
            backtest_poisson_model(
                fixtures=fixtures_by_season[
                    VALIDATION_SEASON
                ],
                min_previous_location_matches=(
                    MINIMUM_PREVIOUS_LOCATION_MATCHES
                ),
                prior_matches=PRIOR_MATCHES,
                half_life_days=HALF_LIFE_DAYS,
                dixon_coles_rho=rho,
            )
        )

        validation_metrics = extract_metrics(
            validation_full_result
        )

        candidates.append(
            {
                "dixon_coles_rho": rho,
                "training_seasons": (
                    training_season_results
                ),
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
            candidate["training_summary"][
                "average_log_loss"
            ],
            candidate["training_summary"][
                "average_brier_score"
            ],
        )
    )

    best_candidate = candidates[0]

    return {
        "league_id": LEAGUE_ID,
        "training_seasons": list(
            TRAINING_SEASONS
        ),
        "validation_season": (
            VALIDATION_SEASON
        ),
        "prior_matches": PRIOR_MATCHES,
        "half_life_days": HALF_LIFE_DAYS,
        "minimum_previous_location_matches": (
            MINIMUM_PREVIOUS_LOCATION_MATCHES
        ),
        "tested_rho_values": list(
            RHO_VALUES
        ),
        "selection_rule": (
            "Χαμηλότερο σταθμισμένο Log Loss "
            "στις σεζόν 2022 και 2023. "
            "Σε ισοβαθμία χρησιμοποιείται "
            "το χαμηλότερο Brier Score."
        ),
        "recommended_dixon_coles_rho": (
            best_candidate[
                "dixon_coles_rho"
            ]
        ),
        "candidates_ranked": candidates,
    }


def main() -> None:
    """
    Εκτελεί το tuning και αποθηκεύει
    τα αποτελέσματα σε αρχείο JSON.
    """

    try:
        results = run_tuning()

    except ValueError as error:
        print(
            f"Σφάλμα: {error}"
        )
        return

    output_path = (
        "dixon_coles_tuning_results.json"
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

    print()
    print("Η διαδικασία ολοκληρώθηκε.")
    print(
        "Προτεινόμενο Dixon-Coles rho:",
        results[
            "recommended_dixon_coles_rho"
        ],
    )
    print(
        "Τα πλήρη αποτελέσματα αποθηκεύτηκαν στο:",
        output_path,
    )


if __name__ == "__main__":
    main()
