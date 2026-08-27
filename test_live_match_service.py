from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from live_match_service import (
    _parse_statistics,
    _parse_timeline,
    build_live_prediction,
    refresh_live_matches,
    select_live_candidates,
)


CANDIDATE = {
    "fixture_id": 9001,
    "fixture_date": "2026-08-30T18:00:00Z",
    "kickoff_time_confirmed": True,
    "status": "NS",
    "home_team": {"team_id": 553, "team_name": "Olympiakos Piraeus"},
    "away_team": {"team_id": 1123, "team_name": "Aris Thessalonikis"},
    "prediction": {"expected_goals": {"home": 1.8, "away": 0.9}},
}


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, params: dict[str, Any], timeout: int) -> _Response:
        self.calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/searchevents.php"):
            return _Response({"event": [{
                "idEvent": "evt-live",
                "idLeague": "4336",
                "dateEvent": "2026-08-30",
                "strHomeTeam": "Olympiacos",
                "strAwayTeam": "Aris",
                "intHomeScore": "1",
                "intAwayScore": "0",
                "strStatus": "2H",
                "strProgress": "63",
            }]})
        if url.endswith("/lookupevent.php"):
            return _Response({"events": [{
                "idEvent": "evt-live",
                "idLeague": "4336",
                "dateEvent": "2026-08-30",
                "strHomeTeam": "Olympiacos",
                "strAwayTeam": "Aris",
                "intHomeScore": "1",
                "intAwayScore": "0",
                "strStatus": "2H",
                "strProgress": "63",
            }]})
        if url.endswith("/lookuptimeline.php"):
            return _Response({"timeline": [{
                "strTimeline": "Goal",
                "strPlayer": "Example Scorer",
                "strHome": "Yes",
                "intTime": "40",
            }]})
        if url.endswith("/lookupeventstats.php"):
            return _Response({"eventstats": [
                {"strStat": "Corner Kicks", "intHome": "5", "intAway": "2"},
                {"strStat": "Shots on Goal", "intHome": "4", "intAway": "1"},
            ]})
        return _Response({})

    def close(self) -> None:
        return None


