from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from database import get_connection, initialize_database
from match_statistics import DEFAULT_DATASET_PATH, load_statistics_dataset, write_statistics_dataset
from statistics_source_policy import is_mixed_source, source_key


@dataclass
class AuditReport:
    mixed_dataset_records_removed: int = 0
    mixed_sqlite_statistics_removed: int = 0
    noncanonical_history_removed: int = 0
    invalid_history_removed: int = 0
    history_score_mismatches: int = 0
    remaining_statistics_records: int = 0
    remaining_history_records: int = 0


def _is_invalid_stat_snapshot(row: dict[str, Any]) -> bool:
    pairs = (
        ("home_shots_on_target", "home_total_shots"),
        ("away_shots_on_target", "away_total_shots"),
    )
    for target_field, total_field in pairs:
        target = row.get(target_field)
        total = row.get(total_field)
        if target is not None and total is not None and int(target) > int(total):
            return True
    return False


def audit_and_fix(*, dataset_path: Path = DEFAULT_DATASET_PATH, apply_fixes: bool = True) -> AuditReport:
    initialize_database()
    report = AuditReport()

    dataset = load_statistics_dataset(dataset_path)
    raw_records = [item for item in dataset.get("fixtures", []) if isinstance(item, dict)]
    clean_records = [record for record in raw_records if not is_mixed_source(record.get("source"))]
    report.mixed_dataset_records_removed = len(raw_records) - len(clean_records)
    if apply_fixes and report.mixed_dataset_records_removed:
        write_statistics_dataset(clean_records, dataset_path)

    with get_connection() as connection:
        mixed_stats = connection.execute(
            "SELECT fixture_id FROM fixture_statistics WHERE source LIKE '% + %' OR lower(source) LIKE '%mixed%'"
        ).fetchall()
        report.mixed_sqlite_statistics_removed = len(mixed_stats)
        if apply_fixes and mixed_stats:
            connection.executemany(
                "DELETE FROM fixture_statistics WHERE fixture_id = ?",
                [(int(row["fixture_id"]),) for row in mixed_stats],
            )

        history_rows = [dict(row) for row in connection.execute(
            """
            SELECT h.*, f.home_goals, f.away_goals
            FROM fixture_history_details AS h
            JOIN fixtures AS f ON f.fixture_id = h.fixture_id
            """
        ).fetchall()]
        remove_ids: set[int] = set()
        for row in history_rows:
            if source_key(row.get("source")) != "api_football" or not bool(row.get("score_verified")):
                remove_ids.add(int(row["fixture_id"]))
                report.noncanonical_history_removed += 1
                continue
            if _is_invalid_stat_snapshot(row):
                remove_ids.add(int(row["fixture_id"]))
                report.invalid_history_removed += 1
                continue
            raw_scorers = row.get("goal_scorers_json")
            if raw_scorers:
                try:
                    scorers = json.loads(str(raw_scorers))
                except json.JSONDecodeError:
                    scorers = []
                if isinstance(scorers, list):
                    expected = int(row.get("home_goals") or 0) + int(row.get("away_goals") or 0)
                    if len(scorers) > expected:
                        report.history_score_mismatches += 1
                        remove_ids.add(int(row["fixture_id"]))

        if apply_fixes and remove_ids:
            connection.executemany(
                "DELETE FROM fixture_history_details WHERE fixture_id = ?",
                [(fixture_id,) for fixture_id in sorted(remove_ids)],
            )
        connection.commit()

        report.remaining_statistics_records = int(connection.execute(
            "SELECT COUNT(*) FROM fixture_statistics"
        ).fetchone()[0])
        report.remaining_history_records = int(connection.execute(
            "SELECT COUNT(*) FROM fixture_history_details"
        ).fetchone()[0])

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Ελέγχει ότι κάθε αγώνας χρησιμοποιεί ένα ενιαίο snapshot παρόχου.")
    parser.add_argument("--report", default="data/canonical_statistics_audit.json")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    report = audit_and_fix(apply_fixes=not args.check_only)
    payload = asdict(report)
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.check_only and (
        report.mixed_dataset_records_removed
        or report.mixed_sqlite_statistics_removed
        or report.noncanonical_history_removed
        or report.invalid_history_removed
        or report.history_score_mismatches
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
