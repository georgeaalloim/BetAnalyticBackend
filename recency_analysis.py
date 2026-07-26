
from datetime import datetime
from typing import Any


def parse_fixture_datetime(value: str) -> datetime:
    """
    Μετατρέπει ημερομηνία ISO 8601 σε datetime.
    Υποστηρίζει και μορφή που τελειώνει σε Z.
    """

    if not value:
        raise ValueError(
            "Η ημερομηνία του αγώνα δεν μπορεί να είναι κενή."
        )

    normalized_value = value.replace(
        "Z",
        "+00:00",
    )

    try:
        return datetime.fromisoformat(
            normalized_value
        )
    except ValueError as error:
        raise ValueError(
            f"Μη έγκυρη ημερομηνία αγώνα: {value}"
        ) from error


def calculate_recency_weight(
    fixture_date: str,
    reference_date: str,
    half_life_days: float,
) -> float:
    """
    Υπολογίζει εκθετικό βάρος παλαιότητας.

    half_life_days:
        Μετά από τόσες ημέρες, το βάρος ενός
        αγώνα γίνεται το μισό.

    Παράδειγμα με half_life_days=90:
        σημερινός αγώνας -> βάρος 1.00
        90 ημέρες πριν   -> βάρος 0.50
        180 ημέρες πριν  -> βάρος 0.25
    """

    if half_life_days <= 0:
        raise ValueError(
            "Το half_life_days πρέπει να είναι θετικό."
        )

    fixture_datetime = parse_fixture_datetime(
        fixture_date
    )

    reference_datetime = parse_fixture_datetime(
        reference_date
    )

    age_seconds = (
        reference_datetime - fixture_datetime
    ).total_seconds()

    # Προστασία σε περίπτωση λανθασμένης
    # μελλοντικής ημερομηνίας.
    age_days = max(
        age_seconds / 86400.0,
        0.0,
    )

    return 0.5 ** (
        age_days / half_life_days
    )


def create_empty_weighted_statistics() -> dict[str, Any]:
    """
    Δημιουργεί κενά σταθμισμένα στατιστικά
    για εντός ή εκτός έδρας αγώνες.
    """

    return {
        "matches": 0.0,
        "actual_matches": 0,
        "wins": 0.0,
        "draws": 0.0,
        "losses": 0.0,
        "goals_for": 0.0,
        "goals_against": 0.0,
        "points": 0.0,
        "goals_for_per_match": 0.0,
        "goals_against_per_match": 0.0,
        "points_per_match": 0.0,
    }


def finalize_weighted_statistics(
    statistics: dict[str, Any],
) -> None:
    """
    Υπολογίζει τους σταθμισμένους μέσους όρους.
    """

    effective_matches = float(
        statistics["matches"]
    )

    if effective_matches <= 0:
        return

    statistics["goals_for_per_match"] = (
        float(statistics["goals_for"])
        / effective_matches
    )

    statistics["goals_against_per_match"] = (
        float(statistics["goals_against"])
        / effective_matches
    )

    statistics["points_per_match"] = (
        float(statistics["points"])
        / effective_matches
    )


def calculate_recency_weighted_home_away_statistics(
    fixtures: list[dict[str, Any]],
    reference_date: str,
    half_life_days: float,
) -> dict[str, Any]:
    """
    Υπολογίζει εντός/εκτός έδρας στατιστικά
    με μεγαλύτερη βαρύτητα στους πρόσφατους αγώνες.

    Η δομή της απάντησης είναι συμβατή με τη
    calculate_home_away_statistics(), ώστε να
    μπορεί να χρησιμοποιηθεί από το Poisson μοντέλο.
    """

    if not fixtures:
        raise ValueError(
            "Δεν δόθηκαν αγώνες για σταθμισμένη ανάλυση."
        )

    if half_life_days <= 0:
        raise ValueError(
            "Το half_life_days πρέπει να είναι θετικό."
        )

    teams: dict[int, dict[str, Any]] = {}

    total_weight = 0.0
    weighted_home_goals = 0.0
    weighted_away_goals = 0.0
    actual_matches = 0

    for fixture in fixtures:
        fixture_date = fixture.get(
            "fixture_date"
        )

        if not fixture_date:
            continue

        weight = calculate_recency_weight(
            fixture_date=fixture_date,
            reference_date=reference_date,
            half_life_days=half_life_days,
        )

        home_team_id = int(
            fixture["home_team_id"]
        )

        away_team_id = int(
            fixture["away_team_id"]
        )

        home_team_name = str(
            fixture["home_team_name"]
        )

        away_team_name = str(
            fixture["away_team_name"]
        )

        home_goals = float(
            fixture["home_goals"]
        )

        away_goals = float(
            fixture["away_goals"]
        )

        if home_team_id not in teams:
            teams[home_team_id] = {
                "team_id": home_team_id,
                "team_name": home_team_name,
                "home": (
                    create_empty_weighted_statistics()
                ),
                "away": (
                    create_empty_weighted_statistics()
                ),
            }

        if away_team_id not in teams:
            teams[away_team_id] = {
                "team_id": away_team_id,
                "team_name": away_team_name,
                "home": (
                    create_empty_weighted_statistics()
                ),
                "away": (
                    create_empty_weighted_statistics()
                ),
            }

        home_statistics = teams[
            home_team_id
        ]["home"]

        away_statistics = teams[
            away_team_id
        ]["away"]

        actual_matches += 1
        total_weight += weight

        weighted_home_goals += (
            home_goals * weight
        )

        weighted_away_goals += (
            away_goals * weight
        )

        home_statistics["actual_matches"] += 1
        away_statistics["actual_matches"] += 1

        home_statistics["matches"] += weight
        away_statistics["matches"] += weight

        home_statistics["goals_for"] += (
            home_goals * weight
        )

        home_statistics["goals_against"] += (
            away_goals * weight
        )

        away_statistics["goals_for"] += (
            away_goals * weight
        )

        away_statistics["goals_against"] += (
            home_goals * weight
        )

        if home_goals > away_goals:
            home_statistics["wins"] += weight
            home_statistics["points"] += (
                3.0 * weight
            )

            away_statistics["losses"] += weight

        elif home_goals < away_goals:
            away_statistics["wins"] += weight
            away_statistics["points"] += (
                3.0 * weight
            )

            home_statistics["losses"] += weight

        else:
            home_statistics["draws"] += weight
            away_statistics["draws"] += weight

            home_statistics["points"] += weight
            away_statistics["points"] += weight

    if total_weight <= 0:
        raise ValueError(
            "Δεν προέκυψε θετικό συνολικό βάρος αγώνων."
        )

    team_list = list(
        teams.values()
    )

    for team in team_list:
        finalize_weighted_statistics(
            team["home"]
        )

        finalize_weighted_statistics(
            team["away"]
        )

    team_list.sort(
        key=lambda team: team["team_name"],
    )

    return {
        "analysis_type": "recency_weighted",
        "reference_date": reference_date,
        "half_life_days": half_life_days,
        "league_averages": {
            "matches": total_weight,
            "actual_matches": actual_matches,
            "total_home_goals": (
                weighted_home_goals
            ),
            "total_away_goals": (
                weighted_away_goals
            ),
            "home_goals_per_match": (
                weighted_home_goals
                / total_weight
            ),
            "away_goals_per_match": (
                weighted_away_goals
                / total_weight
            ),
            "total_goals_per_match": (
                (
                    weighted_home_goals
                    + weighted_away_goals
                )
                / total_weight
            ),
        },
        "teams": team_list,
    }
