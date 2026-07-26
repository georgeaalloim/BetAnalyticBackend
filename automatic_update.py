from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from automation_config import AutomationConfig
from database import initialize_database, save_fixtures
from football_api import api_get
from r2_storage import R2Publisher, UploadItem
from static_feed_generator import generate_static_feed
from time_utils import parse_iso_datetime, to_iso_z, utc_now


load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Συγχρονίζει αυτόματα αγώνες, επανυπολογίζει προβλέψεις "
            "χωρίς temporal leakage και δημοσιεύει στατικό JSON feed."
        )
    )
    parser.add_argument(
        "--output-dir",
        default="automation_output",
        help="Φάκελος παραγωγής των feed.json και manifest.json.",
    )
    parser.add_argument(
        "--seasons",
        default=None,
        help="Ρητή λίστα σεζόν, π.χ. 2025,2026. Κενό = auto.",
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
        help="Δεν καλεί το API-Football· χρησιμοποιεί μόνο την τοπική DB.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Παράγει το feed τοπικά χωρίς upload στο R2.",
    )
    return parser.parse_args()


def _fallback_season_candidates(as_of: datetime) -> tuple[int, ...]:
    """
    Εφεδρική εκτίμηση όταν αποτύχει το endpoint /leagues.

    Οι ευρωπαϊκές σεζόν συνήθως παίρνουν ως API season το έτος έναρξης.
    Η τιμή είναι μόνο fallback· η κανονική ροή χρησιμοποιεί το current
    flag που επιστρέφει το API-Football.
    """

    season = as_of.year if as_of.month >= 7 else as_of.year - 1
    return (season, season + 1)


def discover_seasons(
    config: AutomationConfig,
    as_of: datetime,
) -> tuple[tuple[int, ...], dict[str, Any]]:
    if config.sync_seasons is not None:
        return config.sync_seasons, {
            "mode": "explicit",
            "seasons": list(config.sync_seasons),
        }

    try:
        data = api_get(
            endpoint="/leagues",
            params={"id": config.league_id},
        )
        response_items = data.get("response", [])
        if not response_items:
            raise RuntimeError("Το API δεν επέστρεψε τη διοργάνωση.")

        seasons = response_items[0].get("seasons", [])
        season_years = sorted(
            {
                int(item["year"])
                for item in seasons
                if item.get("year") is not None
            }
        )
        current_years = [
            int(item["year"])
            for item in seasons
            if item.get("current") is True and item.get("year") is not None
        ]

        if current_years:
            selected = [max(current_years)]
        elif season_years:
            selected = [max(season_years)]
        else:
            raise RuntimeError("Δεν βρέθηκε καμία διαθέσιμη σεζόν.")

        if config.include_next_season:
            next_season = selected[0] + 1
            if next_season not in selected:
                selected.append(next_season)

        result = tuple(sorted(selected))
        return result, {
            "mode": "api-discovery",
            "seasons": list(result),
            "available_seasons": season_years,
        }

    except (requests.RequestException, RuntimeError, ValueError) as error:
        fallback = _fallback_season_candidates(as_of)
        return fallback, {
            "mode": "fallback",
            "seasons": list(fallback),
            "warning": str(error),
        }


def sync_fixtures(
    config: AutomationConfig,
    seasons: tuple[int, ...],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    for season in seasons:
        try:
            data = api_get(
                endpoint="/fixtures",
                params={
                    "league": config.league_id,
                    "season": season,
                },
            )
            fixtures = data.get("response", [])
            if not isinstance(fixtures, list):
                raise RuntimeError("Το response του /fixtures δεν είναι λίστα.")

            processed = save_fixtures(fixtures)
            results.append(
                {
                    "season": season,
                    "status": "ok",
                    "received": len(fixtures),
                    "processed": processed,
                }
            )
        except (requests.RequestException, RuntimeError, ValueError) as error:
            results.append(
                {
                    "season": season,
                    "status": "error",
                    "error": str(error),
                }
            )

    return {
        "seasons_requested": list(seasons),
        "successful_seasons": [
            item["season"] for item in results if item["status"] == "ok"
        ],
        "failed_seasons": [
            item["season"] for item in results if item["status"] == "error"
        ],
        "results": results,
    }


def publish_to_r2(
    config: AutomationConfig,
    feed_path: Path,
    manifest_path: Path,
) -> list[str]:
    publisher = R2Publisher(config)

    # Το feed ανεβαίνει πρώτο. Το manifest ανεβαίνει τελευταίο και λειτουργεί
    # ως ο ατομικός δείκτης προς την καινούργια έκδοση δεδομένων.
    return publisher.upload_many(
        [
            UploadItem(
                local_path=feed_path,
                object_key="feed.json",
                content_type="application/json; charset=utf-8",
                cache_control="public, max-age=300",
            ),
            UploadItem(
                local_path=manifest_path,
                object_key="manifest.json",
                content_type="application/json; charset=utf-8",
                cache_control="no-cache, no-store, must-revalidate",
            ),
        ]
    )


def main() -> int:
    args = _parse_args()
    config = AutomationConfig.from_environment(
        output_dir=args.output_dir,
        sync_seasons_override=args.seasons,
        lookahead_days_override=args.lookahead_days,
    )
    as_of = parse_iso_datetime(args.as_of) if args.as_of else utc_now()

    initialize_database()

    seasons, discovery_summary = discover_seasons(config, as_of)
    if args.skip_sync:
        sync_summary: dict[str, Any] = {
            "skipped": True,
            "seasons_requested": list(seasons),
        }
    else:
        sync_summary = sync_fixtures(config, seasons)

    sync_summary["season_discovery"] = discovery_summary
    sync_summary["finished_at"] = to_iso_z(utc_now())

    feed_url = config.public_object_url("feed.json")
    generated = generate_static_feed(
        output_dir=config.output_dir,
        league_id=config.league_id,
        league_name=config.league_name,
        seasons=seasons,
        as_of=as_of,
        lookahead_days=config.lookahead_days,
        upcoming_statuses=config.upcoming_statuses,
        feed_public_url=feed_url,
        sync_summary=sync_summary,
    )

    uploaded_urls: list[str] = []
    upload_status = "skipped"
    if not args.skip_upload:
        if config.r2_is_configured:
            uploaded_urls = publish_to_r2(
                config=config,
                feed_path=generated.feed_path,
                manifest_path=generated.manifest_path,
            )
            upload_status = "ok"
        else:
            upload_status = "not-configured"

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
        "upload_status": upload_status,
        "uploaded_urls": uploaded_urls,
        "sync_summary": sync_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - workflow must print the real cause.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise
