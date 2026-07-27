from typing import Any


def create_empty_team(
    team_id: int,
    team_name: str,
) -> dict[str, Any]:
    return {
        "team_id": team_id,
        "team_name": team_name,
        "matches": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "goals_for": 0,
        "goals_against": 0,
        "goal_difference": 0,
        "points": 0,
        "goals_for_per_match": 0.0,
        "goals_against_per_match": 0.0,
        "points_per_match": 0.0,
    }


def calculate_team_statistics(
    fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    teams: dict[int, dict[str, Any]] = {}

    for fixture in fixtures:
        home_team_id = fixture["home_team_id"]
        home_team_name = fixture["home_team_name"]
        away_team_id = fixture["away_team_id"]
        away_team_name = fixture["away_team_name"]

        home_goals = fixture["home_goals"]
        away_goals = fixture["away_goals"]

        if home_team_id not in teams:
            teams[home_team_id] = create_empty_team(
                home_team_id,
                home_team_name,
            )

        if away_team_id not in teams:
            teams[away_team_id] = create_empty_team(
                away_team_id,
                away_team_name,
            )

        home_team = teams[home_team_id]
        away_team = teams[away_team_id]

        home_team["matches"] += 1
        away_team["matches"] += 1

        home_team["goals_for"] += home_goals
        home_team["goals_against"] += away_goals

        away_team["goals_for"] += away_goals
        away_team["goals_against"] += home_goals

        if home_goals > away_goals:
            home_team["wins"] += 1
            home_team["points"] += 3
            away_team["losses"] += 1

        elif home_goals < away_goals:
            away_team["wins"] += 1
            away_team["points"] += 3
            home_team["losses"] += 1

        else:
            home_team["draws"] += 1
            away_team["draws"] += 1

            home_team["points"] += 1
            away_team["points"] += 1

    team_statistics = list(teams.values())

    for team in team_statistics:
        matches = team["matches"]

        team["goal_difference"] = (
            team["goals_for"] - team["goals_against"]
        )

        if matches > 0:
            team["goals_for_per_match"] = round(
                team["goals_for"] / matches,
                2,
            )

            team["goals_against_per_match"] = round(
                team["goals_against"] / matches,
                2,
            )

            team["points_per_match"] = round(
                team["points"] / matches,
                2,
            )

    team_statistics.sort(
        key=lambda team: (
            team["points"],
            team["goal_difference"],
            team["goals_for"],
        ),
        reverse=True,
    )

    return team_statistics
def create_empty_location_statistics() -> dict[str, Any]:
    """
    Δημιουργεί την αρχική εγγραφή για εντός
    ή εκτός έδρας στατιστικά.
    """

    return {
        "matches": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "goals_for": 0,
        "goals_against": 0,
        "goals_for_per_match": 0.0,
        "goals_against_per_match": 0.0,
        "points": 0,
        "points_per_match": 0.0,
    }


def finalize_location_statistics(
    statistics: dict[str, Any],
) -> None:
    """
    Υπολογίζει τους μέσους όρους μιας εγγραφής
    εντός ή εκτός έδρας.
    """

    matches = statistics["matches"]

    if matches == 0:
        return

    statistics["goals_for_per_match"] = round(
        statistics["goals_for"] / matches,
        3,
    )

    statistics["goals_against_per_match"] = round(
        statistics["goals_against"] / matches,
        3,
    )

    statistics["points_per_match"] = round(
        statistics["points"] / matches,
        3,
    )


def calculate_home_away_statistics(
    fixtures: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Υπολογίζει ξεχωριστά την εντός και εκτός έδρας
    απόδοση κάθε ομάδας.
    """

    teams: dict[int, dict[str, Any]] = {}

    total_home_goals = 0
    total_away_goals = 0

    for fixture in fixtures:
        home_team_id = fixture["home_team_id"]
        home_team_name = fixture["home_team_name"]

        away_team_id = fixture["away_team_id"]
        away_team_name = fixture["away_team_name"]

        home_goals = fixture["home_goals"]
        away_goals = fixture["away_goals"]

        if home_team_id not in teams:
            teams[home_team_id] = {
                "team_id": home_team_id,
                "team_name": home_team_name,
                "home": create_empty_location_statistics(),
                "away": create_empty_location_statistics(),
            }

        if away_team_id not in teams:
            teams[away_team_id] = {
                "team_id": away_team_id,
                "team_name": away_team_name,
                "home": create_empty_location_statistics(),
                "away": create_empty_location_statistics(),
            }

        home_statistics = teams[home_team_id]["home"]
        away_statistics = teams[away_team_id]["away"]

        # Συνολικά γκολ της διοργάνωσης.
        total_home_goals += home_goals
        total_away_goals += away_goals

        # Αγώνες.
        home_statistics["matches"] += 1
        away_statistics["matches"] += 1

        # Γκολ.
        home_statistics["goals_for"] += home_goals
        home_statistics["goals_against"] += away_goals

        away_statistics["goals_for"] += away_goals
        away_statistics["goals_against"] += home_goals

        # Αποτέλεσμα.
        if home_goals > away_goals:
            home_statistics["wins"] += 1
            home_statistics["points"] += 3

            away_statistics["losses"] += 1

        elif home_goals < away_goals:
            away_statistics["wins"] += 1
            away_statistics["points"] += 3

            home_statistics["losses"] += 1

        else:
            home_statistics["draws"] += 1
            away_statistics["draws"] += 1

            home_statistics["points"] += 1
            away_statistics["points"] += 1

    team_list = list(teams.values())

    for team in team_list:
        finalize_location_statistics(team["home"])
        finalize_location_statistics(team["away"])

    team_list.sort(
        key=lambda team: team["team_name"],
    )

    number_of_matches = len(fixtures)

    league_averages = {
        "matches": number_of_matches,
        "total_home_goals": total_home_goals,
        "total_away_goals": total_away_goals,
        "home_goals_per_match": round(
            total_home_goals / number_of_matches,
            3,
        ),
        "away_goals_per_match": round(
            total_away_goals / number_of_matches,
            3,
        ),
        "total_goals_per_match": round(
            (total_home_goals + total_away_goals)
            / number_of_matches,
            3,
        ),
    }

    return {
        "league_averages": league_averages,
        "teams": team_list,
    }