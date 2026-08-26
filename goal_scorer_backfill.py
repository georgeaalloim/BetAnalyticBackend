from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from database import get_connection, initialize_database, save_fixture_goal_scorers
from time_utils import parse_iso_datetime, to_iso_z, utc_now


DEFAULT_BACKFILL_PATH = Path(__file__).resolve().parent / "data" / "goal_scorers_backfill_2026.json"
ATHENS_TZ = ZoneInfo("Europe/Athens")
SOURCE_NAME = "BetAnalytic verified backfill 2026"


@dataclass
class BackfillResult:
    entries: int
    matched: int
    saved: int
    pending: int
    warnings: list[str] = field(default_factory=list)


def _local_date(value: str) -> str | None:
    try:
        return parse_iso_datetime(value).astimezone(ATHENS_TZ).date().isoformat()
    except ValueError:
        return None


def _entry_int(entry: dict[str, Any], key: str) -> int:
    value = entry.get(key)
    return int(value) if value is not None and value != "" else -1


def apply_committed_scorer_backfill(path: str | Path = DEFAULT_BACKFILL_PATH) -> BackfillResult:
    initialize_database()
    source_path = Path(path)
    if not source_path.exists():
        return BackfillResult(0, 0, 0, 0, [f"Δεν βρέθηκε backfill file: {source_path}"])
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return BackfillResult(0, 0, 0, 0, [f"Άκυρο scorer backfill: {exc}"])
    if not isinstance(payload, list):
        return BackfillResult(0, 0, 0, 0, ["Το scorer backfill πρέπει να είναι JSON list."])

    with get_connection() as connection:
        fixture_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT fixture_id, fixture_date, home_team_id, away_team_id,
                       home_goals, away_goals
                FROM fixtures
                WHERE status = 'FT'
                  AND home_goals IS NOT NULL
                  AND away_goals IS NOT NULL
                """
            ).fetchall()
        ]

    matched = 0
    saved = 0
    warnings: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        candidates = [
            row for row in fixture_rows
            if int(row["home_team_id"]) == _entry_int(entry, "home_team_id")
            and int(row["away_team_id"]) == _entry_int(entry, "away_team_id")
            and int(row["home_goals"]) == _entry_int(entry, "home_goals")
            and int(row["away_goals"]) == _entry_int(entry, "away_goals")
            and _local_date(str(row.get("fixture_date") or "")) == str(entry.get("local_date") or "")
        ]
        if len(candidates) != 1:
            warnings.append(
                f"Backfill pending: {entry.get('local_date')} "
                f"{entry.get('home_team_id')}-{entry.get('away_team_id')} "
                f"matches={len(candidates)}"
            )
            continue
        matched += 1
        saved += save_fixture_goal_scorers([
            {
                "fixture_id": int(candidates[0]["fixture_id"]),
                "goal_scorers_json": json.dumps(entry.get("scorers") or [], ensure_ascii=False),
                "source": SOURCE_NAME,
                "provider_event_id": None,
                "score_verified": True,
                "collected_at": to_iso_z(utc_now()),
            }
        ])
    return BackfillResult(
        entries=len(payload),
        matched=matched,
        saved=saved,
        pending=max(0, len(payload) - matched),
        warnings=warnings,
    )
