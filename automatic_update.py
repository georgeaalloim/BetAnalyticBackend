from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from automation_config import AutomationConfig
from database import initialize_database
from fixtur_es_source import (
    fetch_super_league_fixtures,
    replace_source_fixtures,
)
from static_feed_generator import generate_static_feed
from time_utils import parse_iso_datetime, to_iso_z, utc_now


load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ενημερώνει αγώνες από το Fixtur.es, υπολογίζει προβλέψεις "
            "χωρίς temporal leakage και παράγει στατικό JSON feed."
        )
    )
    parser.add_argument(
        "--output-dir",
        default="automation_output",
        help="Φάκελος παραγωγής των feed.json και manifest.json.",
    )
    parser.add_argument(
        "--lookahead-days",
        type=int,
        default=None,
        help="Πόσες ημέρες επερχόμενων αγώνων θα μπουν στο feed.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO UTC ώρα υπολογισμού για ελεγχόμενη δοκιμή.",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Χρησιμοποιεί μόνο την υπάρχουσα τοπική βάση.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Συμβατότητα με το workflow του GitHub Pages.",
    )
    return parser.parse_args()


def _database_seasons() -> tuple[int, ...]:
    from database import get_connection

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT season
            FROM fixtures
            WHERE league_id = 197
            ORDER BY season ASC
            """
        ).fetchall()

    return tuple(int(row["season"]) for row in rows)


def main() -> int:
    args = _parse_args()
    config = AutomationConfig.from_environment(
        output_dir=args.output_dir,
        sync_seasons_override="auto",
        lookahead_days_override=args.lookahead_days,
    )
    as_of: datetime = (
        parse_iso_datetime(args.as_of)
        if args.as_of
        else utc_now()
    )

    initialize_database()

    if args.skip_sync:
        sync_summary: dict[str, Any] = {
            "source": "local-database",
            "skipped": True,
        }
    else:
        source_result = fetch_super_league_fixtures(as_of=as_of)
        processed = replace_source_fixtures(source_result.fixtures)
        source_seasons = sorted(
            {
                int(item["league"]["season"])
                for item in source_result.fixtures
            }
        )
        sync_summary = {
            "source": "Fixtur.es calendar feed",
            "status": "ok",
            "received": len(source_result.fixtures),
            "processed": processed,
            "source_seasons": source_seasons,
            "pages_checked": source_result.pages_checked,
            "calendar_feeds_used": source_result.calendar_urls,
            "warnings": source_result.warnings,
        }

    seasons = _database_seasons()
    if not seasons:
        raise RuntimeError("Η βάση δεν περιέχει καμία σεζόν.")

    sync_summary["finished_at"] = to_iso_z(utc_now())

    generated = generate_static_feed(
        output_dir=config.output_dir,
        league_id=config.league_id,
        league_name=config.league_name,
        seasons=seasons,
        as_of=as_of,
        lookahead_days=config.lookahead_days,
        upcoming_statuses=config.upcoming_statuses,
        feed_public_url="feed.json",
        sync_summary=sync_summary,
    )

    summary = {
        "status": "ok",
        "generated_at": to_iso_z(as_of),
        "seasons": list(seasons),
        "fixtures_in_feed": generated.fixture_count,
        "ready_predictions": generated.ready_prediction_count,
        "unavailable_predictions": generated.unavailable_prediction_count,
        "feed_sha256": generated.sha256,
        "feed_path": str(generated.feed_path),
        "manifest_path": str(generated.manifest_path),
        "sync_summary": sync_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
