from __future__ import annotations

import argparse
import copy
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from fixtur_es_source import LEAGUE_ID, resolve_team
from goal_scorer_enricher import FREE_KEY
from live_match_service import (
    STAT_ALIASES,
    build_live_payload,
    build_live_prediction,
    select_live_candidates,
)
from thesportsdb_recent import (
    discover_events,
    fixture_identity,
    preferred_provider_name,
)
from time_utils import parse_iso_datetime, utc_now

ATHENS_TZ = ZoneInfo("Europe/Athens")
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_ACTIVE_STATUSES = frozenset({"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate BetAnalytic zero-cost near-live JSON."
    )
    parser.add_argument("--feed-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--previous-live-file", type=Path)
    return parser.parse_args()


def _read_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _provider_search_copy(feed: dict[str, Any]) -> dict[str, Any]:
    """Use provider spellings internally without changing Android display names."""
    result = copy.deepcopy(feed)

    def patch_fixture(item: Any) -> None:
        if not isinstance(item, dict):
            return
        for side in ("home", "away"):
            block = item.get(f"{side}_team")
            if not isinstance(block, dict):
                continue
            team_id = block.get("team_id")
            canonical = str(block.get("team_name") or "").strip()
            if team_id is None or not canonical:
                continue
            block["team_name"] = preferred_provider_name(int(team_id), canonical)

    for item in result.get("live_candidates", []):
        patch_fixture(item)
    for season in result.get("seasons", []):
        if not isinstance(season, dict):
            continue
        for item in season.get("fixtures", []):
            patch_fixture(item)
    return result


def _seed_previous(
    previous: dict[str, Any],
    discovered: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    result = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    current = [
        item
        for item in result.get("matches", [])
        if isinstance(item, dict)
    ]
    by_fixture = {
        int(item["fixture_id"]): dict(item)
        for item in current
        if item.get("fixture_id") is not None
    }
    for fixture_id, event in discovered.items():
        event_id = str(event.get("idEvent") or "").strip()
        if not event_id:
            continue
        item = by_fixture.get(int(fixture_id), {"fixture_id": int(fixture_id)})
        item["provider_event_id"] = event_id
        by_fixture[int(fixture_id)] = item
    result["matches"] = list(by_fixture.values())
    return result


def _restore_display_teams(
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    display = {
        int(item["fixture_id"]): item
        for item in candidates
        if item.get("fixture_id") is not None
    }
    for match in payload.get("matches", []):
        if not isinstance(match, dict) or match.get("fixture_id") is None:
            continue
        original = display.get(int(match["fixture_id"]))
        if original is None:
            continue
        for side in ("home", "away"):
            block = original.get(f"{side}_team")
            if isinstance(block, dict):
                match[f"{side}_team"] = copy.deepcopy(block)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _candidate_is_in_active_window(candidate: dict[str, Any], now) -> bool:
    try:
        kickoff = parse_iso_datetime(str(candidate.get("fixture_date") or ""))
    except ValueError:
        return False
    delta = now - kickoff
    return timedelta(minutes=-10) <= delta <= timedelta(hours=3, minutes=45)


def _api_match_for_candidate(
    candidate: dict[str, Any],
    item: dict[str, Any],
) -> bool:
    identity = fixture_identity(candidate)
    if identity is None:
        return False
    _, home_id, _home_name, away_id, _away_name = identity

    teams = item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    api_home_id, _ = resolve_team(str(home.get("name") or ""))
    api_away_id, _ = resolve_team(str(away.get("name") or ""))
    if int(api_home_id) != home_id or int(api_away_id) != away_id:
        return False

    api_fixture = item.get("fixture") or {}
    try:
        api_date = parse_iso_datetime(
            str(api_fixture.get("date") or "")
        ).astimezone(ATHENS_TZ).date()
        candidate_date = parse_iso_datetime(
            str(candidate.get("fixture_date") or "")
        ).astimezone(ATHENS_TZ).date()
    except ValueError:
        return False
    return api_date == candidate_date


def _empty_statistics() -> dict[str, dict[str, None]]:
    return {key: {"home": None, "away": None} for key in STAT_ALIASES}


def _api_football_live_fallback(
    candidates: list[dict[str, Any]],
    *,
    now,
    api_key: str,
    existing_fixture_ids: set[int],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    missing = [
        candidate
        for candidate in candidates
        if candidate.get("fixture_id") is not None
        and int(candidate["fixture_id"]) not in existing_fixture_ids
        and _candidate_is_in_active_window(candidate, now)
    ]
    if not missing or not api_key:
        return [], 0, []

    by_date: dict[str, list[dict[str, Any]]] = {}
    for candidate in missing:
        local_date = parse_iso_datetime(
            str(candidate["fixture_date"])
        ).astimezone(ATHENS_TZ).date().isoformat()
        by_date.setdefault(local_date, []).append(candidate)

    http = requests.Session()
    http.headers.update(
        {
            "x-apisports-key": api_key,
            "User-Agent": "BetAnalytic/1.2 (+current-date live fallback)",
        }
    )
    requests_used = 0
    warnings: list[str] = []
    matches: list[dict[str, Any]] = []

    try:
        for local_date, date_candidates in by_date.items():
            try:
                requests_used += 1
                response = http.get(
                    f"{API_FOOTBALL_BASE_URL}/fixtures",
                    params={
                        "league": LEAGUE_ID,
                        "date": local_date,
                        "timezone": "Europe/Athens",
                    },
                    timeout=25,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("API-Football returned non-object JSON.")
                if data.get("errors"):
                    raise ValueError(f"API-Football errors: {data['errors']}")
                items = data.get("response")
                api_items = (
                    [item for item in items if isinstance(item, dict)]
                    if isinstance(items, list)
                    else []
                )
            except (requests.RequestException, ValueError) as exc:
                warnings.append(
                    f"API-Football LIVE fallback {local_date} απέτυχε: {exc}"
                )
                continue

            for candidate in date_candidates:
                for item in api_items:
                    if not _api_match_for_candidate(candidate, item):
                        continue

                    api_fixture = item.get("fixture") or {}
                    status_block = api_fixture.get("status") or {}
                    status = str(status_block.get("short") or "").upper().strip()
                    if status not in API_ACTIVE_STATUSES:
                        continue

                    goals = item.get("goals") or {}
                    home_score = _as_int(goals.get("home"))
                    away_score = _as_int(goals.get("away"))
                    if home_score is None or away_score is None:
                        continue

                    minute = _as_int(status_block.get("elapsed"))
                    statistics = _empty_statistics()
                    match = {
                        "fixture_id": int(candidate["fixture_id"]),
                        "provider_event_id": None,
                        "provider": "API-Football Free current-date fallback",
                        "provider_updated_at": None,
                        "fixture_date": candidate.get("fixture_date"),
                        "status": status,
                        "progress": str(
                            status_block.get("long")
                            or status_block.get("short")
                            or "LIVE"
                        ),
                        "minute": minute,
                        "minute_estimated": minute is None,
                        "home_team": copy.deepcopy(candidate.get("home_team") or {}),
                        "away_team": copy.deepcopy(candidate.get("away_team") or {}),
                        "score": {"home": home_score, "away": away_score},
                        "events": [],
                        "statistics": statistics,
                        "statistics_available": False,
                        "live_prediction": build_live_prediction(
                            candidate,
                            home_score=home_score,
                            away_score=away_score,
                            minute=minute,
                            statistics=statistics,
                        ),
                    }
                    matches.append(match)
                    break
    finally:
        http.close()

    return matches, requests_used, warnings


def main() -> int:
    args = _parse_args()
    feed = _read_json(args.feed_file)
    if not feed:
        raise SystemExit(f"Invalid or missing feed: {args.feed_file}")

    previous = _read_json(args.previous_live_file)
    now = utc_now()
    candidates = select_live_candidates(feed, as_of=now)
    tdb_key = str(os.getenv("THESPORTSDB_KEY") or FREE_KEY).strip()

    # Discover provider IDs by date first. This fixes cases where the event title
    # differs from our canonical name (e.g. "Volos NFC" vs "Volos").
    discovered, discovery_requests, discovery_warnings = discover_events(
        candidates,
        api_key=tdb_key,
        max_searches_per_fixture=6,
    )
    seeded_previous = _seed_previous(previous, discovered)

    payload = build_live_payload(
        _provider_search_copy(feed),
        previous_live=seeded_previous,
        as_of=now,
        api_key=tdb_key,
    )
    _restore_display_teams(payload, candidates)
    payload["requests_used"] = int(payload.get("requests_used") or 0) + discovery_requests
    payload.setdefault("warnings", []).extend(discovery_warnings)

    # Last-resort free current-date fallback only for a candidate that should
    # already be in play but TheSportsDB did not return as live.
    existing_ids = {
        int(item["fixture_id"])
        for item in payload.get("matches", [])
        if isinstance(item, dict) and item.get("fixture_id") is not None
    }
    fallback_matches, fallback_requests, fallback_warnings = (
        _api_football_live_fallback(
            candidates,
            now=now,
            api_key=str(os.getenv("API_FOOTBALL_KEY") or "").strip(),
            existing_fixture_ids=existing_ids,
        )
    )
    if fallback_matches:
        payload.setdefault("matches", []).extend(fallback_matches)
        source = payload.setdefault("source", {})
        source["name"] = "TheSportsDB API v1 + API-Football Free fallback"
        source["fallback_used"] = True

    payload["matches"] = sorted(
        [
            item
            for item in payload.get("matches", [])
            if isinstance(item, dict)
        ],
        key=lambda item: str(item.get("fixture_date") or ""),
    )
    payload["matches_count"] = len(payload["matches"])
    payload["requests_used"] = int(payload.get("requests_used") or 0) + fallback_requests
    payload.setdefault("warnings", []).extend(fallback_warnings)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "live.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)

    print(
        json.dumps(
            {
                "status": "ok",
                "live_matches": payload["matches_count"],
                "requests_used": payload["requests_used"],
                "live_path": str(output),
                "warnings": payload["warnings"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
