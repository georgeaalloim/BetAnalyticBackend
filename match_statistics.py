from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from database import save_fixture_statistics
from statistics_source_policy import choose_whole_record, clean_source


DEFAULT_DATASET_PATH = Path(__file__).resolve().parent / "data" / "fixture_statistics.json"
DATASET_SCHEMA_VERSION = 3
SOURCE_NAME = "Canonical single-provider snapshots"

COUNT_FIELDS = (
    "home_corners",
    "away_corners",
    "home_yellow_cards",
    "away_yellow_cards",
    "home_red_cards",
    "away_red_cards",
    "home_total_shots",
    "away_total_shots",
    "home_shots_on_target",
    "away_shots_on_target",
    "home_fouls",
    "away_fouls",
    "home_offsides",
    "away_offsides",
)

STATISTIC_ALIASES = {
    "corner kicks": "corners",
    "yellow cards": "yellow_cards",
    "red cards": "red_cards",
    "total shots": "total_shots",
    "shots on goal": "shots_on_target",
    "fouls": "fouls",
    "offsides": "offsides",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)

    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "-"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _statistics_by_team(statistics_blocks: Any) -> dict[int, dict[str, int | None]]:
    result: dict[int, dict[str, int | None]] = {}
    if not isinstance(statistics_blocks, list):
        return result

    for block in statistics_blocks:
        if not isinstance(block, dict):
            continue
        team = block.get("team", {})
        team_id = _as_int(team.get("id")) if isinstance(team, dict) else None
        if team_id is None:
            continue

        parsed: dict[str, int | None] = {
            "corners": None,
            "yellow_cards": None,
            "red_cards": None,
            "total_shots": None,
            "shots_on_target": None,
            "fouls": None,
            "offsides": None,
        }
        raw_statistics = block.get("statistics", [])
        if not isinstance(raw_statistics, list):
            raw_statistics = []

        for item in raw_statistics:
            if not isinstance(item, dict):
                continue
            normalized_name = STATISTIC_ALIASES.get(
                str(item.get("type", "")).strip().lower()
            )
            if normalized_name is not None:
                parsed[normalized_name] = _as_int(item.get("value"))

        result[int(team_id)] = parsed

    return result


def _build_record(
    *,
    fixture_id: int,
    league_id: int,
    season: int,
    fixture_date: Any,
    status: Any,
    home_team_id: int,
    home_team_name: str,
    away_team_id: int,
    away_team_name: str,
    statistics_blocks: Any,
    collected_at: str,
) -> dict[str, Any] | None:
    by_team = _statistics_by_team(statistics_blocks)
    home_stats = by_team.get(int(home_team_id), {})
    away_stats = by_team.get(int(away_team_id), {})

    record: dict[str, Any] = {
        "fixture_id": int(fixture_id),
        "league_id": int(league_id),
        "season": int(season),
        "fixture_date": fixture_date,
        "status": str(status or "") or None,
        "home_team_id": int(home_team_id),
        "home_team_name": str(home_team_name or ""),
        "away_team_id": int(away_team_id),
        "away_team_name": str(away_team_name or ""),
        "home_corners": _as_int(home_stats.get("corners")),
        "away_corners": _as_int(away_stats.get("corners")),
        "home_yellow_cards": _as_int(home_stats.get("yellow_cards")),
        "away_yellow_cards": _as_int(away_stats.get("yellow_cards")),
        "home_red_cards": _as_int(home_stats.get("red_cards")),
        "away_red_cards": _as_int(away_stats.get("red_cards")),
        "home_total_shots": _as_int(home_stats.get("total_shots")),
        "away_total_shots": _as_int(away_stats.get("total_shots")),
        "home_shots_on_target": _as_int(home_stats.get("shots_on_target")),
        "away_shots_on_target": _as_int(away_stats.get("shots_on_target")),
        "home_fouls": _as_int(home_stats.get("fouls")),
        "away_fouls": _as_int(away_stats.get("fouls")),
        "home_offsides": _as_int(home_stats.get("offsides")),
        "away_offsides": _as_int(away_stats.get("offsides")),
        "referee": None,
        "statistics_available": False,
        "unavailable_reason": None,
        "source": "API-Football",
        "collected_at": collected_at,
    }
    record["statistics_available"] = has_complete_statistics(record)
    if not record["statistics_available"]:
        return None
    return record


def parse_api_fixture_statistics(
    fixture_payload: dict[str, Any],
    collected_at: str | None = None,
) -> dict[str, Any] | None:
    fixture = fixture_payload.get("fixture", {})
    league = fixture_payload.get("league", {})
    teams = fixture_payload.get("teams", {})
    if not all(isinstance(item, dict) for item in (fixture, league, teams)):
        return None

    home_team = teams.get("home", {})
    away_team = teams.get("away", {})
    if not isinstance(home_team, dict) or not isinstance(away_team, dict):
        return None

    values = (
        _as_int(fixture.get("id")),
        _as_int(league.get("id")),
        _as_int(league.get("season")),
        _as_int(home_team.get("id")),
        _as_int(away_team.get("id")),
    )
    if any(value is None for value in values):
        return None
    fixture_id, league_id, season, home_team_id, away_team_id = values

    status = fixture.get("status", {})
    status_short = str(status.get("short", "")).strip() if isinstance(status, dict) else ""
    return _build_record(
        fixture_id=int(fixture_id),
        league_id=int(league_id),
        season=int(season),
        fixture_date=fixture.get("date"),
        status=status_short,
        home_team_id=int(home_team_id),
        home_team_name=str(home_team.get("name") or ""),
        away_team_id=int(away_team_id),
        away_team_name=str(away_team.get("name") or ""),
        statistics_blocks=fixture_payload.get("statistics", []),
        collected_at=collected_at or utc_now_iso(),
    )


