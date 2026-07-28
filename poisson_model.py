from math import exp, factorial
from typing import Any

from market_lines import build_total_market_lines


DEFAULT_PRIOR_MATCHES = 2.0
DEFAULT_DIXON_COLES_RHO = 0.0
DEFAULT_MAX_GOALS = 10


def poisson_probability(
    goals: int,
    expected_goals: float,
) -> float:
    """
    Υπολογίζει την πιθανότητα να σημειωθούν ακριβώς
    goals γκολ, όταν ο αναμενόμενος αριθμός γκολ
    είναι expected_goals.
    """

    if goals < 0:
        raise ValueError(
            "Ο αριθμός γκολ δεν μπορεί να είναι αρνητικός."
        )

    if expected_goals < 0:
        raise ValueError(
            "Τα αναμενόμενα γκολ δεν μπορούν "
            "να είναι αρνητικά."
        )

    return (
        exp(-expected_goals)
        * expected_goals ** goals
        / factorial(goals)
    )



def dixon_coles_correction(
    home_goals: int,
    away_goals: int,
    expected_home_goals: float,
    expected_away_goals: float,
    rho: float,
) -> float:
    """
    Υπολογίζει τη διόρθωση Dixon-Coles για τα
    χαμηλά σκορ 0-0, 0-1, 1-0 και 1-1.

    rho = 0:
        Καμία διόρθωση. Το μοντέλο παραμένει
        ανεξάρτητο Poisson.

    Αρνητικό rho συνήθως αυξάνει τις πιθανότητες
    των 0-0 και 1-1 και μειώνει τις 0-1 και 1-0.
    """

    if home_goals == 0 and away_goals == 0:
        correction = (
            1
            - expected_home_goals
            * expected_away_goals
            * rho
        )

    elif home_goals == 0 and away_goals == 1:
        correction = (
            1
            + expected_home_goals
            * rho
        )

    elif home_goals == 1 and away_goals == 0:
        correction = (
            1
            + expected_away_goals
            * rho
        )

    elif home_goals == 1 and away_goals == 1:
        correction = 1 - rho

    else:
        correction = 1.0

    if correction <= 0:
        raise ValueError(
            "Η τιμή rho δημιουργεί μη θετική "
            "διόρθωση Dixon-Coles."
        )

    return correction

def find_team(
    teams: list[dict[str, Any]],
    team_id: int,
    *,
    allow_neutral_fallback: bool = False,
) -> dict[str, Any]:
    """
    Αναζητά μία ομάδα με βάση το μοναδικό team_id.
    """

    for team in teams:
        if team["team_id"] == team_id:
            return team

    if allow_neutral_fallback:
        empty_venue = {
            "matches": 0.0,
            "goals_for": 0.0,
            "goals_against": 0.0,
        }
        return {
            "team_id": int(team_id),
            "team_name": f"team_id={team_id}",
            "home": dict(empty_venue),
            "away": dict(empty_venue),
            "cold_start": True,
        }

    raise ValueError(
        f"Δεν βρέθηκε ομάδα με team_id={team_id}."
    )


def calculate_smoothed_average(
    observed_total: float,
    observed_matches: float,
    league_average: float,
    prior_matches: float,
) -> float:
    """
    Υπολογίζει Bayesian-smoothed μέσο όρο.

    observed_matches μπορεί να είναι δεκαδικός,
    όταν χρησιμοποιούμε βάρη παλαιότητας.
    """

    if observed_matches < 0:
        raise ValueError(
            "Ο αριθμός αγώνων δεν μπορεί "
            "να είναι αρνητικός."
        )

    if observed_total < 0:
        raise ValueError(
            "Το σύνολο γκολ δεν μπορεί "
            "να είναι αρνητικό."
        )

    if league_average < 0:
        raise ValueError(
            "Ο μέσος όρος της λίγκας δεν μπορεί "
            "να είναι αρνητικός."
        )

    if prior_matches < 0:
        raise ValueError(
            "Το prior_matches δεν μπορεί "
            "να είναι αρνητικό."
        )

    denominator = (
        observed_matches + prior_matches
    )

    if denominator <= 0:
        raise ValueError(
            "Δεν υπάρχουν αρκετά δεδομένα για "
            "τον υπολογισμό του μέσου όρου."
        )

    prior_total = (
        prior_matches * league_average
    )

    return (
        observed_total + prior_total
    ) / denominator


