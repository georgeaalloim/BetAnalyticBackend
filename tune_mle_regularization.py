from __future__ import annotations

import json
from typing import Any

from backtesting_mle import backtest_poisson_mle_model
from database import get_completed_fixtures


LEAGUE_ID = 197
TRAINING_SEASONS = (2022, 2023)
VALIDATION_SEASON = 2024

L2_VALUES = (
    0.1,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
)

MINIMUM_PREVIOUS_TEAM_MATCHES = 5
REFIT_INTERVAL = 7


def extract_metrics(result: dict[str, Any]) -> dict[str, float | int]:
    return {
        "evaluated_matches": int(result["evaluated_matches"]),
        "correct_predictions": int(result["correct_predictions"]),
        "accuracy_percent": float(result["accuracy_percent"]),
        "balanced_accuracy_percent": float(
            result["balanced_accuracy_percent"]
        ),
        "macro_f1_percent": float(result["macro_f1_percent"]),
        "average_log_loss": float(result["average_log_loss"]),
        "average_brier_score": float(result["average_brier_score"]),
        "model_refits": int(result["model_refits"]),
    }


def weighted_average(
    season_results: dict[int, dict[str, float | int]],
    metric: str,
) -> float:
    total_matches = sum(
        int(result["evaluated_matches"])
        for result in season_results.values()
    )
    return sum(
        float(result[metric]) * int(result["evaluated_matches"])
        for result in season_results.values()
    ) / total_matches


def summarize(
    season_results: dict[int, dict[str, float | int]],
) -> dict[str, float | int]:
    total_matches = sum(
        int(result["evaluated_matches"])
        for result in season_results.values()
    )
    total_correct = sum(
        int(result["correct_predictions"])
        for result in season_results.values()
    )

    return {
        "evaluated_matches": total_matches,
        "correct_predictions": total_correct,
        "accuracy_percent": round(total_correct / total_matches * 100, 2),
        "balanced_accuracy_percent": round(
            weighted_average(
                season_results,
                "balanced_accuracy_percent",
            ),
            2,
        ),
        "macro_f1_percent": round(
            weighted_average(season_results, "macro_f1_percent"),
            2,
        ),
        "average_log_loss": round(
            weighted_average(season_results, "average_log_loss"),
            4,
        ),
        "average_brier_score": round(
            weighted_average(season_results, "average_brier_score"),
            4,
        ),
    }


def load_fixtures(season: int) -> list[dict[str, Any]]:
    fixtures = get_completed_fixtures(
        league_id=LEAGUE_ID,
        season=season,
    )
    if not fixtures:
        raise ValueError(f"Δεν βρέθηκαν αγώνες για τη σεζόν {season}.")
    return fixtures


def run_tuning() -> dict[str, Any]:
    seasons = (*TRAINING_SEASONS, VALIDATION_SEASON)
    fixtures_by_season = {
        season: load_fixtures(season)
        for season in seasons
    }

    candidates: list[dict[str, Any]] = []

    for l2_regularization in L2_VALUES:
        print(
            f"Δοκιμή L2 regularization={l2_regularization}...",
            flush=True,
        )

        training_results: dict[int, dict[str, float | int]] = {}

        for season in TRAINING_SEASONS:
            result = backtest_poisson_mle_model(
                fixtures=fixtures_by_season[season],
                min_previous_team_matches=(
                    MINIMUM_PREVIOUS_TEAM_MATCHES
                ),
                l2_regularization=l2_regularization,
                refit_interval=REFIT_INTERVAL,
            )
            training_results[season] = extract_metrics(result)

        training_summary = summarize(training_results)

        validation_result = backtest_poisson_mle_model(
            fixtures=fixtures_by_season[VALIDATION_SEASON],
            min_previous_team_matches=(
                MINIMUM_PREVIOUS_TEAM_MATCHES
            ),
            l2_regularization=l2_regularization,
            refit_interval=REFIT_INTERVAL,
        )

        candidates.append(
            {
                "l2_regularization": l2_regularization,
                "training_seasons": training_results,
                "training_summary": training_summary,
                "validation_season": VALIDATION_SEASON,
                "validation_metrics": extract_metrics(
                    validation_result
                ),
            }
        )

    candidates.sort(
        key=lambda candidate: (
            candidate["training_summary"]["average_log_loss"],
            candidate["training_summary"]["average_brier_score"],
        )
    )

    return {
        "league_id": LEAGUE_ID,
        "model": "Poisson MLE v0.4",
        "training_seasons": list(TRAINING_SEASONS),
        "validation_season": VALIDATION_SEASON,
        "tested_l2_values": list(L2_VALUES),
        "minimum_previous_team_matches": (
            MINIMUM_PREVIOUS_TEAM_MATCHES
        ),
        "refit_interval": REFIT_INTERVAL,
        "selection_rule": (
            "Χαμηλότερο σταθμισμένο Log Loss στις σεζόν "
            "2022 και 2023. Σε ισοβαθμία χρησιμοποιείται "
            "το χαμηλότερο Brier Score."
        ),
        "recommended_l2_regularization": candidates[0][
            "l2_regularization"
        ],
        "candidates_ranked": candidates,
    }


def main() -> None:
    try:
        results = run_tuning()
    except (ValueError, ImportError) as error:
        print(f"Σφάλμα: {error}")
        return

    output_path = "mle_regularization_tuning_results.json"

    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(
            results,
            output_file,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("Η διαδικασία ολοκληρώθηκε.")
    print(
        "Προτεινόμενο L2 regularization:",
        results["recommended_l2_regularization"],
    )
    print("Αποτελέσματα:", output_path)


if __name__ == "__main__":
    main()
