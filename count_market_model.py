from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite, log, sqrt
from typing import Any, Iterable


MODEL_NAME = "Bayesian-Smoothed Count Markets v0.1"
PRIOR_MATCHES = 5.0
MINIMUM_LEAGUE_MATCHES = 60
MINIMUM_TEAM_VENUE_MATCHES = 4

MARKET_CONFIG: dict[str, dict[str, Any]] = {
    "corners": {
        "home_field": "home_corners",
        "away_field": "away_corners",
        "lines": tuple(value + 0.5 for value in range(5, 14)),
        "relevant_distance": 1.5,
        "minimum_expected_team": 0.35,
        "maximum_expected_team": 11.0,
    },
    "yellow_cards": {
        "home_field": "home_yellow_cards",
        "away_field": "away_yellow_cards",
        "lines": tuple(value + 0.5 for value in range(1, 8)),
        "relevant_distance": 1.0,
        "minimum_expected_team": 0.15,
        "maximum_expected_team": 6.0,
    },
}


@dataclass(frozen=True)
class TeamVenueStats:
    home_for: tuple[float, ...]
    home_against: tuple[float, ...]
    away_for: tuple[float, ...]
    away_against: tuple[float, ...]


@dataclass(frozen=True)
class CountMarketContext:
    market: str
    fixtures_used: int
    league_home_average: float
    league_away_average: float
    teams: dict[int, TeamVenueStats]


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _smoothed_mean(values: tuple[float, ...], prior: float) -> float:
    return (sum(values) + PRIOR_MATCHES * prior) / (len(values) + PRIOR_MATCHES)


def _poisson_under_probability(expected_total: float, line: float) -> float:
    maximum_count = int(line)
    probability = 0.0
    term = exp(-expected_total)
    probability += term
    for count in range(1, maximum_count + 1):
        term *= expected_total / count
        probability += term
    return min(max(probability, 0.0), 1.0)


def calculate_market_lines(
    expected_total: float,
    lines: Iterable[float],
) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    for raw_line in lines:
        line = float(raw_line)
        under = _poisson_under_probability(expected_total, line)
        over = 1.0 - under
        result.append(
            {
                "line": line,
                "over": round(over, 8),
                "under": round(under, 8),
                "over_percent": round(over * 100.0, 2),
                "under_percent": round(under * 100.0, 2),
            }
        )
    return result


def _relevant_rows(
    all_lines: list[dict[str, float]],
    *,
    expected_total: float,
    distance: float,
) -> list[dict[str, float]]:
    candidates = [
        row
        for row in all_lines
        if abs(float(row["line"]) - expected_total) <= distance
    ]
    if len(candidates) >= 2:
        return candidates
    return sorted(
        all_lines,
        key=lambda row: abs(float(row["line"]) - expected_total),
    )[:2]


def select_most_probable_relevant_line(
    *,
    expected_total: float,
    all_lines: list[dict[str, float]],
    relevant_distance: float,
) -> dict[str, Any]:
    candidates = _relevant_rows(
        all_lines,
        expected_total=expected_total,
        distance=relevant_distance,
    )
    choices: list[tuple[float, str, float]] = []
    for row in candidates:
        choices.append((float(row["over"]), "OVER", float(row["line"])))
        choices.append((float(row["under"]), "UNDER", float(row["line"])))

    probability, side, line = max(
        choices,
        key=lambda item: (item[0], -abs(item[2] - expected_total)),
    )
    return {
        "side": side,
        "line": line,
        "label": f"{side.title()} {line:.1f}",
        "probability": round(probability, 8),
        "probability_percent": round(probability * 100.0, 2),
        "expected_total": round(expected_total, 3),
        "candidate_lines": sorted({float(row["line"]) for row in candidates}),
        "selection_rule": (
            "Επιλέγεται η πιθανότερη πλευρά μόνο ανάμεσα σε γραμμές "
            "κοντά στην αναμενόμενη συνολική τιμή, ώστε να αποκλείονται "
            "τεχνητά εύκολες ακραίες γραμμές."
        ),
    }


def build_count_market_context(
    records: Iterable[dict[str, Any]],
    *,
    market: str,
) -> CountMarketContext:
    if market not in MARKET_CONFIG:
        raise ValueError(f"Άγνωστη αγορά μετρήσεων: {market}")
    config = MARKET_CONFIG[market]
    home_field = str(config["home_field"])
    away_field = str(config["away_field"])

    valid: list[tuple[int, int, float, float]] = []
    for record in records:
        try:
            home_team_id = int(record["home_team_id"])
            away_team_id = int(record["away_team_id"])
        except (KeyError, TypeError, ValueError):
            continue
        home_value = _number(record.get(home_field))
        away_value = _number(record.get(away_field))
        if home_value is None or away_value is None:
            continue
        valid.append((home_team_id, away_team_id, home_value, away_value))

    if len(valid) < MINIMUM_LEAGUE_MATCHES:
        raise ValueError(
            f"Απαιτούνται τουλάχιστον {MINIMUM_LEAGUE_MATCHES} αγώνες "
            f"με πλήρη δεδομένα {market}. Διαθέσιμοι: {len(valid)}."
        )

    league_home_average = sum(item[2] for item in valid) / len(valid)
    league_away_average = sum(item[3] for item in valid) / len(valid)
    mutable: dict[int, dict[str, list[float]]] = {}

    def team_bucket(team_id: int) -> dict[str, list[float]]:
        return mutable.setdefault(
            team_id,
            {
                "home_for": [],
                "home_against": [],
                "away_for": [],
                "away_against": [],
            },
        )

    for home_team_id, away_team_id, home_value, away_value in valid:
        home_bucket = team_bucket(home_team_id)
        away_bucket = team_bucket(away_team_id)
        home_bucket["home_for"].append(home_value)
        home_bucket["home_against"].append(away_value)
        away_bucket["away_for"].append(away_value)
        away_bucket["away_against"].append(home_value)

    teams = {
        team_id: TeamVenueStats(
            home_for=tuple(bucket["home_for"]),
            home_against=tuple(bucket["home_against"]),
            away_for=tuple(bucket["away_for"]),
            away_against=tuple(bucket["away_against"]),
        )
        for team_id, bucket in mutable.items()
    }
    return CountMarketContext(
        market=market,
        fixtures_used=len(valid),
        league_home_average=league_home_average,
        league_away_average=league_away_average,
        teams=teams,
    )


