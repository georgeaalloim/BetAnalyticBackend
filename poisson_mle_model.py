from __future__ import annotations

from math import exp, factorial, lgamma, log
from typing import Any

import numpy as np
from scipy.optimize import minimize


DEFAULT_L2_REGULARIZATION = 1.0
DEFAULT_MAX_GOALS = 10


def poisson_probability(
    goals: int,
    expected_goals: float,
) -> float:
    """Υπολογίζει P(X=goals) για κατανομή Poisson."""

    if goals < 0:
        raise ValueError("Ο αριθμός γκολ δεν μπορεί να είναι αρνητικός.")

    if expected_goals <= 0:
        raise ValueError("Τα αναμενόμενα γκολ πρέπει να είναι θετικά.")

    return (
        exp(-expected_goals)
        * expected_goals**goals
        / factorial(goals)
    )


def _build_team_maps(
    fixtures: list[dict[str, Any]],
) -> tuple[list[int], dict[int, int], dict[int, str]]:
    """Δημιουργεί σταθερή αντιστοίχιση team_id -> θέση παραμέτρου."""

    team_names: dict[int, str] = {}

    for fixture in fixtures:
        home_team_id = int(fixture["home_team_id"])
        away_team_id = int(fixture["away_team_id"])

        team_names[home_team_id] = str(fixture["home_team_name"])
        team_names[away_team_id] = str(fixture["away_team_name"])

    team_ids = sorted(team_names)

    if len(team_ids) < 2:
        raise ValueError("Χρειάζονται τουλάχιστον δύο ομάδες για εκτίμηση.")

    team_index = {
        team_id: index
        for index, team_id in enumerate(team_ids)
    }

    return team_ids, team_index, team_names


def _reconstruct_zero_sum_parameters(
    free_values: np.ndarray,
) -> np.ndarray:
    """Προσθέτει τελευταία τιμή ώστε το άθροισμα να είναι μηδέν."""

    return np.concatenate(
        [free_values, np.array([-float(np.sum(free_values))])]
    )


