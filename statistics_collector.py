from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

from database import get_connection, initialize_database
from football_api import api_get
from match_statistics import (
    DEFAULT_DATASET_PATH,
    load_statistics_dataset,
    merge_statistics_records,
    parse_api_fixture_statistics,
    utc_now_iso,
    write_statistics_dataset,
)


DEFAULT_LEAGUE_ID = 197
DEFAULT_SEASONS = (2022, 2023, 2024)
DEFAULT_MAX_REQUESTS = 90
MAX_FIXTURE_IDS_PER_REQUEST = 20
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


def _chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


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


def _build_summary(
    *,
    league_id: int,
    seasons: tuple[int, ...],
    dataset_path: Path,
    eligible_count: int,
    already_saved_count: int,
    requested_fixture_count: int,
    requests_planned: int,
    requests_completed: int,
    records_received: int,
    records_parsed: int,
    records_rejected_missing_statistics: int,
    total_records_after_merge: int,
    plan_only: bool,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "status": "plan-only" if plan_only else "ok",
        "source": "API-Football /fixtures?ids=...",
        "league_id": league_id,
        "seasons": list(seasons),
        "dataset_path": str(dataset_path),
        "eligible_completed_fixtures": eligible_count,
        "already_saved_fixtures": already_saved_count,
        "missing_fixtures_considered": requested_fixture_count,
        "requests_planned": requests_planned,
        "requests_completed": requests_completed,
        "api_fixture_objects_received": records_received,
        "records_with_corners_and_yellow_cards": records_parsed,
        "records_rejected_missing_statistics": (
            records_rejected_missing_statistics
        ),
        "total_records_after_merge": total_records_after_merge,
        "plan_only": plan_only,
        "warnings": warnings,
        "finished_at": utc_now_iso(),
    }


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
        help="Ανώτατο όριο API calls για αυτή την εκτέλεση.",
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
    saved_ids = {
        int(item["fixture_id"])
        for item in existing_records
        if item.get("fixture_id") is not None
    }

    eligible_rows = _eligible_fixture_rows(
        league_id=args.league_id,
        seasons=seasons,
    )
    missing_ids = [
        int(row["fixture_id"])
        for row in eligible_rows
        if int(row["fixture_id"]) not in saved_ids
    ]

    max_fixture_count = args.max_requests * MAX_FIXTURE_IDS_PER_REQUEST
    selected_missing_ids = missing_ids[:max_fixture_count]
    requests_planned = math.ceil(
        len(selected_missing_ids) / MAX_FIXTURE_IDS_PER_REQUEST
    ) if selected_missing_ids else 0

    warnings: list[str] = []
    if len(missing_ids) > len(selected_missing_ids):
        warnings.append(
            "Δεν χωρούν όλοι οι αγώνες στο όριο αιτημάτων. "
            "Τρέξε ξανά το workflow για τους υπόλοιπους."
        )

    if args.plan_only:
        summary = _build_summary(
            league_id=args.league_id,
            seasons=seasons,
            dataset_path=dataset_path,
            eligible_count=len(eligible_rows),
            already_saved_count=len(saved_ids),
            requested_fixture_count=len(selected_missing_ids),
            requests_planned=requests_planned,
            requests_completed=0,
            records_received=0,
            records_parsed=0,
            records_rejected_missing_statistics=0,
            total_records_after_merge=len(existing_records),
            plan_only=True,
            warnings=warnings,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    collected_at = utc_now_iso()
    parsed_records: list[dict[str, Any]] = []
    requests_completed = 0
    received_objects = 0
    rejected = 0

    for fixture_ids in _chunks(
        selected_missing_ids,
        MAX_FIXTURE_IDS_PER_REQUEST,
    ):
        data = api_get(
            endpoint="/fixtures",
            params={"ids": "-".join(str(item) for item in fixture_ids)},
        )
        requests_completed += 1

        response_items = data.get("response", [])
        if not isinstance(response_items, list):
            raise RuntimeError("Το response του API-Football δεν είναι λίστα.")

        received_objects += len(response_items)

        returned_ids: set[int] = set()
        for item in response_items:
            if not isinstance(item, dict):
                rejected += 1
                continue

            fixture_object = item.get("fixture", {})
            if isinstance(fixture_object, dict) and fixture_object.get("id") is not None:
                returned_ids.add(int(fixture_object["id"]))

            parsed = parse_api_fixture_statistics(
                item,
                collected_at=collected_at,
            )
            if parsed is None:
                rejected += 1
                continue

            parsed_records.append(parsed)

        missing_from_response = set(fixture_ids) - returned_ids
        if missing_from_response:
            warnings.append(
                "Το API δεν επέστρεψε τους fixture IDs: "
                + ", ".join(str(item) for item in sorted(missing_from_response))
            )

    merged_records = merge_statistics_records(
        existing_records,
        parsed_records,
    )
    write_statistics_dataset(
        merged_records,
        path=dataset_path,
        updated_at=collected_at,
    )

    summary = _build_summary(
        league_id=args.league_id,
        seasons=seasons,
        dataset_path=dataset_path,
        eligible_count=len(eligible_rows),
        already_saved_count=len(saved_ids),
        requested_fixture_count=len(selected_missing_ids),
        requests_planned=requests_planned,
        requests_completed=requests_completed,
        records_received=received_objects,
        records_parsed=len(parsed_records),
        records_rejected_missing_statistics=rejected,
        total_records_after_merge=len(merged_records),
        plan_only=False,
        warnings=warnings,
    )

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
