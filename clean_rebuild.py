from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "betanalytic.db"
DEFAULT_DATASET = ROOT / "data" / "fixture_statistics.json"
DEFAULT_LEDGER = ROOT / "data" / "manual_verified_matches.json"
DEFAULT_REPORT = ROOT / "data" / "clean_rebuild_report.json"
DEFAULT_MANUAL_REPORT = ROOT / "data" / "manual_verification_report.json"
DEFAULT_QUARANTINE = ROOT / "data" / "quarantined_statistics.json"
DEFAULT_QUEUE = ROOT / "data" / "manual_verification_queue.csv"

STAT_FIELDS = (
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_name(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if unicodedata.category(character) != "Mn"
    )
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def _date_only(value: Any) -> str:
    return str(value or "")[:10]


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_dataset(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 3,
            "source": "Canonical single-provider snapshots",
            "updated_at": utc_now_iso(),
            "fixtures_count": 0,
            "available_statistics_count": 0,
            "unavailable_statistics_count": 0,
            "fixtures": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid statistics dataset: {path}")
    return payload


def _write_dataset(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = sorted(
        records,
        key=lambda item: (
            int(item.get("season") or 0),
            str(item.get("fixture_date") or ""),
            int(item.get("fixture_id") or 0),
        ),
    )
    payload = {
        "schema_version": 3,
        "source": "Canonical single-provider snapshots",
        "updated_at": utc_now_iso(),
        "fixtures_count": len(records),
        "available_statistics_count": len(records),
        "unavailable_statistics_count": 0,
        "fixtures": records,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@dataclass
class QualityReport:
    fixture_count: int = 0
    completed_fixture_count: int = 0
    statistics_count_before: int = 0
    statistics_count_after: int = 0
    history_count_before: int = 0
    history_count_after: int = 0
    invalid_statistics_quarantined: int = 0
    invalid_history_quarantined: int = 0
    duplicate_natural_keys: list[dict[str, Any]] = field(default_factory=list)
    foreign_key_errors: list[dict[str, Any]] = field(default_factory=list)
    critical_errors: list[str] = field(default_factory=list)
    quarantine: list[dict[str, Any]] = field(default_factory=list)


def _invalid_stat_reasons(fixture: dict[str, Any], stats: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    source = str(stats.get("source") or "").lower()
    if not source or "mixed" in source or " + " in source:
        reasons.append("missing_or_mixed_source")
    if str(fixture.get("status") or "").upper() != "FT":
        reasons.append("statistics_attached_to_non_completed_match")
    if fixture.get("home_goals") is None or fixture.get("away_goals") is None:
        reasons.append("completed_match_score_missing")
    if int(fixture["season"]) != int(stats["season"]):
        reasons.append("season_mismatch")
    if int(fixture["home_team_id"]) != int(stats["home_team_id"]):
        reasons.append("home_team_id_mismatch")
    if int(fixture["away_team_id"]) != int(stats["away_team_id"]):
        reasons.append("away_team_id_mismatch")
    if _normalize_name(fixture["home_team_name"]) != _normalize_name(stats["home_team_name"]):
        reasons.append("home_team_name_mismatch")
    if _normalize_name(fixture["away_team_name"]) != _normalize_name(stats["away_team_name"]):
        reasons.append("away_team_name_mismatch")
    if _date_only(fixture.get("fixture_date")) != _date_only(stats.get("fixture_date")):
        reasons.append("fixture_date_mismatch")

    for target, total in (
        ("home_shots_on_target", "home_total_shots"),
        ("away_shots_on_target", "away_total_shots"),
    ):
        target_value = _as_int(stats.get(target))
        total_value = _as_int(stats.get(total))
        if target_value is not None and total_value is not None and target_value > total_value:
            reasons.append(f"{target}_greater_than_{total}")

    for field_name in STAT_FIELDS:
        value = _as_int(stats.get(field_name))
        if value is not None and value < 0:
            reasons.append(f"negative_{field_name}")
    return reasons


def audit_clean_database(db_path: Path, dataset_path: Path, *, apply_fixes: bool) -> QualityReport:
    report = QualityReport()
    dataset = _read_dataset(dataset_path)
    dataset_records = {
        int(item["fixture_id"]): dict(item)
        for item in dataset.get("fixtures", [])
        if isinstance(item, dict) and item.get("fixture_id") is not None
    }

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        report.foreign_key_errors = [
            dict(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        if report.foreign_key_errors:
            report.critical_errors.append("foreign_key_check_failed")

        report.fixture_count = int(
            connection.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
        )
        report.completed_fixture_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM fixtures WHERE upper(status) = 'FT'"
            ).fetchone()[0]
        )
        report.statistics_count_before = int(
            connection.execute("SELECT COUNT(*) FROM fixture_statistics").fetchone()[0]
        )
        report.history_count_before = int(
            connection.execute("SELECT COUNT(*) FROM fixture_history_details").fetchone()[0]
        )

        duplicates = connection.execute(
            """
            SELECT season, substr(fixture_date, 1, 10) AS match_date,
                   lower(home_team_name) AS home_name,
                   lower(away_team_name) AS away_name,
                   COUNT(*) AS total,
                   group_concat(fixture_id) AS fixture_ids
            FROM fixtures
            GROUP BY season, match_date, home_name, away_name
            HAVING COUNT(*) > 1
            ORDER BY total DESC, season, match_date
            """
        ).fetchall()
        report.duplicate_natural_keys = [dict(row) for row in duplicates]
        if duplicates:
            report.critical_errors.append("duplicate_fixture_natural_keys")

        missing_score = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM fixtures
                WHERE upper(status) = 'FT'
                  AND (home_goals IS NULL OR away_goals IS NULL)
                """
            ).fetchone()[0]
        )
        if missing_score:
            report.critical_errors.append(
                f"completed_fixtures_without_score:{missing_score}"
            )

        rows = connection.execute(
            """
            SELECT
                f.fixture_id AS f_fixture_id,
                f.league_id AS f_league_id,
                f.season AS f_season,
                f.fixture_date AS f_fixture_date,
                f.status AS f_status,
                f.home_team_id AS f_home_team_id,
                f.home_team_name AS f_home_team_name,
                f.away_team_id AS f_away_team_id,
                f.away_team_name AS f_away_team_name,
                f.home_goals AS f_home_goals,
                f.away_goals AS f_away_goals,
                s.*
            FROM fixture_statistics AS s
            JOIN fixtures AS f ON f.fixture_id = s.fixture_id
            ORDER BY s.season, s.fixture_date, s.fixture_id
            """
        ).fetchall()
        invalid_stat_ids: set[int] = set()
        for row in rows:
            raw = dict(row)
            fixture = {
                "fixture_id": raw["f_fixture_id"],
                "league_id": raw["f_league_id"],
                "season": raw["f_season"],
                "fixture_date": raw["f_fixture_date"],
                "status": raw["f_status"],
                "home_team_id": raw["f_home_team_id"],
                "home_team_name": raw["f_home_team_name"],
                "away_team_id": raw["f_away_team_id"],
                "away_team_name": raw["f_away_team_name"],
                "home_goals": raw["f_home_goals"],
                "away_goals": raw["f_away_goals"],
            }
            stats = {key: value for key, value in raw.items() if not key.startswith("f_")}
            reasons = _invalid_stat_reasons(fixture, stats)
            if reasons:
                fixture_id = int(stats["fixture_id"])
                invalid_stat_ids.add(fixture_id)
                report.quarantine.append(
                    {
                        "kind": "fixture_statistics",
                        "fixture_id": fixture_id,
                        "reason": reasons,
                        "fixture": fixture,
                        "record": stats,
                    }
                )

        history_rows = connection.execute(
            """
            SELECT h.*, f.status, f.home_goals, f.away_goals
            FROM fixture_history_details AS h
            JOIN fixtures AS f ON f.fixture_id = h.fixture_id
            """
        ).fetchall()
        invalid_history_ids: set[int] = set()
        for row in history_rows:
            record = dict(row)
            reasons: list[str] = []
            source = str(record.get("source") or "").lower()
            if not source or "mixed" in source or " + " in source:
                reasons.append("missing_or_mixed_source")
            if str(record.get("status") or "").upper() != "FT":
                reasons.append("history_attached_to_non_completed_match")
            for target, total in (
                ("home_shots_on_target", "home_total_shots"),
                ("away_shots_on_target", "away_total_shots"),
            ):
                target_value = _as_int(record.get(target))
                total_value = _as_int(record.get(total))
                if target_value is not None and total_value is not None and target_value > total_value:
                    reasons.append(f"{target}_greater_than_{total}")
            if reasons:
                fixture_id = int(record["fixture_id"])
                invalid_history_ids.add(fixture_id)
                report.quarantine.append(
                    {
                        "kind": "fixture_history_details",
                        "fixture_id": fixture_id,
                        "reason": reasons,
                        "record": record,
                    }
                )

        report.invalid_statistics_quarantined = len(invalid_stat_ids)
        report.invalid_history_quarantined = len(invalid_history_ids)

        if apply_fixes:
            if invalid_stat_ids:
                connection.executemany(
                    "DELETE FROM fixture_statistics WHERE fixture_id = ?",
                    [(item,) for item in sorted(invalid_stat_ids)],
                )
                for fixture_id in invalid_stat_ids:
                    dataset_records.pop(fixture_id, None)
            if invalid_history_ids:
                connection.executemany(
                    "DELETE FROM fixture_history_details WHERE fixture_id = ?",
                    [(item,) for item in sorted(invalid_history_ids)],
                )
            connection.commit()
            _write_dataset(dataset_path, list(dataset_records.values()))

        report.statistics_count_after = int(
            connection.execute("SELECT COUNT(*) FROM fixture_statistics").fetchone()[0]
        )
        report.history_count_after = int(
            connection.execute("SELECT COUNT(*) FROM fixture_history_details").fetchone()[0]
        )
    finally:
        connection.close()
    return report


@dataclass
class ManualVerificationResult:
    ledger_entries: int = 0
    fixtures_found: int = 0
    fully_matching_entries: int = 0
    score_mismatches: int = 0
    statistics_mismatches: int = 0
    statistics_quarantined: int = 0
    missing_fixtures: int = 0
    missing_statistics: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def _find_fixture(connection: sqlite3.Connection, entry: dict[str, Any]) -> sqlite3.Row | None:
    season = int(entry["season"])
    match_date = str(entry["date"])
    home_id = _as_int(entry.get("home_team_id"))
    away_id = _as_int(entry.get("away_team_id"))
    if home_id is not None and away_id is not None:
        row = connection.execute(
            """
            SELECT * FROM fixtures
            WHERE season = ? AND substr(fixture_date, 1, 10) = ?
              AND home_team_id = ? AND away_team_id = ?
            """,
            (season, match_date, home_id, away_id),
        ).fetchone()
        if row is not None:
            return row

    candidates = connection.execute(
        """
        SELECT * FROM fixtures
        WHERE season = ? AND substr(fixture_date, 1, 10) = ?
        """,
        (season, match_date),
    ).fetchall()
    expected_home = _normalize_name(entry.get("home_team_name"))
    expected_away = _normalize_name(entry.get("away_team_name"))
    for row in candidates:
        if (
            _normalize_name(row["home_team_name"]) == expected_home
            and _normalize_name(row["away_team_name"]) == expected_away
        ):
            return row
    return None


def apply_manual_verification(
    db_path: Path,
    dataset_path: Path,
    ledger_path: Path,
    *,
    apply_fixes: bool,
) -> ManualVerificationResult:
    result = ManualVerificationResult()
    if not ledger_path.exists():
        return result
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    entries = [item for item in ledger.get("matches", []) if isinstance(item, dict)]
    result.ledger_entries = len(entries)
    dataset = _read_dataset(dataset_path)
    dataset_records = {
        int(item["fixture_id"]): dict(item)
        for item in dataset.get("fixtures", [])
        if isinstance(item, dict) and item.get("fixture_id") is not None
    }

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        for entry in entries:
            detail: dict[str, Any] = {
                "verification_id": entry.get("verification_id"),
                "official_reference_url": entry.get("official_reference_url"),
                "checked_at": entry.get("checked_at"),
                "action_on_mismatch": entry.get(
                    "action_on_mismatch", "quarantine_statistics"
                ),
            }
            fixture = _find_fixture(connection, entry)
            if fixture is None:
                result.missing_fixtures += 1
                detail["status"] = "fixture_not_found"
                result.details.append(detail)
                continue
            result.fixtures_found += 1
            fixture_id = int(fixture["fixture_id"])
            detail["fixture_id"] = fixture_id

            expected_score = entry.get("score") or {}
            actual_score = {
                "home": _as_int(fixture["home_goals"]),
                "away": _as_int(fixture["away_goals"]),
            }
            detail["actual_score"] = actual_score
            score_match = (
                actual_score["home"] == _as_int(expected_score.get("home"))
                and actual_score["away"] == _as_int(expected_score.get("away"))
            )
            if not score_match:
                result.score_mismatches += 1
                detail["status"] = "critical_score_mismatch"
                detail["expected_score"] = expected_score
                result.details.append(detail)
                continue

            stats_row = connection.execute(
                "SELECT * FROM fixture_statistics WHERE fixture_id = ?",
                (fixture_id,),
            ).fetchone()
            expected_stats = entry.get("expected_statistics") or {}
            if stats_row is None:
                result.missing_statistics += 1
                detail["status"] = "score_verified_statistics_missing"
                result.details.append(detail)
                continue

            stats = dict(stats_row)
            mismatches: dict[str, dict[str, int | None]] = {}
            for field_name, expected in expected_stats.items():
                if field_name not in STAT_FIELDS:
                    continue
                expected_value = _as_int(expected)
                actual_value = _as_int(stats.get(field_name))
                if actual_value != expected_value:
                    mismatches[field_name] = {
                        "expected": expected_value,
                        "actual": actual_value,
                    }

            if mismatches:
                result.statistics_mismatches += 1
                detail["status"] = "statistics_mismatch_quarantined"
                detail["mismatches"] = mismatches
                detail["provider_source"] = stats.get("source")
                if apply_fixes and entry.get(
                    "action_on_mismatch", "quarantine_statistics"
                ) == "quarantine_statistics":
                    connection.execute(
                        "DELETE FROM fixture_statistics WHERE fixture_id = ?",
                        (fixture_id,),
                    )
                    connection.execute(
                        "DELETE FROM fixture_history_details WHERE fixture_id = ?",
                        (fixture_id,),
                    )
                    dataset_records.pop(fixture_id, None)
                    result.statistics_quarantined += 1
            else:
                result.fully_matching_entries += 1
                detail["status"] = "verified_match"
                detail["provider_source"] = stats.get("source")
            result.details.append(detail)

        if apply_fixes:
            connection.commit()
            _write_dataset(dataset_path, list(dataset_records.values()))
    finally:
        connection.close()
    return result


def export_manual_queue(db_path: Path, output_path: Path, manual_report: ManualVerificationResult) -> None:
    verified_by_fixture = {
        int(item["fixture_id"]): str(item.get("status") or "")
        for item in manual_report.details
        if item.get("fixture_id") is not None
    }
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT f.fixture_id, f.season, substr(f.fixture_date, 1, 10) AS match_date,
                   f.home_team_name, f.away_team_name, f.home_goals, f.away_goals,
                   s.home_corners, s.away_corners,
                   s.home_yellow_cards, s.away_yellow_cards,
                   s.home_red_cards, s.away_red_cards,
                   s.home_total_shots, s.away_total_shots,
                   s.home_shots_on_target, s.away_shots_on_target,
                   s.home_fouls, s.away_fouls,
                   s.home_offsides, s.away_offsides,
                   s.source
            FROM fixtures AS f
            JOIN fixture_statistics AS s ON s.fixture_id = f.fixture_id
            WHERE upper(f.status) = 'FT'
            ORDER BY f.season DESC, f.fixture_date DESC, f.fixture_id DESC
            """
        ).fetchall()
    finally:
        connection.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "fixture_id", "season", "match_date", "home_team_name", "away_team_name",
        "home_goals", "away_goals", *STAT_FIELDS, "source",
        "manual_verification_status", "official_reference_url", "review_notes",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for raw in rows:
            row = dict(raw)
            row["manual_verification_status"] = verified_by_fixture.get(
                int(row["fixture_id"]), "PENDING_MANUAL_REVIEW"
            )
            row["official_reference_url"] = ""
            row["review_notes"] = ""
            writer.writerow(row)


def _run_automatic_update(
    *,
    db_path: Path,
    dataset_path: Path,
    output_dir: Path,
    as_of: str | None,
    skip_source_refresh: bool,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["BETANALYTIC_DATABASE_PATH"] = str(db_path)
    env["BETANALYTIC_STATISTICS_PATH"] = str(dataset_path)
    command = [
        sys.executable,
        str(ROOT / "automatic_update.py"),
        "--output-dir",
        str(output_dir),
        "--skip-upload",
    ]
    if as_of:
        command.extend(["--as-of", as_of])
    if skip_source_refresh:
        command.extend(["--skip-sync", "--skip-detailed-stats"])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    text = completed.stdout.strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if text[index + end :].strip():
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError(
        "automatic_update.py did not return a final JSON object.\n" + text[-4000:]
    )


def _database_counts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT season, status, COUNT(*) FROM fixtures GROUP BY season, status"
        ).fetchall()
        return {
            "exists": True,
            "sha256": _sha256(path),
            "fixtures": int(connection.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]),
            "statistics": int(connection.execute("SELECT COUNT(*) FROM fixture_statistics").fetchone()[0]),
            "history_details": int(connection.execute("SELECT COUNT(*) FROM fixture_history_details").fetchone()[0]),
            "by_season_status": [
                {"season": int(season), "status": status, "count": int(count)}
                for season, status, count in rows
            ],
        }
    finally:
        connection.close()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=destination.name + ".", delete=False
    ) as handle:
        temp_path = Path(handle.name)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuilds BetAnalytic in a new SQLite file from the configured public "
            "sources, quarantines invalid/mismatching statistics and replaces the "
            "production database only after validation."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--manual-ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manual-report", type=Path, default=DEFAULT_MANUAL_REPORT)
    parser.add_argument("--quarantine", type=Path, default=DEFAULT_QUARANTINE)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work_root = ROOT / "clean_rebuild_work" / timestamp
    backup_root = ROOT / "clean_rebuild_backup" / timestamp
    work_root.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)

    clean_db = work_root / "betanalytic.clean.db"
    clean_dataset = work_root / "fixture_statistics.clean.json"
    clean_output = work_root / "automation_output"

    before = _database_counts(DEFAULT_DB)
    for source in (DEFAULT_DB, DEFAULT_DATASET):
        if source.exists():
            shutil.copy2(source, backup_root / source.name)

    first_run = _run_automatic_update(
        db_path=clean_db,
        dataset_path=clean_dataset,
        output_dir=clean_output,
        as_of=args.as_of,
        skip_source_refresh=False,
    )

    quality = audit_clean_database(clean_db, clean_dataset, apply_fixes=True)
    manual = apply_manual_verification(
        clean_db,
        clean_dataset,
        args.manual_ledger,
        apply_fixes=True,
    )

    if manual.score_mismatches:
        quality.critical_errors.append(
            f"manual_official_score_mismatches:{manual.score_mismatches}"
        )

    # Regenerate the feed from the cleaned DB/dataset without fetching the
    # detailed source again; otherwise quarantined provider rows would return.
    second_run = _run_automatic_update(
        db_path=clean_db,
        dataset_path=clean_dataset,
        output_dir=clean_output,
        as_of=args.as_of,
        skip_source_refresh=True,
    )

    after = _database_counts(clean_db)
    if int(after.get("fixtures", 0)) <= 0:
        quality.critical_errors.append("clean_database_has_no_fixtures")
    if int(after.get("statistics", 0)) > int(after.get("fixtures", 0)):
        quality.critical_errors.append("statistics_count_exceeds_fixture_count")

    export_manual_queue(clean_db, args.queue, manual)

    quarantine_payload = {
        "generated_at": utc_now_iso(),
        "policy": (
            "Quarantined records are excluded from the production database, "
            "feed and model. Official-site checks are manual only; no SLGR "
            "scraping is performed."
        ),
        "quality_quarantine": quality.quarantine,
        "manual_verification_details": [
            item
            for item in manual.details
            if item.get("status") == "statistics_mismatch_quarantined"
        ],
    }
    args.quarantine.parent.mkdir(parents=True, exist_ok=True)
    args.quarantine.write_text(
        json.dumps(quarantine_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manual_payload = asdict(manual)
    manual_payload["generated_at"] = utc_now_iso()
    manual_payload["ledger_path"] = str(args.manual_ledger)
    args.manual_report.parent.mkdir(parents=True, exist_ok=True)
    args.manual_report.write_text(
        json.dumps(manual_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report_payload = {
        "status": "blocked" if quality.critical_errors else "ready",
        "generated_at": utc_now_iso(),
        "apply_requested": bool(args.apply),
        "production_replaced": False,
        "before": before,
        "after": after,
        "quality": asdict(quality),
        "manual_verification": manual_payload,
        "source_refresh_summary": {
            "first_run": first_run,
            "clean_feed_regeneration": second_run,
        },
        "backup_directory": str(backup_root),
        "work_directory": str(work_root),
        "clean_database_sha256": _sha256(clean_db),
        "clean_dataset_sha256": _sha256(clean_dataset),
    }

    if args.apply and not quality.critical_errors:
        _atomic_copy(clean_db, DEFAULT_DB)
        _atomic_copy(clean_dataset, DEFAULT_DATASET)
        report_payload["production_replaced"] = True
        report_payload["production_database_sha256"] = _sha256(DEFAULT_DB)
        report_payload["production_dataset_sha256"] = _sha256(DEFAULT_DATASET)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report_payload, ensure_ascii=False, indent=2))

    if quality.critical_errors:
        return 2
    if not args.apply:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