def _unpack_parameters(
    parameters: np.ndarray,
    team_count: int,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Μετατρέπει το διάνυσμα βελτιστοποίησης σε ratings."""

    free_count = team_count - 1

    attack_free = parameters[:free_count]
    defence_free = parameters[free_count : 2 * free_count]

    log_base_rate = float(parameters[-2])
    home_advantage = float(parameters[-1])

    attack = _reconstruct_zero_sum_parameters(attack_free)
    defence = _reconstruct_zero_sum_parameters(defence_free)

    return attack, defence, log_base_rate, home_advantage


def _negative_log_likelihood(
    parameters: np.ndarray,
    team_count: int,
    home_indices: np.ndarray,
    away_indices: np.ndarray,
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    l2_regularization: float,
) -> float:
    """Poisson αρνητική log-likelihood με L2 regularization."""

    attack, defence, log_base_rate, home_advantage = (
        _unpack_parameters(parameters, team_count)
    )

    home_linear = (
        log_base_rate
        + home_advantage
        + attack[home_indices]
        - defence[away_indices]
    )

    away_linear = (
        log_base_rate
        + attack[away_indices]
        - defence[home_indices]
    )

    # Προστασία από αριθμητικό overflow κατά τη βελτιστοποίηση.
    home_linear = np.clip(home_linear, -8.0, 8.0)
    away_linear = np.clip(away_linear, -8.0, 8.0)

    expected_home_goals = np.exp(home_linear)
    expected_away_goals = np.exp(away_linear)

    home_log_factorials = np.array(
        [lgamma(float(goals) + 1.0) for goals in home_goals]
    )
    away_log_factorials = np.array(
        [lgamma(float(goals) + 1.0) for goals in away_goals]
    )

    negative_log_likelihood = float(
        np.sum(
            expected_home_goals
            - home_goals * home_linear
            + home_log_factorials
        )
        + np.sum(
            expected_away_goals
            - away_goals * away_linear
            + away_log_factorials
        )
    )

    penalty = 0.5 * l2_regularization * float(
        np.sum(attack**2) + np.sum(defence**2)
    )

    return negative_log_likelihood + penalty


def fit_poisson_mle_model(
    fixtures: list[dict[str, Any]],
    l2_regularization: float = DEFAULT_L2_REGULARIZATION,
) -> dict[str, Any]:
    """
    Εκτιμά επίθεση, άμυνα, βασικό ρυθμό γκολ και πλεονέκτημα έδρας.

    Μοντέλο:
        log(lambda_home) = base + home_adv + attack_home - defence_away
        log(lambda_away) = base + attack_away - defence_home
    """

    if not fixtures:
        raise ValueError("Δεν δόθηκαν αγώνες για εκτίμηση του μοντέλου.")

    if l2_regularization < 0:
        raise ValueError("Το l2_regularization δεν μπορεί να είναι αρνητικό.")

    team_ids, team_index, team_names = _build_team_maps(fixtures)
    team_count = len(team_ids)

    home_indices = np.array(
        [team_index[int(fixture["home_team_id"])] for fixture in fixtures],
        dtype=int,
    )
    away_indices = np.array(
        [team_index[int(fixture["away_team_id"])] for fixture in fixtures],
        dtype=int,
    )
    home_goals = np.array(
        [float(fixture["home_goals"]) for fixture in fixtures],
        dtype=float,
    )
    away_goals = np.array(
        [float(fixture["away_goals"]) for fixture in fixtures],
        dtype=float,
    )

    mean_home_goals = max(float(np.mean(home_goals)), 0.05)
    mean_away_goals = max(float(np.mean(away_goals)), 0.05)

    initial_base_rate = max(
        (mean_home_goals + mean_away_goals) / 2.0,
        0.05,
    )
    initial_home_advantage = log(mean_home_goals / mean_away_goals)

    free_count = team_count - 1
    initial_parameters = np.zeros(2 * free_count + 2, dtype=float)
    initial_parameters[-2] = log(initial_base_rate)
    initial_parameters[-1] = initial_home_advantage

    bounds = (
        [(-2.5, 2.5)] * (2 * free_count)
        + [(-3.0, 1.5), (-1.5, 1.5)]
    )

    optimization = minimize(
        _negative_log_likelihood,
        initial_parameters,
        args=(
            team_count,
            home_indices,
            away_indices,
            home_goals,
            away_goals,
            l2_regularization,
        ),
        method="L-BFGS-B",
        bounds=bounds,
        options={
            "maxiter": 600,
            "ftol": 1e-10,
            "gtol": 1e-7,
        },
    )

    if not optimization.success:
        raise ValueError(
            "Η βελτιστοποίηση του Poisson MLE απέτυχε: "
            f"{optimization.message}"
        )

    attack, defence, log_base_rate, home_advantage = (
        _unpack_parameters(optimization.x, team_count)
    )

    teams = []
    for team_id in team_ids:
        index = team_index[team_id]
        teams.append(
            {
                "team_id": team_id,
                "team_name": team_names[team_id],
                "attack_rating": float(attack[index]),
                "defence_rating": float(defence[index]),
                "attack_multiplier": float(exp(attack[index])),
                "defence_multiplier": float(exp(-defence[index])),
            }
        )

    return {
        "model": "Poisson MLE v0.4",
        "parameters": {
            "l2_regularization": l2_regularization,
            "base_goals_per_team": float(exp(log_base_rate)),
            "home_advantage_log": home_advantage,
            "home_advantage_multiplier": float(exp(home_advantage)),
        },
        "fit": {
            "fixtures_used": len(fixtures),
            "teams_count": team_count,
            "negative_log_likelihood_with_penalty": float(
                optimization.fun
            ),
            "iterations": int(optimization.nit),
            "converged": bool(optimization.success),
        },
        "team_index": team_index,
        "teams": teams,
    }


def _find_team_rating(
    fitted_model: dict[str, Any],
    team_id: int,
) -> dict[str, Any]:
    """Βρίσκει τα ratings μιας ομάδας στο fitted model."""

    for team in fitted_model["teams"]:
        if int(team["team_id"]) == int(team_id):
            return team

    raise ValueError(f"Η ομάδα με team_id={team_id} δεν υπάρχει στο μοντέλο.")


def predict_match_mle(
    fitted_model: dict[str, Any],
    home_team_id: int,
    away_team_id: int,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> dict[str, Any]:
    """Παράγει 1-X-2 και αγορές γκολ από fitted Poisson MLE."""

    if home_team_id == away_team_id:
        raise ValueError("Οι δύο ομάδες δεν μπορούν να είναι ίδιες.")

    if max_goals < 1:
        raise ValueError("Το max_goals πρέπει να είναι τουλάχιστον 1.")

    home_team = _find_team_rating(fitted_model, home_team_id)
    away_team = _find_team_rating(fitted_model, away_team_id)

    parameters = fitted_model["parameters"]
    base_goals = float(parameters["base_goals_per_team"])
    home_advantage_log = float(parameters["home_advantage_log"])

    expected_home_goals = exp(
        log(base_goals)
        + home_advantage_log
        + float(home_team["attack_rating"])
        - float(away_team["defence_rating"])
    )
    expected_away_goals = exp(
        log(base_goals)
        + float(away_team["attack_rating"])
        - float(home_team["defence_rating"])
    )

    home_win_probability = 0.0
    draw_probability = 0.0
    away_win_probability = 0.0
    over_2_5_probability = 0.0
    both_teams_score_probability = 0.0
    total_probability = 0.0
    score_probabilities: list[dict[str, Any]] = []

    for home_goals_value in range(max_goals + 1):
        home_probability = poisson_probability(
            home_goals_value,
            expected_home_goals,
        )

        for away_goals_value in range(max_goals + 1):
            away_probability = poisson_probability(
                away_goals_value,
                expected_away_goals,
            )
            score_probability = home_probability * away_probability
            total_probability += score_probability

            if home_goals_value > away_goals_value:
                home_win_probability += score_probability
            elif home_goals_value == away_goals_value:
                draw_probability += score_probability
            else:
                away_win_probability += score_probability

            if home_goals_value + away_goals_value >= 3:
                over_2_5_probability += score_probability

            if home_goals_value >= 1 and away_goals_value >= 1:
                both_teams_score_probability += score_probability

            score_probabilities.append(
                {
                    "home_goals": home_goals_value,
                    "away_goals": away_goals_value,
                    "probability": score_probability,
                }
            )

    if total_probability <= 0:
        raise ValueError("Δεν ήταν δυνατός ο υπολογισμός πιθανοτήτων.")

    home_win_probability /= total_probability
    draw_probability /= total_probability
    away_win_probability /= total_probability
    over_2_5_probability /= total_probability
    both_teams_score_probability /= total_probability

    for score in score_probabilities:
        score["probability"] /= total_probability

    score_probabilities.sort(
        key=lambda score: float(score["probability"]),
        reverse=True,
    )

    most_likely_scores = [
        {
            "score": f"{score['home_goals']}-{score['away_goals']}",
            "probability_percent": round(
                float(score["probability"]) * 100,
                2,
            ),
        }
        for score in score_probabilities[:5]
    ]

    return {
        "model": fitted_model["model"],
        "home_team": {
            "team_id": int(home_team["team_id"]),
            "team_name": str(home_team["team_name"]),
        },
        "away_team": {
            "team_id": int(away_team["team_id"]),
            "team_name": str(away_team["team_name"]),
        },
        "expected_goals": {
            "home": round(expected_home_goals, 3),
            "away": round(expected_away_goals, 3),
            "total": round(expected_home_goals + expected_away_goals, 3),
        },
        "result_probabilities": {
            "home_win": round(home_win_probability, 8),
            "draw": round(draw_probability, 8),
            "away_win": round(away_win_probability, 8),
            "home_win_percent": round(home_win_probability * 100, 2),
            "draw_percent": round(draw_probability * 100, 2),
            "away_win_percent": round(away_win_probability * 100, 2),
        },
        "goals_probabilities": {
            "over_2_5": round(over_2_5_probability, 8),
            "under_2_5": round(1 - over_2_5_probability, 8),
            "both_teams_score_yes": round(
                both_teams_score_probability,
                8,
            ),
            "both_teams_score_no": round(
                1 - both_teams_score_probability,
                8,
            ),
            "over_2_5_percent": round(over_2_5_probability * 100, 2),
            "under_2_5_percent": round(
                (1 - over_2_5_probability) * 100,
                2,
            ),
            "both_teams_score_yes_percent": round(
                both_teams_score_probability * 100,
                2,
            ),
            "both_teams_score_no_percent": round(
                (1 - both_teams_score_probability) * 100,
                2,
            ),
        },
        "most_likely_scores": most_likely_scores,
    }
