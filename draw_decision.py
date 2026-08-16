from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable, Mapping


RESULT_LABELS = ("HOME", "DRAW", "AWAY")

# Βασικός, πολύ στενός tie-breaker που ήδη χρησιμοποιούσε το production.
DEFAULT_DRAW_TIE_MARGIN = 0.015

# Δεύτερο επίπεδο: όταν και οι δύο ομάδες έχουν πρόσφατη τάση προς Χ
# στο αντίστοιχο venue, επιτρέπεται ελαφρώς μεγαλύτερη απόσταση του DRAW
# από τον ισχυρότερο HOME/AWAY ανταγωνιστή.
DEFAULT_DRAW_TREND_MARGIN = 0.075
DRAW_TREND_WINDOW = 5
MIN_VENUE_MATCHES_FOR_TREND = 5
MIN_DRAWS_EACH_TEAM = 1
MIN_COMBINED_DRAWS = 3


def _is_completed_draw(fixture: Mapping[str, Any]) -> bool:
    home_goals = fixture.get("home_goals")
    away_goals = fixture.get("away_goals")
    if home_goals is None or away_goals is None:
        return False
    return int(home_goals) == int(away_goals)


def build_draw_tendency_context(
    fixtures: Iterable[Mapping[str, Any]],
    *,
    window: int = DRAW_TREND_WINDOW,
    target_season: int | None = None,
) -> dict[str, Any]:
    """
    Χτίζει μικρό, χρονικά ασφαλές context για την πρόσφατη τάση ισοπαλίας.

    Για τον μελλοντικό γηπεδούχο κρατά τους τελευταίους `window` ΕΝΤΟΣ
    αγώνες του. Για τον μελλοντικό φιλοξενούμενο κρατά τους τελευταίους
    `window` ΕΚΤΟΣ αγώνες του.

    Όταν δίνεται target_season, η τάση υπολογίζεται μόνο από την ίδια
    σεζόν. Αυτό αποτρέπει το να μεταφέρουμε ισχυρό draw signal από παλιά
    σεζόν στην αρχή μιας νέας, ενώ το βασικό goal model μπορεί κανονικά να
    χρησιμοποιεί μεγαλύτερο training window.
    """

    if window < 1:
        raise ValueError("Το draw trend window πρέπει να είναι τουλάχιστον 1.")

    selected: list[Mapping[str, Any]] = []
    for fixture in fixtures:
        if fixture.get("home_goals") is None or fixture.get("away_goals") is None:
            continue
        if target_season is not None and fixture.get("season") is not None:
            if int(fixture["season"]) != int(target_season):
                continue
        selected.append(fixture)

    selected.sort(key=lambda item: str(item.get("fixture_date") or ""))

    home_history: dict[int, deque[bool]] = defaultdict(lambda: deque(maxlen=window))
    away_history: dict[int, deque[bool]] = defaultdict(lambda: deque(maxlen=window))

    for fixture in selected:
        home_team_id = int(fixture["home_team_id"])
        away_team_id = int(fixture["away_team_id"])
        draw = _is_completed_draw(fixture)
        home_history[home_team_id].append(draw)
        away_history[away_team_id].append(draw)

    team_ids = set(home_history) | set(away_history)
    teams: dict[int, dict[str, int]] = {}

    for team_id in team_ids:
        home_values = list(home_history.get(team_id, ()))
        away_values = list(away_history.get(team_id, ()))
        teams[int(team_id)] = {
            "home_matches": len(home_values),
            "home_draws": sum(1 for value in home_values if value),
            "away_matches": len(away_values),
            "away_draws": sum(1 for value in away_values if value),
        }

    return {
        "window": int(window),
        "target_season": int(target_season) if target_season is not None else None,
        "teams": teams,
    }


