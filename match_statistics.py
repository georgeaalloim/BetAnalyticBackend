from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from database import save_fixture_statistics


DEFAULT_DATASET_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "fixture_statistics.json"
)

DATASET_SCHEMA_VERSION = 1
SOURCE_NAME = "API-Football"


STATISTIC_ALIASES = {
    "corner kicks": "corners",
    "yellow cards": "yellow_cards",
    "red cards": "red_cards",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
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


def _statistics_by_team(
    fixture_payload: dict[str, Any],
) -> dict[int, dict[str, int | None]]:
    result: dict[int, dict[str, int | None]] = {}

    statistics_blocks = fixture_payload.get("statistics", [])
    if not isinstance(statistics_blocks, list):
        return result

    for block in statistics_blocks:
        if not isinstance(block, dict):
            continue

        team = block.get("team", {})
        team_id = _as_int(team.get("id")) if isinstance(team, dict) else None
        if team_id is None:
            continue

        parsed = {
            "corners": None,
            "yellow_cards": None,
            "red_cards": None,
        }

        raw_statistics = block.get("statistics", [])
        if not isinstance(raw_statistics, list):
            raw_statistics = []

        for item in raw_statistics:
            if not isinstance(item, dict):
                continue

            raw_type = str(item.get("type", "")).strip().lower()
            normalized_name = STATISTIC_ALIASES.get(raw_type)
            if normalized_name is None:
                continue

            parsed[normalized_name] = _as_int(item.get("value"))

        result[team_id] = parsed

    return result


def parse_api_fixture_statistics(
    fixture_payload: dict[str, Any],
    collected_at: str | None = None,
) -> dict[str, Any] | None:
    """
    Μετατρέπει ένα πλήρες αντικείμενο `/fixtures?ids=...` σε εγγραφή
    κόρνερ και καρτών.

    Για να θεωρηθεί έγκυρη η εγγραφή απαιτούνται πραγματικές τιμές για
    κόρνερ και κίτρινες κάρτες και για τις δύο ομάδες. Οι κόκκινες κάρτες
    αποθηκεύονται όταν υπάρχουν, αλλά δεν χρησιμοποιούνται ως υποκατάστατο
    των κίτρινων καρτών.
    """

    fixture = fixture_payload.get("fixture", {})
    league = fixture_payload.get("league", {})
    teams = fixture_payload.get("teams", {})

    if not all(isinstance(item, dict) for item in (fixture, league, teams)):
        return None

    home_team = teams.get("home", {})
    away_team = teams.get("away", {})
    if not isinstance(home_team, dict) or not isinstance(away_team, dict):
        return None

    fixture_id = _as_int(fixture.get("id"))
    league_id = _as_int(league.get("id"))
    season = _as_int(league.get("season"))
    home_team_id = _as_int(home_team.get("id"))
    away_team_id = _as_int(away_team.get("id"))

    required_ids = (
        fixture_id,
        league_id,
        season,
        home_team_id,
        away_team_id,
    )
    if any(value is None for value in required_ids):
        return None

    by_team = _statistics_by_team(fixture_payload)
    home_stats = by_team.get(int(home_team_id), {})
    away_stats = by_team.get(int(away_team_id), {})

    home_corners = _as_int(home_stats.get("corners"))
    away_corners = _as_int(away_stats.get("corners"))
    home_yellow = _as_int(home_stats.get("yellow_cards"))
    away_yellow = _as_int(away_stats.get("yellow_cards"))

    if any(
        value is None
        for value in (
            home_corners,
            away_corners,
            home_yellow,
            away_yellow,
        )
    ):
        return None

    status = fixture.get("status", {})
    status_short = (
        str(status.get("short", "")).strip()
        if isinstance(status, dict)
        else ""
    )

    return {
        "fixture_id": int(fixture_id),
        "league_id": int(league_id),
        "season": int(season),
        "fixture_date": fixture.get("date"),
        "status": status_short or None,
        "home_team_id": int(home_team_id),
        "home_team_name": str(home_team.get("name") or ""),
        "away_team_id": int(away_team_id),
        "away_team_name": str(away_team.get("name") or ""),
        "home_corners": int(home_corners),
        "away_corners": int(away_corners),
        "home_yellow_cards": int(home_yellow),
        "away_yellow_cards": int(away_yellow),
        "home_red_cards": _as_int(home_stats.get("red_cards")),
        "away_red_cards": _as_int(away_stats.get("red_cards")),
        "source": SOURCE_NAME,
        "collected_at": collected_at or utc_now_iso(),
    }


def empty_dataset() -> dict[str, Any]:
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "updated_at": None,
        "fixtures": [],
    }


def load_statistics_dataset(
    path: str | Path = DEFAULT_DATASET_PATH,
) -> dict[str, Any]:
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

    fixtures = data.get("fixtures", [])
    if not isinstance(fixtures, list):
        raise RuntimeError("Το πεδίο fixtures του αρχείου στατιστικών δεν είναι λίστα.")

    data.setdefault("schema_version", DATASET_SCHEMA_VERSION)
    data.setdefault("source", SOURCE_NAME)
    data.setdefault("updated_at", None)
    return data


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
    payload = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "updated_at": updated_at or utc_now_iso(),
        "fixtures_count": len(normalized_records),
        "fixtures": normalized_records,
    }

    dataset_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dataset_path


def import_statistics_dataset(
    path: str | Path = DEFAULT_DATASET_PATH,
) -> dict[str, int]:
    dataset = load_statistics_dataset(path)
    fixtures = dataset.get("fixtures", [])

    valid_records = [
        record
        for record in fixtures
        if isinstance(record, dict)
    ]

    saved = save_fixture_statistics(valid_records)
    return {
        "records_in_file": len(valid_records),
        "records_saved": saved,
    }