def parse_fixture_statistics_response(
    fixture_row: dict[str, Any],
    response_items: Any,
    collected_at: str | None = None,
) -> dict[str, Any] | None:
    values = (
        _as_int(fixture_row.get("fixture_id")),
        _as_int(fixture_row.get("league_id")),
        _as_int(fixture_row.get("season")),
        _as_int(fixture_row.get("home_team_id")),
        _as_int(fixture_row.get("away_team_id")),
    )
    if any(value is None for value in values):
        return None
    fixture_id, league_id, season, home_team_id, away_team_id = values

    return _build_record(
        fixture_id=int(fixture_id),
        league_id=int(league_id),
        season=int(season),
        fixture_date=fixture_row.get("fixture_date"),
        status=fixture_row.get("status"),
        home_team_id=int(home_team_id),
        home_team_name=str(fixture_row.get("home_team_name") or ""),
        away_team_id=int(away_team_id),
        away_team_name=str(fixture_row.get("away_team_name") or ""),
        statistics_blocks=response_items,
        collected_at=collected_at or utc_now_iso(),
    )


def build_unavailable_statistics_record(
    fixture_row: dict[str, Any],
    *,
    reason: str,
    collected_at: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "fixture_id": int(fixture_row["fixture_id"]),
        "league_id": int(fixture_row["league_id"]),
        "season": int(fixture_row["season"]),
        "fixture_date": fixture_row.get("fixture_date"),
        "status": fixture_row.get("status"),
        "home_team_id": int(fixture_row["home_team_id"]),
        "home_team_name": str(fixture_row.get("home_team_name") or ""),
        "away_team_id": int(fixture_row["away_team_id"]),
        "away_team_name": str(fixture_row.get("away_team_name") or ""),
        "statistics_available": False,
        "unavailable_reason": str(reason),
        "source": "API-Football",
        "collected_at": collected_at or utc_now_iso(),
        "referee": None,
    }
    for field in COUNT_FIELDS:
        record[field] = None
    return record


def has_complete_statistics(record: dict[str, Any]) -> bool:
    return all(
        record.get(field) is not None
        for field in (
            "home_corners",
            "away_corners",
            "home_yellow_cards",
            "away_yellow_cards",
        )
    )


def empty_dataset() -> dict[str, Any]:
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "updated_at": None,
        "fixtures_count": 0,
        "available_statistics_count": 0,
        "unavailable_statistics_count": 0,
        "fixtures": [],
    }


def load_statistics_dataset(path: str | Path = DEFAULT_DATASET_PATH) -> dict[str, Any]:
    dataset_path = Path(path)
    if not dataset_path.exists():
        return empty_dataset()
    try:
        data = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Το αρχείο στατιστικών δεν μπορεί να διαβαστεί: {dataset_path}"
        ) from error
    if not isinstance(data, dict):
        raise RuntimeError("Το αρχείο στατιστικών πρέπει να είναι JSON object.")
    if not isinstance(data.get("fixtures", []), list):
        raise RuntimeError("Το πεδίο fixtures του αρχείου στατιστικών δεν είναι λίστα.")
    data.setdefault("schema_version", DATASET_SCHEMA_VERSION)
    data.setdefault("source", SOURCE_NAME)
    data.setdefault("updated_at", None)
    return data


def _merge_record(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    selected = choose_whole_record(existing, new)
    selected["source"] = clean_source(selected.get("source")) or SOURCE_NAME
    complete = has_complete_statistics(selected)
    selected["statistics_available"] = complete
    selected["unavailable_reason"] = None if complete else (
        selected.get("unavailable_reason")
        or "Η επιλεγμένη ενιαία πηγή δεν παρέχει πλήρη κόρνερ και κάρτες."
    )
    return selected

def merge_statistics_records(
    existing_records: Iterable[dict[str, Any]],
    new_records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_fixture_id: dict[int, dict[str, Any]] = {}
    for record in (*list(existing_records), *list(new_records)):
        if not isinstance(record, dict):
            continue
        fixture_id = _as_int(record.get("fixture_id"))
        if fixture_id is None:
            continue
        if int(fixture_id) in by_fixture_id:
            by_fixture_id[int(fixture_id)] = _merge_record(
                by_fixture_id[int(fixture_id)], dict(record)
            )
        else:
            by_fixture_id[int(fixture_id)] = dict(record)

    return sorted(
        by_fixture_id.values(),
        key=lambda item: (
            int(item.get("season") or 0),
            str(item.get("fixture_date") or ""),
            int(item.get("fixture_id") or 0),
        ),
    )


def write_statistics_dataset(
    records: Iterable[dict[str, Any]],
    path: str | Path = DEFAULT_DATASET_PATH,
    updated_at: str | None = None,
) -> Path:
    dataset_path = Path(path)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_records = merge_statistics_records([], records)
    available_count = sum(1 for record in normalized_records if has_complete_statistics(record))
    payload = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "updated_at": updated_at or utc_now_iso(),
        "fixtures_count": len(normalized_records),
        "available_statistics_count": available_count,
        "unavailable_statistics_count": len(normalized_records) - available_count,
        "fixtures": normalized_records,
    }
    dataset_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dataset_path


def import_statistics_dataset(path: str | Path = DEFAULT_DATASET_PATH) -> dict[str, int]:
    dataset = load_statistics_dataset(path)
    fixtures = dataset.get("fixtures", [])
    valid_records = [
        record
        for record in fixtures
        if isinstance(record, dict) and has_complete_statistics(record)
    ]
    saved = save_fixture_statistics(valid_records)
    return {
        "records_in_file": len(fixtures),
        "records_with_complete_statistics": len(valid_records),
        "records_saved": saved,
    }