def predict_count_market(
    context: CountMarketContext,
    *,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, Any]:
    config = MARKET_CONFIG[context.market]
    home = context.teams.get(int(home_team_id))
    away = context.teams.get(int(away_team_id))
    if home is None:
        raise ValueError(f"Δεν υπάρχουν ιστορικά {context.market} για τη γηπεδούχο.")
    if away is None:
        raise ValueError(f"Δεν υπάρχουν ιστορικά {context.market} για τη φιλοξενούμενη.")
    if len(home.home_for) < MINIMUM_TEAM_VENUE_MATCHES:
        raise ValueError(
            f"Η γηπεδούχος έχει μόνο {len(home.home_for)} εντός έδρας "
            f"αγώνες με δεδομένα {context.market}."
        )
    if len(away.away_for) < MINIMUM_TEAM_VENUE_MATCHES:
        raise ValueError(
            f"Η φιλοξενούμενη έχει μόνο {len(away.away_for)} εκτός έδρας "
            f"αγώνες με δεδομένα {context.market}."
        )

    home_for = _smoothed_mean(home.home_for, context.league_home_average)
    away_allowed = _smoothed_mean(away.away_against, context.league_home_average)
    away_for = _smoothed_mean(away.away_for, context.league_away_average)
    home_allowed = _smoothed_mean(home.home_against, context.league_away_average)

    minimum = float(config["minimum_expected_team"])
    maximum = float(config["maximum_expected_team"])
    expected_home = min(max(sqrt(home_for * away_allowed), minimum), maximum)
    expected_away = min(max(sqrt(away_for * home_allowed), minimum), maximum)
    expected_total = expected_home + expected_away

    all_lines = calculate_market_lines(expected_total, config["lines"])
    selected = select_most_probable_relevant_line(
        expected_total=expected_total,
        all_lines=all_lines,
        relevant_distance=float(config["relevant_distance"]),
    )
    return {
        "status": "ready",
        "model": MODEL_NAME,
        "market": context.market,
        "fixtures_used": context.fixtures_used,
        "prior_matches": PRIOR_MATCHES,
        "minimum_team_venue_matches": MINIMUM_TEAM_VENUE_MATCHES,
        "expected": {
            "home": round(expected_home, 3),
            "away": round(expected_away, 3),
            "total": round(expected_total, 3),
        },
        "selected": selected,
        "all_lines": all_lines,
    }


def unavailable_market(error: str, fixtures_used: int = 0) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "model": MODEL_NAME,
        "fixtures_used": int(fixtures_used),
        "error": str(error),
        "expected": None,
        "selected": None,
        "all_lines": [],
    }


def walk_forward_backtest(
    records: Iterable[dict[str, Any]],
    *,
    market: str,
    minimum_training_matches: int = MINIMUM_LEAGUE_MATCHES,
) -> dict[str, Any]:
    config = MARKET_CONFIG[market]
    home_field = str(config["home_field"])
    away_field = str(config["away_field"])
    ordered = [
        dict(record)
        for record in records
        if _number(record.get(home_field)) is not None
        and _number(record.get(away_field)) is not None
    ]
    ordered.sort(key=lambda item: (str(item.get("fixture_date") or ""), int(item.get("fixture_id") or 0)))

    evaluated = 0
    correct = 0
    brier_sum = 0.0
    log_loss_sum = 0.0
    skipped = 0
    for index in range(minimum_training_matches, len(ordered)):
        target = ordered[index]
        try:
            context = build_count_market_context(ordered[:index], market=market)
            prediction = predict_count_market(
                context,
                home_team_id=int(target["home_team_id"]),
                away_team_id=int(target["away_team_id"]),
            )
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue

        selected = prediction["selected"]
        actual_total = float(target[home_field]) + float(target[away_field])
        side = str(selected["side"])
        line = float(selected["line"])
        event_happened = actual_total > line if side == "OVER" else actual_total < line
        outcome = 1.0 if event_happened else 0.0
        probability = min(max(float(selected["probability"]), 1e-9), 1.0 - 1e-9)

        evaluated += 1
        correct += int(event_happened)
        brier_sum += (probability - outcome) ** 2
        log_loss_sum += -(outcome * log(probability) + (1.0 - outcome) * log(1.0 - probability))

    return {
        "market": market,
        "method": "strict walk-forward; each match uses only earlier statistics",
        "records_available": len(ordered),
        "predictions_evaluated": evaluated,
        "predictions_skipped_for_team_history": skipped,
        "accuracy_percent": round(correct / evaluated * 100.0, 2) if evaluated else None,
        "brier_score": round(brier_sum / evaluated, 4) if evaluated else None,
        "log_loss": round(log_loss_sum / evaluated, 4) if evaluated else None,
    }
