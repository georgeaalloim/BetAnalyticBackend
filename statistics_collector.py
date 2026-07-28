from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from database import get_connection, initialize_database
from football_api import api_get
from match_statistics import (
    DEFAULT_DATASET_PATH,
    build_unavailable_statistics_record,
    has_complete_statistics,
    load_statistics_dataset,
    merge_statistics_records,
    parse_fixture_statistics_response,
    utc_now_iso,
    write_statistics_dataset,
)


DEFAULT_LEAGUE_ID = 197
DEFAULT_SEASONS = (2022, 2023, 2024)
DEFAULT_MAX_REQUESTS = 85
SYNTHETIC_FIXTURE_ID_MIN = 1_200_000_000


def _parse_seasons(raw_value: str) -> tuple[int, ...]:
    seasons: list[int] = []
    for part in raw_value.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        season = int(cleaned)
        if season not in seasons:
            seasons.append(season)

    if not seasons:
        raise ValueError("Πρέπει να δηλωθεί τουλάχιστον μία σεζόν.")

    return tuple(sorted(seasons))


def _eligible_fixture_rows(
    league_id: int,
    seasons: tuple[int, ...],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in seasons)
    query = f"""
        SELECT
            fixture_id,
            league_id,
            season,
            fixture_date,
            status,
            home_team_id,
            home_team_name,
            away_team_id,
            away_team_name
        FROM fixtures
        WHERE league_id = ?
          AND season IN ({placeholders})
          AND status = 'FT'
          AND fixture_id < ?
        ORDER BY season ASC, fixture_date ASC, fixture_id ASC
    """

    parameters = (
        league_id,
        *seasons,
        SYNTHETIC_FIXTURE_ID_MIN,
    )

    with get_connection() as connection:
        rows = connection.execute(query, parameters).fetchall()

    return [dict(row) for row in rows]


def _is_fatal_api_error(message: str) -> bool:
    lowered = message.lower()
    fatal_markers = (
        "suspended",
        "api_football_key",
        "api-football_key",
        "quota",
        "rate limit",
        "too many requests",
        "requests limit",
        "free plans do not have access",
        "authentication",
        "unauthorized",
        "forbidden",
    )
    return any(marker in lowered for marker in fatal_markers)