def get_exact_league_goal_averages(
    league_averages: dict[str, Any],
) -> tuple[float, float]:
    """
    Υπολογίζει τους ακριβείς μέσους όρους γκολ
    από τα συνολικά γκολ και τα συνολικά βάρη αγώνων.

    Λειτουργεί τόσο με κανονικούς όσο και με
    χρονικά σταθμισμένους αγώνες.
    """

    league_matches = float(
        league_averages["matches"]
    )

    if league_matches <= 0:
        raise ValueError(
            "Ο αριθμός αγώνων της διοργάνωσης "
            "πρέπει να είναι θετικός."
        )

    total_home_goals = float(
        league_averages["total_home_goals"]
    )

    total_away_goals = float(
        league_averages["total_away_goals"]
    )

    average_home_goals = (
        total_home_goals / league_matches
    )

    average_away_goals = (
        total_away_goals / league_matches
    )

    if (
        average_home_goals <= 0
        or average_away_goals <= 0
    ):
        raise ValueError(
            "Οι μέσοι όροι γκολ της διοργάνωσης "
            "πρέπει να είναι θετικοί."
        )

    return (
        average_home_goals,
        average_away_goals,
    )


def calculate_team_strengths(
    analysis: dict[str, Any],
    home_team_id: int,
    away_team_id: int,
    prior_matches: float,
) -> dict[str, Any]:
    """
    Υπολογίζει επιθετικές και αμυντικές δυνάμεις
    των δύο ομάδων με Bayesian smoothing.
    """

    league_averages = analysis[
        "league_averages"
    ]

    teams = analysis["teams"]

    home_team = find_team(
        teams=teams,
        team_id=home_team_id,
        allow_neutral_fallback=True,
    )

    away_team = find_team(
        teams=teams,
        team_id=away_team_id,
        allow_neutral_fallback=True,
    )

    average_home_goals, average_away_goals = (
        get_exact_league_goal_averages(
            league_averages=league_averages,
        )
    )

    home_statistics = home_team["home"]
    away_statistics = away_team["away"]

    home_matches = float(
        home_statistics["matches"]
    )

    away_matches = float(
        away_statistics["matches"]
    )

    smoothed_home_goals_for = (
        calculate_smoothed_average(
            observed_total=float(
                home_statistics["goals_for"]
            ),
            observed_matches=home_matches,
            league_average=average_home_goals,
            prior_matches=prior_matches,
        )
    )

    smoothed_home_goals_against = (
        calculate_smoothed_average(
            observed_total=float(
                home_statistics["goals_against"]
            ),
            observed_matches=home_matches,
            league_average=average_away_goals,
            prior_matches=prior_matches,
        )
    )

    smoothed_away_goals_for = (
        calculate_smoothed_average(
            observed_total=float(
                away_statistics["goals_for"]
            ),
            observed_matches=away_matches,
            league_average=average_away_goals,
            prior_matches=prior_matches,
        )
    )

    smoothed_away_goals_against = (
        calculate_smoothed_average(
            observed_total=float(
                away_statistics["goals_against"]
            ),
            observed_matches=away_matches,
            league_average=average_home_goals,
            prior_matches=prior_matches,
        )
    )

    home_attack_strength = (
        smoothed_home_goals_for
        / average_home_goals
    )

    home_defence_strength = (
        smoothed_home_goals_against
        / average_away_goals
    )

    away_attack_strength = (
        smoothed_away_goals_for
        / average_away_goals
    )

    away_defence_strength = (
        smoothed_away_goals_against
        / average_home_goals
    )

    expected_home_goals = (
        average_home_goals
        * home_attack_strength
        * away_defence_strength
    )

    expected_away_goals = (
        average_away_goals
        * away_attack_strength
        * home_defence_strength
    )

    return {
        "home_team": home_team,
        "away_team": away_team,
        "league_averages": {
            "home_goals_per_match": (
                average_home_goals
            ),
            "away_goals_per_match": (
                average_away_goals
            ),
        },
        "raw_goal_rates": {
            "home_goals_for_per_match": (
                float(home_statistics["goals_for"]) / home_matches
                if home_matches > 0
                else average_home_goals
            ),
            "home_goals_against_per_match": (
                float(home_statistics["goals_against"]) / home_matches
                if home_matches > 0
                else average_away_goals
            ),
            "away_goals_for_per_match": (
                float(away_statistics["goals_for"]) / away_matches
                if away_matches > 0
                else average_away_goals
            ),
            "away_goals_against_per_match": (
                float(away_statistics["goals_against"]) / away_matches
                if away_matches > 0
                else average_home_goals
            ),
        },
        "cold_start": {
            "home": home_matches <= 0,
            "away": away_matches <= 0,
        },
        "smoothed_goal_rates": {
            "home_goals_for_per_match": (
                smoothed_home_goals_for
            ),
            "home_goals_against_per_match": (
                smoothed_home_goals_against
            ),
            "away_goals_for_per_match": (
                smoothed_away_goals_for
            ),
            "away_goals_against_per_match": (
                smoothed_away_goals_against
            ),
        },
        "strengths": {
            "home_attack": home_attack_strength,
            "home_defence": home_defence_strength,
            "away_attack": away_attack_strength,
            "away_defence": away_defence_strength,
        },
        "expected_home_goals": (
            expected_home_goals
        ),
        "expected_away_goals": (
            expected_away_goals
        ),
    }


