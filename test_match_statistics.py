from match_statistics import (
    build_unavailable_statistics_record,
    has_complete_statistics,
    merge_statistics_records,
    parse_api_fixture_statistics,
    parse_fixture_statistics_response,
)


def _statistics_blocks() -> list[dict]:
    return [
        {
            "team": {"id": 10},
            "statistics": [
                {"type": "Corner Kicks", "value": 7},
                {"type": "Yellow Cards", "value": 3},
                {"type": "Red Cards", "value": 0},
            ],
        },
        {
            "team": {"id": 20},
            "statistics": [
                {"type": "Corner Kicks", "value": "4"},
                {"type": "Yellow Cards", "value": 2},
                {"type": "Red Cards", "value": None},
            ],
        },
    ]


def _sample_fixture() -> dict:
    return {
        "fixture": {
            "id": 12345,
            "date": "2024-03-01T17:00:00+00:00",
            "status": {"short": "FT"},
        },
        "league": {"id": 197, "season": 2024},
        "teams": {
            "home": {"id": 10, "name": "Home FC"},
            "away": {"id": 20, "name": "Away FC"},
        },
        "statistics": _statistics_blocks(),
    }


def _sample_row() -> dict:
    return {
        "fixture_id": 12345,
        "league_id": 197,
        "season": 2024,
        "fixture_date": "2024-03-01T17:00:00+00:00",
        "status": "FT",
        "home_team_id": 10,
        "home_team_name": "Home FC",
        "away_team_id": 20,
        "away_team_name": "Away FC",
    }


def test_parse_api_fixture_statistics() -> None:
    parsed = parse_api_fixture_statistics(
        _sample_fixture(),
        collected_at="2026-07-28T00:00:00Z",
    )

    assert parsed is not None
    assert parsed["home_corners"] == 7
    assert parsed["away_corners"] == 4
    assert parsed["home_yellow_cards"] == 3
    assert parsed["away_yellow_cards"] == 2
    assert parsed["home_red_cards"] == 0
    assert parsed["away_red_cards"] is None
    assert parsed["statistics_available"] is True


def test_parse_free_plan_statistics_response() -> None:
    parsed = parse_fixture_statistics_response(
        _sample_row(),
        _statistics_blocks(),
        collected_at="2026-07-28T00:00:00Z",
    )

    assert parsed is not None
    assert parsed["fixture_id"] == 12345
    assert parsed["home_corners"] == 7
    assert parsed["away_yellow_cards"] == 2
    assert has_complete_statistics(parsed)


def test_missing_required_stat_is_rejected() -> None:
    payload = _sample_fixture()
    payload["statistics"][1]["statistics"] = [
        {"type": "Yellow Cards", "value": 2}
    ]

    assert parse_api_fixture_statistics(payload) is None


def test_unavailable_record_is_not_complete() -> None:
    unavailable = build_unavailable_statistics_record(
        _sample_row(),
        reason="missing",
        collected_at="2026-07-28T00:00:00Z",
    )

    assert unavailable["statistics_available"] is False
    assert unavailable["unavailable_reason"] == "missing"
    assert not has_complete_statistics(unavailable)


def test_merge_replaces_same_fixture() -> None:
    first = {"fixture_id": 1, "season": 2023, "home_corners": 4}
    newer = {"fixture_id": 1, "season": 2023, "home_corners": 6}

    merged = merge_statistics_records([first], [newer])
    assert len(merged) == 1
    assert merged[0]["home_corners"] == 6