class LiveMatchServiceTests(unittest.TestCase):
    def test_select_live_candidates_uses_dedicated_catalog_after_kickoff(self) -> None:
        feed = {"live_candidates": [CANDIDATE], "seasons": []}
        selected = select_live_candidates(
            feed,
            as_of=datetime(2026, 8, 30, 18, 45, tzinfo=timezone.utc),
        )
        self.assertEqual([9001], [item["fixture_id"] for item in selected])

    def test_unconfirmed_kickoff_is_not_live_candidate(self) -> None:
        item = dict(CANDIDATE)
        item["kickoff_time_confirmed"] = False
        selected = select_live_candidates(
            {"live_candidates": [item]},
            as_of=datetime(2026, 8, 30, 18, 10, tzinfo=timezone.utc),
        )
        self.assertEqual([], selected)

    def test_parse_statistics_by_name_not_fixed_position(self) -> None:
        payload = {
            "eventstats": [
                {"strStat": "Corner Kicks", "intHome": "7", "intAway": "2"},
                {"strStat": "Shots on Goal", "intHome": "5", "intAway": "1"},
                {"strStat": "Ball Possession", "intHome": "61%", "intAway": "39%"},
                {"strStat": "Yellow Cards", "intHome": "1", "intAway": "3"},
            ]
        }
        stats = _parse_statistics(payload)
        self.assertEqual({"home": 7, "away": 2}, stats["corners"])
        self.assertEqual({"home": 5, "away": 1}, stats["shots_on_target"])
        self.assertEqual({"home": 61.0, "away": 39.0}, stats["possession"])
        self.assertEqual({"home": 1, "away": 3}, stats["yellow_cards"])

    def test_timeline_keeps_goals_and_cards(self) -> None:
        timeline = _parse_timeline([
            {"strTimeline": "Goal", "strPlayer": "Player A", "strHome": "Yes", "intTime": "12"},
            {"strTimeline": "Yellow Card", "strPlayer": "Player B", "strHome": "No", "intTime": "44"},
            {"strTimeline": "Goal", "strTimelineDetail": "Penalty", "strPlayer": "Player C", "strHome": "No", "intTime": "90+2"},
        ], CANDIDATE)
        self.assertEqual(["goal", "yellow_card", "goal"], [item["type"] for item in timeline])
        self.assertEqual("Penalty", timeline[-1]["detail"])
        self.assertEqual(2, timeline[-1]["extra_minute"])

    def test_live_prediction_respects_current_score_and_is_normalized(self) -> None:
        prediction = build_live_prediction(
            CANDIDATE,
            home_score=2,
            away_score=0,
            minute=75,
            statistics={
                "shots_on_target": {"home": 6, "away": 1},
                "corners": {"home": 8, "away": 2},
                "red_cards": {"home": 0, "away": 0},
            },
        )
        result = prediction["result_probabilities_percent"]
        self.assertGreater(result["home_win"], result["away_win"])
        self.assertAlmostEqual(100.0, result["home_win"] + result["draw"] + result["away_win"], delta=0.2)
        next_goal = prediction["next_goal_percent"]
        self.assertAlmostEqual(100.0, next_goal["home"] + next_goal["away"] + next_goal["no_more_goal"], delta=0.2)

    def test_live_prediction_uses_total_shots_and_possession_for_dominance(self) -> None:
        neutral_candidate = dict(CANDIDATE)
        neutral_candidate["prediction"] = {"expected_goals": {"home": 1.2, "away": 1.2}}
        prediction = build_live_prediction(
            neutral_candidate,
            home_score=0,
            away_score=0,
            minute=60,
            statistics={
                "shots_on_target": {"home": 3, "away": 3},
                "shots": {"home": 16, "away": 6},
                "corners": {"home": 4, "away": 4},
                "possession": {"home": 66, "away": 34},
                "red_cards": {"home": 0, "away": 0},
            },
        )
        result = prediction["result_probabilities_percent"]
        dominance = prediction["live_dominance"]
        self.assertEqual("home", dominance["leader"])
        self.assertGreater(dominance["home_index_percent"], 50.0)
        self.assertGreater(result["home_win"], result["away_win"])
        self.assertIn("shots", dominance["available_inputs"])
        self.assertIn("possession", dominance["available_inputs"])

    def test_live_prediction_red_card_has_strong_separate_effect(self) -> None:
        neutral_candidate = dict(CANDIDATE)
        neutral_candidate["prediction"] = {"expected_goals": {"home": 1.3, "away": 1.3}}
        common = {
            "shots_on_target": {"home": 4, "away": 4},
            "shots": {"home": 10, "away": 10},
            "corners": {"home": 4, "away": 4},
            "possession": {"home": 50, "away": 50},
        }
        even = build_live_prediction(
            neutral_candidate, home_score=0, away_score=0, minute=55,
            statistics={**common, "red_cards": {"home": 0, "away": 0}},
        )
        home_red = build_live_prediction(
            neutral_candidate, home_score=0, away_score=0, minute=55,
            statistics={**common, "red_cards": {"home": 1, "away": 0}},
        )
        self.assertLess(
            home_red["result_probabilities_percent"]["home_win"],
            even["result_probabilities_percent"]["home_win"],
        )
        self.assertGreater(
            home_red["result_probabilities_percent"]["away_win"],
            even["result_probabilities_percent"]["away_win"],
        )

    def test_live_prediction_missing_optional_stats_remains_safe(self) -> None:
        prediction = build_live_prediction(
            CANDIDATE,
            home_score=1,
            away_score=1,
            minute=50,
            statistics={"red_cards": {"home": 0, "away": 0}},
        )
        self.assertEqual([], prediction["live_dominance"]["available_inputs"])
        result = prediction["result_probabilities_percent"]
        self.assertAlmostEqual(
            100.0, result["home_win"] + result["draw"] + result["away_win"], delta=0.2
        )


    def test_refresh_live_match_builds_score_stats_events_and_prediction(self) -> None:
        session = _Session()
        result = refresh_live_matches(
            {"live_candidates": [CANDIDATE]},
            as_of=datetime(2026, 8, 30, 19, 3, tzinfo=timezone.utc),
            api_key="123",
            session=session,
        )
        self.assertEqual(1, result.matches_live)
        match = result.matches[0]
        self.assertEqual({"home": 1, "away": 0}, match["score"])
        self.assertEqual(5, match["statistics"]["corners"]["home"])
        self.assertEqual("Example Scorer", match["events"][0]["player_name"])
        self.assertIn("result_probabilities_percent", match["live_prediction"])
        self.assertLessEqual(result.requests_used, 5)



if __name__ == "__main__":
    unittest.main()