def predict_match(
    analysis: dict[str, Any],
    home_team_id: int,
    away_team_id: int,
    max_goals: int = DEFAULT_MAX_GOALS,
    prior_matches: float = DEFAULT_PRIOR_MATCHES,
    rho: float = DEFAULT_DIXON_COLES_RHO,
) -> dict[str, Any]:
    """
    Υπολογίζει αναμενόμενα γκολ και πιθανότητες
    αγώνα με Bayesian smoothing, Poisson και
    προαιρετική διόρθωση Dixon-Coles.
    """

    if home_team_id == away_team_id:
        raise ValueError(
            "Η γηπεδούχος και η φιλοξενούμενη "
            "ομάδα δεν μπορούν να είναι ίδιες."
        )

    if max_goals < 1:
        raise ValueError(
            "Το max_goals πρέπει να είναι "
            "τουλάχιστον 1."
        )

    if prior_matches < 0:
        raise ValueError(
            "Το prior_matches δεν μπορεί "
            "να είναι αρνητικό."
        )

    if rho < -0.25 or rho > 0.25:
        raise ValueError(
            "Το rho πρέπει να βρίσκεται στο "
            "διάστημα από -0.25 έως 0.25."
        )

    strength_analysis = calculate_team_strengths(
        analysis=analysis,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        prior_matches=prior_matches,
    )

    home_team = strength_analysis[
        "home_team"
    ]

    away_team = strength_analysis[
        "away_team"
    ]

    expected_home_goals = strength_analysis[
        "expected_home_goals"
    ]

    expected_away_goals = strength_analysis[
        "expected_away_goals"
    ]

    home_win_probability = 0.0
    draw_probability = 0.0
    away_win_probability = 0.0

    over_2_5_probability = 0.0
    both_teams_score_probability = 0.0
    total_probability = 0.0

    score_probabilities: list[
        dict[str, Any]
    ] = []

    for home_goals in range(
        max_goals + 1
    ):
        home_probability = poisson_probability(
            goals=home_goals,
            expected_goals=expected_home_goals,
        )

        for away_goals in range(
            max_goals + 1
        ):
            away_probability = poisson_probability(
                goals=away_goals,
                expected_goals=expected_away_goals,
            )

            correction = dixon_coles_correction(
                home_goals=home_goals,
                away_goals=away_goals,
                expected_home_goals=(
                    expected_home_goals
                ),
                expected_away_goals=(
                    expected_away_goals
                ),
                rho=rho,
            )

            score_probability = (
                home_probability
                * away_probability
                * correction
            )

            total_probability += score_probability

            if home_goals > away_goals:
                home_win_probability += (
                    score_probability
                )

            elif home_goals == away_goals:
                draw_probability += (
                    score_probability
                )

            else:
                away_win_probability += (
                    score_probability
                )

            if (
                home_goals + away_goals
                >= 3
            ):
                over_2_5_probability += (
                    score_probability
                )

            if (
                home_goals >= 1
                and away_goals >= 1
            ):
                both_teams_score_probability += (
                    score_probability
                )

            score_probabilities.append(
                {
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "probability": (
                        score_probability
                    ),
                }
            )

    if total_probability <= 0:
        raise ValueError(
            "Δεν ήταν δυνατός ο υπολογισμός "
            "των πιθανοτήτων."
        )

    home_win_probability /= total_probability
    draw_probability /= total_probability
    away_win_probability /= total_probability
    over_2_5_probability /= total_probability
    both_teams_score_probability /= (
        total_probability
    )

    for score in score_probabilities:
        score["probability"] /= (
            total_probability
        )

    score_probabilities.sort(
        key=lambda score: score["probability"],
        reverse=True,
    )

    total_goals_lines = build_total_market_lines(
        score_probabilities=score_probabilities,
    )

    most_likely_scores = []

    for score in score_probabilities[:5]:
        most_likely_scores.append(
            {
                "score": (
                    f"{score['home_goals']}"
                    f"-"
                    f"{score['away_goals']}"
                ),
                "probability": round(
                    score["probability"],
                    6,
                ),
                "probability_percent": round(
                    score["probability"] * 100,
                    2,
                ),
            }
        )

    strengths = strength_analysis[
        "strengths"
    ]

    raw_goal_rates = strength_analysis[
        "raw_goal_rates"
    ]

    smoothed_goal_rates = strength_analysis[
        "smoothed_goal_rates"
    ]

    league_averages = strength_analysis[
        "league_averages"
    ]

    model_name = (
        "Bayesian-Smoothed Dixon-Coles "
        "Poisson v0.4"
        if rho != 0
        else (
            "Bayesian-Smoothed Independent "
            "Poisson v0.3"
        )
    )

    return {
        "model": model_name,
        "model_parameters": {
            "prior_matches": prior_matches,
            "dixon_coles_rho": rho,
            "max_goals": max_goals,
        },
        "home_team": {
            "team_id": home_team["team_id"],
            "team_name": home_team["team_name"],
        },
        "away_team": {
            "team_id": away_team["team_id"],
            "team_name": away_team["team_name"],
        },
        "data_quality": {
            "level": (
                "limited"
                if any(strength_analysis["cold_start"].values())
                else "standard"
            ),
            "cold_start_home": bool(strength_analysis["cold_start"]["home"]),
            "cold_start_away": bool(strength_analysis["cold_start"]["away"]),
        },
        "league_averages": {
            "home_goals_per_match": round(
                league_averages[
                    "home_goals_per_match"
                ],
                3,
            ),
            "away_goals_per_match": round(
                league_averages[
                    "away_goals_per_match"
                ],
                3,
            ),
        },
        "raw_goal_rates": {
            key: round(value, 3)
            for key, value
            in raw_goal_rates.items()
        },
        "smoothed_goal_rates": {
            key: round(value, 3)
            for key, value
            in smoothed_goal_rates.items()
        },
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
        "strengths": {
            "home_attack": round(
                strengths["home_attack"],
                3,
            ),
            "home_defence": round(
                strengths["home_defence"],
                3,
            ),
            "away_attack": round(
                strengths["away_attack"],
                3,
            ),
            "away_defence": round(
                strengths["away_defence"],
                3,
            ),
        },
        "result_probabilities": {
            "home_win": round(
                home_win_probability,
                6,
            ),
            "draw": round(
                draw_probability,
                6,
            ),
            "away_win": round(
                away_win_probability,
                6,
            ),
            "home_win_percent": round(
                home_win_probability * 100,
                2,
            ),
            "draw_percent": round(
                draw_probability * 100,
                2,
            ),
            "away_win_percent": round(
                away_win_probability * 100,
                2,
            ),
        },
        "total_goals_lines": total_goals_lines,
        "goals_probabilities": {
            "over_2_5": round(
                over_2_5_probability,
                6,
            ),
            "under_2_5": round(
                1 - over_2_5_probability,
                6,
            ),
            "both_teams_score_yes": round(
                both_teams_score_probability,
                6,
            ),
            "both_teams_score_no": round(
                1
                - both_teams_score_probability,
                6,
            ),
            "over_2_5_percent": round(
                over_2_5_probability * 100,
                2,
            ),
            "under_2_5_percent": round(
                (
                    1 - over_2_5_probability
                )
                * 100,
                2,
            ),
            "both_teams_score_yes_percent": round(
                both_teams_score_probability
                * 100,
                2,
            ),
            "both_teams_score_no_percent": round(
                (
                    1
                    - both_teams_score_probability
                )
                * 100,
                2,
            ),
        },
        "most_likely_scores": (
            most_likely_scores
        ),
    }