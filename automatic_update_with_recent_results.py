from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from api_football_history_enricher import enrich_history
from fixtur_es_source import LEAGUE_ID, season_from_local_date
from ofstats_scorer_fallback import enrich_goal_scorers_from_ofstats
from recent_result_sync import sync_recent_results
from static_feed_generator import _build_history_payload
from time_utils import parse_iso_datetime, utc_now


ATHENS_TZ = ZoneInfo("Europe/Athens")
RECENT_HISTORY_DAYS = 7


def _as_of_from_argv():
    args = sys.argv[1:]
    for index, value in enumerate(args):
        if value == "--as-of" and index + 1 < len(args):
            return parse_iso_datetime(args[index + 1])
        if value.startswith("--as-of="):
            return parse_iso_datetime(value.split("=", 1)[1])
    return utc_now()


def _option_value(name: str, default: str) -> str:
    args = sys.argv[1:]
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return args[index + 1]
        prefix = name + "="
        if value.startswith(prefix):
            return value[len(prefix):]
    return default


def _has_flag(name: str) -> bool:
    return name in sys.argv[1:]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _refresh_generated_history(
    output_dir: Path,
    *,
    as_of,
    previous_feed: dict[str, Any],
) -> None:
    """Refresh only History after recent API details/scorers are saved."""
    feed_path = output_dir / "feed.json"
    manifest_path = output_dir / "manifest.json"
    feed = _read_json(feed_path)
    manifest = _read_json(manifest_path)
    if not feed or not manifest:
        return

    previous_summary = previous_feed.get("sync_summary")
    current_summary = feed.get("sync_summary")
    if isinstance(previous_summary, dict) and isinstance(current_summary, dict):
        if _has_flag("--skip-sync") and isinstance(previous_summary.get("fixtures"), dict):
            current_summary["fixtures"] = previous_summary["fixtures"]
        if (
            _has_flag("--skip-detailed-stats")
            and isinstance(previous_summary.get("detailed_statistics"), dict)
        ):
            current_summary["detailed_statistics"] = previous_summary["detailed_statistics"]

    default_season = season_from_local_date(as_of.astimezone(ATHENS_TZ).date())
    feed["history"] = _build_history_payload(
        league_id=int((feed.get("league") or {}).get("league_id") or LEAGUE_ID),
        default_season=default_season,
    )
    _write_json(feed_path, feed)

    feed_sha256 = hashlib.sha256(feed_path.read_bytes()).hexdigest()
    manifest["feed_sha256"] = feed_sha256
    history = feed["history"]
    manifest["history_matches_count"] = sum(
        int(item.get("matches_count") or 0)
        for item in history.get("seasons", [])
        if isinstance(item, dict)
    )
    manifest["history_default_season"] = int(
        history.get("default_season") or default_season
    )
    _write_json(manifest_path, manifest)


def main() -> int:
    as_of = _as_of_from_argv()
    output_dir = Path(_option_value("--output-dir", "automation_output"))
    previous_feed = _read_json(output_dir / "feed.json")

    # 1) Promote a just-finished fixture to FT before feed generation.
    api_football_key = str(os.getenv("API_FOOTBALL_KEY") or "").strip()
    result = sync_recent_results(
        as_of=as_of,
        recent_hours=18.0,
        thesportsdb_key=os.getenv("THESPORTSDB_KEY"),
        api_football_key=api_football_key,
    )
    _write_json(Path("data/recent_result_sync_summary.json"), asdict(result))

    # 2) Build the normal feed without wasting API-Football quota on blocked
    # full-season requests. The key is restored immediately afterwards.
    previous_api_key = os.environ.pop("API_FOOTBALL_KEY", None)
    try:
        from automatic_update import main as automatic_update_main
        return_code = int(automatic_update_main())
    finally:
        if previous_api_key is not None:
            os.environ["API_FOOTBALL_KEY"] = previous_api_key

    if return_code != 0:
        return return_code

    active_season = season_from_local_date(as_of.astimezone(ATHENS_TZ).date())

    # 3) Keep the existing optional recent-date API-Football enrichment.
    enrichment = enrich_history(
        seasons=(active_season,),
        api_key=api_football_key,
        recent_days=RECENT_HISTORY_DAYS,
        max_detail_batches=1,
        scorers_only=False,
        date_fallback_days=RECENT_HISTORY_DAYS,
    )

    # 4) TheSportsDB is still the primary scorer source inside automatic_update.
    # If its timeline is empty/stale, fill ONLY still-missing scorer rows from a
    # second free public source. Nothing is saved unless date + teams + FT score
    # and the complete goal progression agree exactly.
    scorer_fallback = enrich_goal_scorers_from_ofstats(
        season=active_season,
        recent_days=RECENT_HISTORY_DAYS,
        max_matches=8,
        as_of=as_of,
    )

    # Preserve the existing summary contract. `matches_enriched` is the effective
    # number of recent DB enrichments so the existing GitHub workflow also commits
    # scorer-only fallback changes. Raw API count and fallback detail stay explicit.
    history_summary = asdict(enrichment)
    api_matches_enriched = int(history_summary.get("matches_enriched") or 0)
    history_summary["api_matches_enriched"] = api_matches_enriched
    history_summary["scorer_fallback"] = asdict(scorer_fallback)
    history_summary["matches_enriched"] = (
        api_matches_enriched + int(scorer_fallback.matches_saved)
    )
    _write_json(Path("data/recent_history_enrichment_summary.json"), history_summary)

    # 5) Feed was generated before steps 3-4. Rebuild ONLY History so newly
    # saved scorer rows are published immediately; predictions/stats remain intact.
    _refresh_generated_history(
        output_dir,
        as_of=as_of,
        previous_feed=previous_feed,
    )

    print(
        json.dumps(
            {
                "recent_results_updated": result.updated,
                "recent_scorer_sets_saved": result.scorer_sets_saved,
                "recent_history_matches_enriched": enrichment.matches_enriched,
                "recent_history_requests_used": enrichment.requests_used,
                "recent_history_pending": enrichment.pending_matches,
                "ofstats_scorer_matches_found": scorer_fallback.matches_found,
                "ofstats_scorer_sets_saved": scorer_fallback.matches_saved,
                "ofstats_scorer_requests_used": scorer_fallback.requests_used,
                "ofstats_scorer_pending": scorer_fallback.pending_matches,
                "ofstats_warnings": scorer_fallback.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
