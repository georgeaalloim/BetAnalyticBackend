import unittest

from draw_decision import (
    DEFAULT_DRAW_TREND_MARGIN,
    build_draw_tendency_context,
    get_match_draw_tendency,
    select_1x2_result,
)


def fixture(index: int, home: int, away: int, hg: int, ag: int, season: int = 2026):
    return {
        "fixture_id": index,
        "season": season,
        "fixture_date": f"2026-09-{index:02d}T18:00:00+00:00",
        "home_team_id": home,
        "home_team_name": f"T{home}",
        "away_team_id": away,
        "away_team_name": f"T{away}",
        "home_goals": hg,
        "away_goals": ag,
    }


class DrawTendencyTests(unittest.TestCase):
    def test_default_trend_margin_is_seven_point_five_percent(self) -> None:
        self.assertEqual(DEFAULT_DRAW_TREND_MARGIN, 0.075)

    def test_context_uses_last_five_home_and_away_matches(self) -> None:
        fixtures = [
            fixture(1, 1, 20, 1, 1),
            fixture(2, 1, 21, 2, 0),
            fixture(3, 1, 22, 0, 0),
            fixture(4, 1, 23, 1, 0),
            fixture(5, 1, 24, 2, 2),
            fixture(6, 1, 25, 3, 0),  # βγάζει τον αγώνα 1 από το last-5
            fixture(7, 30, 2, 0, 0),
            fixture(8, 31, 2, 1, 0),
            fixture(9, 32, 2, 2, 2),
            fixture(10, 33, 2, 1, 0),
            fixture(11, 34, 2, 0, 1),
        ]
        context = build_draw_tendency_context(fixtures, target_season=2026)
        trend = get_match_draw_tendency(context, home_team_id=1, away_team_id=2)

        self.assertEqual(trend["home_recent_home_matches"], 5)
        self.assertEqual(trend["home_recent_home_draws"], 2)
        self.assertEqual(trend["away_recent_away_matches"], 5)
        self.assertEqual(trend["away_recent_away_draws"], 2)
        self.assertTrue(trend["eligible"])

    def test_trend_requires_at_least_three_combined_draws(self) -> None:
        context = {
            "teams": {
                1: {"home_matches": 5, "home_draws": 1, "away_matches": 0, "away_draws": 0},
                2: {"home_matches": 0, "home_draws": 0, "away_matches": 5, "away_draws": 1},
            }
        }
        trend = get_match_draw_tendency(context, home_team_id=1, away_team_id=2)
        self.assertFalse(trend["eligible"])

    def test_eligible_trend_can_select_draw_inside_extended_margin(self) -> None:
        probabilities = {"HOME": 0.390, "DRAW": 0.325, "AWAY": 0.285}
        trend = {
            "eligible": True,
            "home_recent_home_matches": 5,
            "home_recent_home_draws": 2,
            "away_recent_away_matches": 5,
            "away_recent_away_draws": 1,
            "combined_recent_draws": 3,
        }
        self.assertEqual(
            select_1x2_result(probabilities, draw_tendency=trend),
            "DRAW",
        )

    def test_same_probabilities_remain_home_without_trend(self) -> None:
        probabilities = {"HOME": 0.390, "DRAW": 0.325, "AWAY": 0.285}
        self.assertEqual(select_1x2_result(probabilities), "HOME")

    def test_trend_does_not_override_large_probability_gap(self) -> None:
        probabilities = {"HOME": 0.500, "DRAW": 0.300, "AWAY": 0.200}
        trend = {"eligible": True}
        self.assertEqual(
            select_1x2_result(probabilities, draw_tendency=trend),
            "HOME",
        )

    def test_target_season_filters_old_draws(self) -> None:
        fixtures = [
            fixture(1, 1, 2, 1, 1, season=2025),
            fixture(2, 1, 2, 1, 1, season=2025),
            fixture(3, 1, 2, 1, 1, season=2025),
            fixture(4, 1, 2, 1, 1, season=2025),
            fixture(5, 1, 2, 1, 1, season=2025),
        ]
        context = build_draw_tendency_context(fixtures, target_season=2026)
        trend = get_match_draw_tendency(context, home_team_id=1, away_team_id=2)
        self.assertFalse(trend["eligible"])
        self.assertEqual(trend["home_recent_home_matches"], 0)


if __name__ == "__main__":
    unittest.main()