def _build_summary(
    *,
    status: str,
    league_id: int,
    seasons: tuple[int, ...],
    dataset_path: Path,
    eligible_count: int,
    already_processed_count: int,
    selected_count: int,
    requests_attempted: int,
    requests_completed: int,
    records_added_available: int,
    records_added_unavailable: int,
    total_records_after_merge: int,
    total_available_after_merge: int,
    total_unavailable_after_merge: int,
    plan_only: bool,
    fatal_error: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "status": status,
        "source": "API-Football /fixtures/statistics?fixture=...",
        "collection_mode": "one fixture per request (Free-plan compatible)",
        "league_id": league_id,
        "seasons": list(seasons),
        "dataset_path": str(dataset_path),
        "eligible_completed_fixtures": eligible_count,
        "already_processed_fixtures": already_processed_count,
        "missing_fixtures_considered": selected_count,
        "requests_planned": selected_count,
        "requests_attempted": requests_attempted,
        "requests_completed": requests_completed,
        "new_records_with_corners_and_yellow_cards": records_added_available,
        "new_records_without_complete_statistics": records_added_unavailable,
        "total_records_after_merge": total_records_after_merge,
        "total_records_with_complete_statistics": total_available_after_merge,
        "total_records_without_complete_statistics": total_unavailable_after_merge,
        "plan_only": plan_only,
        "fatal_error": fatal_error,
        "warnings": warnings,
        "finished_at": utc_now_iso(),
    }


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Συλλέγει πραγματικά κόρνερ και κάρτες για ολοκληρωμένους "
            "αγώνες της Super League και τα αποθηκεύει σε σταθερό JSON."
        )
    )
    parser.add_argument(
        "--league-id",
        type=int,
        default=DEFAULT_LEAGUE_ID,
    )
    parser.add_argument(
        "--seasons",
        default=",".join(str(item) for item in DEFAULT_SEASONS),
        help="Σεζόν χωρισμένες με κόμμα, π.χ. 2022,2023,2024.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help=(
            "Ανώτατο όριο API calls για αυτή την εκτέλεση. "
            "Κάθε αγώνας χρειάζεται ένα request."
        ),
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET_PATH),
    )
    parser.add_argument(
        "--summary-output",
        default="data/statistics_collection_summary.json",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Δεν καλεί το API. Εμφανίζει μόνο το σχέδιο αιτημάτων.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    seasons = _parse_seasons(args.seasons)

    if args.max_requests < 1:
        raise ValueError("Το max-requests πρέπει να είναι τουλάχιστον 1.")

    initialize_database()

    dataset_path = Path(args.dataset)
    summary_path = Path(args.summary_output)
    existing_dataset = load_statistics_dataset(dataset_path)
    existing_records = [
        item
        for item in existing_dataset.get("fixtures", [])
        if isinstance(item, dict)
    ]
    processed_ids = {
        int(item["fixture_id"])
        for item in existing_records
        if item.get("fixture_id") is not None
    }

    eligible_rows = _eligible_fixture_rows(
        league_id=args.league_id,
        seasons=seasons,
    )
    missing_rows = [
        row
        for row in eligible_rows
        if int(row["fixture_id"]) not in processed_ids
    ]
    selected_rows = missing_rows[: args.max_requests]

    warnings: list[str] = []
    if len(missing_rows) > len(selected_rows):
        warnings.append(
            "Δεν χωρούν όλοι οι αγώνες στο ημερήσιο όριο. "
            "Τρέξε ξανά το workflow την επόμενη ημέρα για τους υπόλοιπους."
        )

    existing_available = sum(
        1 for record in existing_records if has_complete_statistics(record)
    )
    existing_unavailable = len(existing_records) - existing_available

    if args.plan_only:
        summary = _build_summary(
            status="plan-only",
            league_id=args.league_id,
            seasons=seasons,
            dataset_path=dataset_path,
            eligible_count=len(eligible_rows),
            already_processed_count=len(processed_ids),
            selected_count=len(selected_rows),
            requests_attempted=0,
            requests_completed=0,
            records_added_available=0,
            records_added_unavailable=0,
            total_records_after_merge=len(existing_records),
            total_available_after_merge=existing_available,
            total_unavailable_after_merge=existing_unavailable,
            plan_only=True,
            fatal_error=None,
            warnings=warnings,
        )
        _write_summary(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    collected_at = utc_now_iso()
    new_records: list[dict[str, Any]] = []
    requests_attempted = 0
    requests_completed = 0
    records_available = 0
    records_unavailable = 0
    fatal_error: str | None = None

    for fixture_row in selected_rows:
        fixture_id = int(fixture_row["fixture_id"])
        requests_attempted += 1

        try:
            data = api_get(
                endpoint="/fixtures/statistics",
                params={"fixture": fixture_id},
            )
            requests_completed += 1
        except Exception as error:  # noqa: BLE001 - καταγράφουμε API/network failure
            message = str(error)
            warnings.append(f"Fixture {fixture_id}: {message}")
            if _is_fatal_api_error(message):
                fatal_error = message
                break
            continue

        response_items = data.get("response", [])
        if not isinstance(response_items, list):
            warnings.append(
                f"Fixture {fixture_id}: το response του API δεν ήταν λίστα."
            )
            continue

        parsed = parse_fixture_statistics_response(
            fixture_row,
            response_items,
            collected_at=collected_at,
        )
        if parsed is not None:
            new_records.append(parsed)
            records_available += 1
            continue

        new_records.append(
            build_unavailable_statistics_record(
                fixture_row,
                reason=(
                    "Το API απάντησε, αλλά δεν επέστρεψε πλήρη Corner Kicks "
                    "και Yellow Cards και για τις δύο ομάδες."
                ),
                collected_at=collected_at,
            )
        )
        records_unavailable += 1

    merged_records = merge_statistics_records(
        existing_records,
        new_records,
    )
    write_statistics_dataset(
        merged_records,
        path=dataset_path,
        updated_at=collected_at,
    )

    total_available = sum(
        1 for record in merged_records if has_complete_statistics(record)
    )
    total_unavailable = len(merged_records) - total_available

    if fatal_error is not None:
        status = "partial" if new_records else "error"
    else:
        status = "ok"

    summary = _build_summary(
        status=status,
        league_id=args.league_id,
        seasons=seasons,
        dataset_path=dataset_path,
        eligible_count=len(eligible_rows),
        already_processed_count=len(processed_ids),
        selected_count=len(selected_rows),
        requests_attempted=requests_attempted,
        requests_completed=requests_completed,
        records_added_available=records_available,
        records_added_unavailable=records_unavailable,
        total_records_after_merge=len(merged_records),
        total_available_after_merge=total_available,
        total_unavailable_after_merge=total_unavailable,
        plan_only=False,
        fatal_error=fatal_error,
        warnings=warnings,
    )
    _write_summary(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    return 1 if fatal_error is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
