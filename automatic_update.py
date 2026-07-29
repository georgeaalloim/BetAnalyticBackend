from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from api_football_free_source import fetch_api_football_fixtures
from api_football_history_enricher import enrich_history
from automation_config import AutomationConfig
from database import initialize_database, save_fixture_statistics
from fixtur_es_source import (
    fetch_super_league_fixtures,
    replace_source_fixtures,
    season_from_local_date,
)
from football_data_source import (
    FootballDataResult,
    fetch_football_data,
    reconcile_and_save_football_data,
)
from free_schedule_source import merge_free_schedule_sources
from match_statistics import (
    DEFAULT_DATASET_PATH,
    has_complete_statistics,
    load_statistics_dataset,
    merge_statistics_records,
    write_statistics_dataset,
)
from static_feed_generator import generate_static_feed
from openfootball_source import fetch_openfootball_fixtures
from time_utils import parse_iso_datetime, to_iso_z, utc_now


load_dotenv()
ATHENS_TZ = ZoneInfo("Europe/Athens")
DETAILED_STATS_SEASON_WINDOW = 4


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ενημερώνει δωρεάν το πρόγραμμα με διασταύρωση Fixtur.es, "
            "OpenFootball, Football-Data.co.uk και προαιρετικά το δωρεάν "
            "API-Football, ενημερώνει αποτελέσματα/"
            "αναλυτικά ιστορικά στατιστικά από Football-Data και παράγει το feed."
        )
    )
    parser.add_argument("--output-dir", default="automation_output")
    parser.add_argument("--lookahead-days", type=int, default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-detailed-stats", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
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


def _automatic_detailed_seasons(
    database_seasons: tuple[int, ...],
    *,
    as_of: datetime,
) -> tuple[int, ...]:
    current = season_from_local_date(as_of.astimezone(ATHENS_TZ).date())
    latest = max((*database_seasons, current)) if database_seasons else current
    first = latest - DETAILED_STATS_SEASON_WINDOW + 1
    return tuple(range(first, latest + 1))


def _schedule_verification_seasons(as_of: datetime) -> tuple[int, ...]:
    current = season_from_local_date(as_of.astimezone(ATHENS_TZ).date())
    # Only the active and next season are schedule-synced. Older completed
    # seasons come from the historical database/Football-Data, preventing
    # duplicate historical matches from different fixture IDs.
    return (current, current + 1)


def _merge_and_save_statistics(
    new_records: list[dict[str, Any]],
) -> dict[str, int]:
    dataset = load_statistics_dataset(DEFAULT_DATASET_PATH)
    existing = [
        item for item in dataset.get("fixtures", []) if isinstance(item, dict)
    ]
    merged = merge_statistics_records(existing, new_records)
    write_statistics_dataset(merged, DEFAULT_DATASET_PATH)
    complete = [item for item in merged if has_complete_statistics(item)]
    saved = save_fixture_statistics(complete)
    return {
        "records_before_merge": len(existing),
        "records_received": len(new_records),
        "records_after_merge": len(merged),
        "records_with_complete_statistics": len(complete),
        "records_saved_to_sqlite": saved,
    }


def _completed_only(result: FootballDataResult) -> FootballDataResult:
    fixtures = [
        payload
        for payload in result.fixtures
        if str((payload.get("fixture", {}).get("status") or {}).get("short") or "")
        .upper()
        == "FT"
    ]
    return replace(result, fixtures=fixtures)


def main() -> int:
    args = _parse_args()
    config = AutomationConfig.from_environment(
        output_dir=args.output_dir,
        sync_seasons_override="auto",
        lookahead_days_override=args.lookahead_days,
    )
    as_of: datetime = parse_iso_datetime(args.as_of) if args.as_of else utc_now()

    initialize_database()
    database_seasons_before = _database_seasons()
    detailed_seasons = _automatic_detailed_seasons(
        database_seasons_before,
        as_of=as_of,
    )

    # Football-Data is used both as a free cross-check for the schedule and as
    # the automatic source for completed results/corners/cards.
    football_data: FootballDataResult | None = None
    if not (args.skip_sync and args.skip_detailed_stats):
        football_data = fetch_football_data(seasons=detailed_seasons, as_of=as_of)

    sync_summary: dict[str, Any] = {}

    if args.skip_sync:
        sync_summary["fixtures"] = {
            "source": "local-database",
            "source_key": "local_database",
            "skipped": True,
        }
    else:
        fixtur_es = fetch_super_league_fixtures(as_of=as_of)
        verification_seasons = _schedule_verification_seasons(as_of)
        openfootball = fetch_openfootball_fixtures(
            seasons=verification_seasons,
            as_of=as_of,
        )
        api_football = fetch_api_football_fixtures(
            seasons=verification_seasons,
            api_key=os.getenv("API_FOOTBALL_KEY"),
        )
        allowed_schedule_seasons = set(verification_seasons)
        fixtur_schedule = [
            item
            for item in fixtur_es.fixtures
            if int(item["league"]["season"]) in allowed_schedule_seasons
        ]
        football_fixtures = [
            item
            for item in (football_data.fixtures if football_data else [])
            if int(item["league"]["season"]) in allowed_schedule_seasons
        ]
        merged_schedule = merge_free_schedule_sources(
            fixtur_es_fixtures=fixtur_schedule,
            openfootball_fixtures=openfootball.fixtures,
            football_data_fixtures=football_fixtures,
            api_football_fixtures=api_football.fixtures,
            as_of=as_of,
        )
        if not merged_schedule.fixtures:
            raise RuntimeError(
                "Καμία δωρεάν πηγή δεν επέστρεψε πρόγραμμα για την ενεργή "
                "ή την επόμενη σεζόν. Το workflow σταμάτησε για να μη "
                "δημοσιευτεί παλιό ή κενό πρόγραμμα."
            )
        processed = replace_source_fixtures(merged_schedule.fixtures)
        source_seasons = sorted(
            {
                int(item["league"]["season"])
                for item in merged_schedule.fixtures
            }
        )
        sync_summary["fixtures"] = {
            "source": (
                "Free cross-checked schedule: Fixtur.es + OpenFootball CC0 + "
                "Football-Data.co.uk + optional API-Football Free"
            ),
            "source_key": "free_cross_checked_schedule",
            "status": "ok",
            "received": len(merged_schedule.fixtures),
            "processed": processed,
            "source_seasons": source_seasons,
            "verification_counts": merged_schedule.verification_counts,
            "source_counts": merged_schedule.source_counts,
            "fixtur_es_pages_checked": fixtur_es.pages_checked,
            "fixtur_es_calendars_used": fixtur_es.calendar_urls,
            "openfootball_seasons_requested": openfootball.seasons_requested,
            "openfootball_seasons_loaded": openfootball.seasons_loaded,
            "openfootball_urls_loaded": openfootball.urls_loaded,
            "api_football_free_enabled": api_football.enabled,
            "api_football_free_seasons_requested": api_football.seasons_requested,
            "api_football_free_seasons_loaded": api_football.seasons_loaded,
            "api_football_free_requests_used": api_football.requests_used,
            "api_football_free_quota_remaining": api_football.quota_remaining,
            "warnings": [
                *fixtur_es.warnings,
                *openfootball.warnings,
                *api_football.warnings,
                *merged_schedule.warnings,
            ],
            "safety_rule": (
                "Η ώρα εμφανίζεται όταν δύο πηγές συμφωνούν στην ώρα ή όταν "
                "δύο ανεξάρτητες πηγές επιβεβαιώνουν την ημερομηνία και μία "
                "από αυτές δίνει ρητή ώρα. Αν υπάρχουν αντικρουόμενες ώρες, "
                "η εφαρμογή εμφανίζει «Ώρα δεν έχει οριστεί»."
            ),
        }

    database_seasons = _database_seasons()
    if not database_seasons:
        raise RuntimeError("Η βάση δεν περιέχει καμία σεζόν.")

    if args.skip_detailed_stats:
        dataset = load_statistics_dataset(DEFAULT_DATASET_PATH)
        existing_records = [
            item for item in dataset.get("fixtures", []) if isinstance(item, dict)
        ]
        statistics_summary = _merge_and_save_statistics([])
        sync_summary["detailed_statistics"] = {
            "source": "committed dataset only",
            "skipped": True,
            "records_in_dataset": len(existing_records),
            **statistics_summary,
        }
    else:
        if football_data is None:
            football_data = fetch_football_data(
                seasons=detailed_seasons,
                as_of=as_of,
            )
        # Only completed Football-Data matches are persisted here. Future
        # kickoff times are controlled by the multi-source verification above.
        reconciled = reconcile_and_save_football_data(_completed_only(football_data))
        statistics_summary = _merge_and_save_statistics(reconciled.statistics)
        sync_summary["detailed_statistics"] = {
            "source": "Football-Data.co.uk CSV (free; no API key)",
            "status": "ok" if football_data.seasons_loaded else "fallback",
            "seasons_requested": football_data.seasons_requested,
            "seasons_loaded": football_data.seasons_loaded,
            "urls_loaded": football_data.urls_loaded,
            "csv_rows_loaded": football_data.rows_loaded,
            "csv_rows_with_complete_corners_and_cards": (
                football_data.complete_statistics_rows
            ),
            "matched_existing_fixtures": reconciled.matched_existing_fixtures,
            "inserted_new_fixtures": reconciled.inserted_new_fixtures,
            "fixtures_saved": reconciled.fixtures_saved,
            "warnings": football_data.warnings,
            **statistics_summary,
        }

    history_enrichment = enrich_history(
        seasons=(as_of.year - 1, as_of.year),
        api_key=os.getenv("API_FOOTBALL_KEY"),
        recent_days=4,
        max_detail_batches=1,
    )
    sync_summary["history_enrichment"] = {
        "source": "API-Football Free fixture details",
        "enabled": history_enrichment.enabled,
        "seasons": history_enrichment.seasons,
        "completed_matches_considered": history_enrichment.completed_matches_considered,
        "api_matches_found": history_enrichment.api_matches_found,
        "matches_enriched": history_enrichment.matches_enriched,
        "requests_used": history_enrichment.requests_used,
        "quota_remaining": history_enrichment.quota_remaining,
        "warnings": history_enrichment.warnings,
        "mode": "recent completed matches only; one details batch maximum",
    }

    seasons = _database_seasons()
    sync_summary["automatic_mode"] = {
        "enabled": True,
        "paid_api_required": False,
        "api_secret_required": False,
        "optional_free_api_supported": True,
        "optional_free_api_enabled": bool(os.getenv("API_FOOTBALL_KEY", "").strip()),
        "superleague_scraping": False,
        "manual_schedule_overrides": False,
        "future_detailed_season_detection": True,
        "description": (
            "Κάθε run διασταυρώνει δωρεάν το πρόγραμμα, χρησιμοποιώντας "
            "το δωρεάν API-Football μόνο ως πρόσθετο έλεγχο όταν έχει "
            "οριστεί key, κρύβει μη επιβεβαιωμένες ώρες και ξαναδιαβάζει "
            "τα δωρεάν CSV "
            "αποτελεσμάτων/στατιστικών. Οι νέοι ολοκληρωμένοι αγώνες προστίθενται αυτόματα στο ιστορικό και "
            "χρησιμοποιούνται αυτόματα στις επόμενες προβλέψεις με αυστηρό "
            "χρονικό cutoff."
        ),
    }
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