def get_match_draw_tendency(
    context: Mapping[str, Any] | None,
    *,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, Any]:
    """Επιστρέφει το venue-specific draw signal για έναν συγκεκριμένο αγώνα."""

    default = {
        "eligible": False,
        "home_recent_home_matches": 0,
        "home_recent_home_draws": 0,
        "away_recent_away_matches": 0,
        "away_recent_away_draws": 0,
        "combined_recent_draws": 0,
        "reason": "Δεν υπάρχει draw tendency context.",
    }

    if not context:
        return default

    teams = context.get("teams")
    if not isinstance(teams, Mapping):
        return default

    home_stats = teams.get(int(home_team_id), {})
    away_stats = teams.get(int(away_team_id), {})

    home_matches = int(home_stats.get("home_matches", 0))
    home_draws = int(home_stats.get("home_draws", 0))
    away_matches = int(away_stats.get("away_matches", 0))
    away_draws = int(away_stats.get("away_draws", 0))
    combined_draws = home_draws + away_draws

    enough_history = (
        home_matches >= MIN_VENUE_MATCHES_FOR_TREND
        and away_matches >= MIN_VENUE_MATCHES_FOR_TREND
    )
    enough_each = (
        home_draws >= MIN_DRAWS_EACH_TEAM
        and away_draws >= MIN_DRAWS_EACH_TEAM
    )
    enough_combined = combined_draws >= MIN_COMBINED_DRAWS
    eligible = enough_history and enough_each and enough_combined

    if not enough_history:
        reason = "Δεν υπάρχουν 5 πρόσφατοι venue-specific αγώνες και για τις δύο ομάδες."
    elif not enough_each:
        reason = "Τουλάχιστον μία ομάδα δεν έχει πρόσφατη venue-specific ισοπαλία."
    elif not enough_combined:
        reason = "Οι δύο ομάδες δεν έχουν τουλάχιστον 3 συνολικές ισοπαλίες στους 10 venue-specific αγώνες."
    else:
        reason = "Ενεργό draw tendency signal."

    return {
        "eligible": bool(eligible),
        "home_recent_home_matches": home_matches,
        "home_recent_home_draws": home_draws,
        "away_recent_away_matches": away_matches,
        "away_recent_away_draws": away_draws,
        "combined_recent_draws": combined_draws,
        "reason": reason,
    }


def select_1x2_result(
    probabilities: Mapping[str, float],
    draw_tie_margin: float = DEFAULT_DRAW_TIE_MARGIN,
    *,
    draw_tendency: Mapping[str, Any] | None = None,
    draw_trend_margin: float = DEFAULT_DRAW_TREND_MARGIN,
) -> str:
    """
    Επιλέγει HOME/DRAW/AWAY χωρίς να μεταβάλλει τις πιθανότητες.

    1) Κανονικά ισχύει ο στενός draw tie-breaker (1,5 π.μ.).
    2) Αν υπάρχει πραγματικό πρόσφατο draw tendency και για τις δύο ομάδες,
       επιτρέπεται DRAW όταν βρίσκεται έως 7,5 π.μ. από τον ισχυρότερο 1/2.

    Δεν επηρεάζονται expected goals, Over/Under, BTTS, corners ή οι ίδιες οι
    πιθανότητες 1X2. Αλλάζει μόνο η τελική επιλογή predicted_result.
    """

    for name, margin in (
        ("draw_tie_margin", draw_tie_margin),
        ("draw_trend_margin", draw_trend_margin),
    ):
        if margin < 0 or margin > 0.15:
            raise ValueError(
                f"Το {name} πρέπει να βρίσκεται στο διάστημα 0 έως 0.15."
            )

    missing = [label for label in RESULT_LABELS if label not in probabilities]
    if missing:
        raise ValueError("Λείπουν πιθανότητες για: " + ", ".join(missing) + ".")

    values = {label: float(probabilities[label]) for label in RESULT_LABELS}

    if any(value < 0 for value in values.values()):
        raise ValueError("Οι πιθανότητες δεν μπορούν να είναι αρνητικές.")

    if sum(values.values()) <= 0:
        raise ValueError("Το άθροισμα των πιθανοτήτων πρέπει να είναι θετικό.")

    strongest_home_or_away = max(values["HOME"], values["AWAY"])

    if values["DRAW"] >= strongest_home_or_away - draw_tie_margin:
        return "DRAW"

    trend_is_eligible = bool(draw_tendency and draw_tendency.get("eligible"))
    if (
        trend_is_eligible
        and values["DRAW"] >= strongest_home_or_away - draw_trend_margin
    ):
        return "DRAW"

    return "HOME" if values["HOME"] >= values["AWAY"] else "AWAY"
