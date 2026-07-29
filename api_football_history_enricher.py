from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from database import (
    get_connection,
    initialize_database,
    save_fixture_history_details,
)
from fixtur_es_source import LEAGUE_ID, resolve_team
from match_statistics import STATISTIC_ALIASES, utc_now_iso
from time_utils import parse_iso_datetime

BASE_URL = "https://v3.football.api-sports.io"
SOURCE_NAME = "API-Football Free fixture details"
ATHENS_TZ = ZoneInfo("Europe/Athens")
REQUEST_TIMEOUT_SECONDS = 30
MAX_IDS_PER_REQUEST = 20


@dataclass(frozen=True)
class EnrichmentResult:
    enabled: bool
    seasons: list[int]
    completed_matches_considered: int
    api_matches_found: int
    matches_enriched: int
    requests_used: int
    quota_remaining: int | None
    warnings: list[str]


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            cleaned = value.strip().replace("%", "")
            if not cleaned or cleaned.lower() in {"null", "none", "-"}:
                return None
            return int(float(cleaned))
        return int(value)
    except (TypeError, ValueError):
        return None


def _chunks(items: list[int], size: int = MAX_IDS_PER_REQUEST) -> Iterable[list[int]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _local_date(value: str) -> str | None:
    try:
        return parse_iso_datetime(value).astimezone(ATHENS_TZ).date().isoformat()
    except ValueError:
        return None


def _canonical_completed_rows(seasons: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in seasons)
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT
                f.fixture_id,
                f.season,
                f.fixture_date,
                f.home_team_id,
                f.home_team_name,
                f.away_team_id,
                f.away_team_name,
                h.goal_scorers_json,
                h.home_total_shots,
                h.away_total_shots,
                h.home_shots_on_target,
                h.away_shots_on_target,
                h.home_fouls,
                h.away_fouls,
                h.home_corners,
                h.away_corners
            FROM fixtures AS f
            LEFT JOIN fixture_history_details AS h
              ON h.fixture_id = f.fixture_id
            WHERE f.league_id = ?
              AND f.season IN ({placeholders})
              AND f.status = 'FT'
              AND f.home_goals IS NOT NULL
              AND f.away_goals IS NOT NULL
            ORDER BY f.fixture_date ASC, f.fixture_id ASC
            """,
            (LEAGUE_ID, *seasons),
        ).fetchall()
    return [dict(row) for row in rows]


def _needs_enrichment(row: dict[str, Any]) -> bool:
    important = (
        "home_total_shots",
        "away_total_shots",
        "home_shots_on_target",
        "away_shots_on_target",
        "home_fouls",
        "away_fouls",
        "home_corners",
        "away_corners",
    )
    return not row.get("goal_scorers_json") or any(row.get(key) is None for key in important)


def _request_json(
    http: requests.Session,
    path: str,
    *,
    params: dict[str, Any],
) -> tuple[dict[str, Any], int | None]:
    response = http.get(
        f"{BASE_URL}{path}",
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Το API-Football επέστρεψε μη έγκυρο JSON object.")
    errors = payload.get("errors")
    if errors:
        raise ValueError(f"API-Football errors: {errors}")
    remaining_raw = response.headers.get("x-ratelimit-requests-remaining")
    remaining = _as_int(remaining_raw)
    return payload, remaining


def _api_match_index(items: list[dict[str, Any]]) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
    result: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        fixture = item.get("fixture") or {}
        league = item.get("league") or {}
        teams = item.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        status = str((fixture.get("status") or {}).get("short") or "").upper()
        if status != "FT":
            continue
        api_id = _as_int(fixture.get("id"))
        season = _as_int(league.get("season"))
        date_value = str(fixture.get("date") or "")
        if api_id is None or season is None or not date_value:
            continue
        home_id, home_name = resolve_team(str(home.get("name") or ""))
        away_id, away_name = resolve_team(str(away.get("name") or ""))
        normalized = dict(item)
        normalized["_api_fixture_id"] = api_id
        normalized["_local_date"] = _local_date(date_value)
        normalized["_canonical_home_id"] = home_id
        normalized["_canonical_away_id"] = away_id
        normalized["_canonical_home_name"] = home_name
        normalized["_canonical_away_name"] = away_name
        result.setdefault((int(season), int(home_id), int(away_id)), []).append(normalized)
    return result


def _match_api_fixture(
    row: dict[str, Any],
    index: dict[tuple[int, int, int], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    key = (int(row["season"]), int(row["home_team_id"]), int(row["away_team_id"]))
    candidates = index.get(key, [])
    if not candidates:
        return None
    canonical_date = _local_date(str(row.get("fixture_date") or ""))
    if canonical_date is None:
        return candidates[0]
    exact = [item for item in candidates if item.get("_local_date") == canonical_date]
    if exact:
        return exact[0]
    try:
        target = datetime.fromisoformat(canonical_date).date()
        ranked = sorted(
            candidates,
            key=lambda item: abs(
                (datetime.fromisoformat(str(item.get("_local_date"))).date() - target).days
            ) if item.get("_local_date") else 999,
        )
        if ranked and ranked[0].get("_local_date"):
            difference = abs(
                (datetime.fromisoformat(str(ranked[0]["_local_date"])).date() - target).days
            )
            if difference <= 2:
                return ranked[0]
    except ValueError:
        pass
    return None


def _stats_by_api_team(item: dict[str, Any]) -> dict[int, dict[str, int | None]]:
    result: dict[int, dict[str, int | None]] = {}
    statistics = item.get("statistics")
    if not isinstance(statistics, list):
        return result
    for block in statistics:
        if not isinstance(block, dict):
            continue
        team = block.get("team") or {}
        team_id = _as_int(team.get("id"))
        if team_id is None:
            continue
        values: dict[str, int | None] = {}
        for entry in block.get("statistics") or []:
            if not isinstance(entry, dict):
                continue
            alias = STATISTIC_ALIASES.get(str(entry.get("type") or "").strip().lower())
            if alias:
                values[alias] = _as_int(entry.get("value"))
        result[int(team_id)] = values
    return result


def _goal_scorers(item: dict[str, Any]) -> list[dict[str, Any]]:
    scorers: list[dict[str, Any]] = []
    events = item.get("events")
    if not isinstance(events, list):
        return scorers
    for event in events:
        if not isinstance(event, dict) or str(event.get("type") or "").lower() != "goal":
            continue
        detail = str(event.get("detail") or "")
        if "missed" in detail.lower() or "cancel" in detail.lower():
            continue
        player = event.get("player") or {}
        team = event.get("team") or {}
        time_data = event.get("time") or {}
        player_name = str(player.get("name") or "").strip()
        if not player_name:
            continue
        team_id = _as_int(team.get("id"))
        minute = _as_int(time_data.get("elapsed"))
        extra = _as_int(time_data.get("extra"))
        scorers.append(
            {
                "player_name": player_name,
                "team_api_id": team_id,
                "team_name": str(team.get("name") or ""),
                "minute": minute,
                "extra_minute": extra,
                "detail": detail,
            }
        )
    return scorers


def _detail_record(canonical: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    teams = item.get("teams") or {}
    api_home = teams.get("home") or {}
    api_away = teams.get("away") or {}
    home_api_id = _as_int(api_home.get("id"))
    away_api_id = _as_int(api_away.get("id"))
    by_team = _stats_by_api_team(item)
    home = by_team.get(int(home_api_id), {}) if home_api_id is not None else {}
    away = by_team.get(int(away_api_id), {}) if away_api_id is not None else {}
    scorers = _goal_scorers(item)
    for scorer in scorers:
        if scorer.get("team_api_id") == home_api_id:
            scorer["side"] = "home"
            scorer["team_id"] = int(canonical["home_team_id"])
            scorer["team_name"] = str(canonical["home_team_name"])
        elif scorer.get("team_api_id") == away_api_id:
            scorer["side"] = "away"
            scorer["team_id"] = int(canonical["away_team_id"])
            scorer["team_name"] = str(canonical["away_team_name"])
        scorer.pop("team_api_id", None)
    collected_at = utc_now_iso()
    return {
        "fixture_id": int(canonical["fixture_id"]),
        "home_total_shots": home.get("total_shots"),
        "away_total_shots": away.get("total_shots"),
        "home_shots_on_target": home.get("shots_on_target"),
        "away_shots_on_target": away.get("shots_on_target"),
        "home_fouls": home.get("fouls"),
        "away_fouls": away.get("fouls"),
        "home_yellow_cards": home.get("yellow_cards"),
        "away_yellow_cards": away.get("yellow_cards"),
        "home_red_cards": home.get("red_cards"),
        "away_red_cards": away.get("red_cards"),
        "home_offsides": home.get("offsides"),
        "away_offsides": away.get("offsides"),
        "home_corners": home.get("corners"),
        "away_corners": away.get("corners"),
        "goal_scorers_json": json.dumps(scorers, ensure_ascii=False),
        "source": SOURCE_NAME,
        "collected_at": collected_at,
    }


def enrich_history(
    *,
    seasons: Iterable[int],
    api_key: str | None,
    session: requests.Session | None = None,
    recent_days: int | None = None,
    max_detail_batches: int | None = None,
) -> EnrichmentResult:
    requested = sorted(set(int(value) for value in seasons))
    cleaned_key = str(api_key or "").strip()
    if not cleaned_key:
        return EnrichmentResult(
            enabled=False,
            seasons=requested,
            completed_matches_considered=0,
            api_matches_found=0,
            matches_enriched=0,
            requests_used=0,
            quota_remaining=None,
            warnings=["Δεν έχει οριστεί το δωρεάν API_FOOTBALL_KEY."],
        )

    initialize_database()
    rows = [row for row in _canonical_completed_rows(requested) if _needs_enrichment(row)]
    if recent_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(recent_days)))
        recent_rows: list[dict[str, Any]] = []
        for row in rows:
            try:
                fixture_date = parse_iso_datetime(str(row.get("fixture_date") or ""))
            except ValueError:
                continue
            if fixture_date >= cutoff:
                recent_rows.append(row)
        rows = recent_rows
    rows.sort(key=lambda row: str(row.get("fixture_date") or ""), reverse=True)
    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"x-apisports-key": cleaned_key, "User-Agent": "BetAnalytic/1.0"})
    requests_used = 0
    quota_remaining: int | None = None
    warnings: list[str] = []
    api_items: list[dict[str, Any]] = []
    try:
        for season in requested:
            try:
                payload, remaining = _request_json(
                    http,
                    "/fixtures",
                    params={"league": LEAGUE_ID, "season": season, "timezone": "Europe/Athens"},
                )
                requests_used += 1
                if remaining is not None:
                    quota_remaining = remaining
                response = payload.get("response")
                if isinstance(response, list):
                    api_items.extend(item for item in response if isinstance(item, dict))
            except (requests.RequestException, ValueError) as error:
                warnings.append(f"Αποτυχία λίστας API-Football για {season}: {error}")

        index = _api_match_index(api_items)
        canonical_by_api_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            matched = _match_api_fixture(row, index)
            if matched is not None:
                canonical_by_api_id[int(matched["_api_fixture_id"])] = row

        detail_records: list[dict[str, Any]] = []
        ordered_ids = [
            api_id
            for api_id, _row in sorted(
                canonical_by_api_id.items(),
                key=lambda item: str(item[1].get("fixture_date") or ""),
                reverse=True,
            )
        ]
        if max_detail_batches is not None:
            ordered_ids = ordered_ids[: max(0, int(max_detail_batches)) * MAX_IDS_PER_REQUEST]
        for batch in _chunks(ordered_ids):
            try:
                payload, remaining = _request_json(
                    http,
                    "/fixtures",
                    params={"ids": "-".join(str(value) for value in batch)},
                )
                requests_used += 1
                if remaining is not None:
                    quota_remaining = remaining
                response = payload.get("response")
                if not isinstance(response, list):
                    continue
                for item in response:
                    if not isinstance(item, dict):
                        continue
                    api_id = _as_int((item.get("fixture") or {}).get("id"))
                    canonical = canonical_by_api_id.get(int(api_id)) if api_id is not None else None
                    if canonical is not None:
                        detail_records.append(_detail_record(canonical, item))
            except (requests.RequestException, ValueError) as error:
                warnings.append(f"Αποτυχία λεπτομερειών API-Football για {batch}: {error}")

        saved = save_fixture_history_details(detail_records)
        return EnrichmentResult(
            enabled=True,
            seasons=requested,
            completed_matches_considered=len(rows),
            api_matches_found=len(canonical_by_api_id),
            matches_enriched=saved,
            requests_used=requests_used,
            quota_remaining=quota_remaining,
            warnings=warnings,
        )
    finally:
        if own_session:
            http.close()


def _parse_seasons(value: str) -> list[int]:
    return sorted({int(item.strip()) for item in value.split(",") if item.strip()})


def main() -> int:
    parser = argparse.ArgumentParser(description="Συμπληρώνει δωρεάν στατιστικά ιστορικού και σκόρερ.")
    parser.add_argument("--seasons", default="2025,2026")
    parser.add_argument("--summary-output", default="data/history_enrichment_summary.json")
    args = parser.parse_args()
    result = enrich_history(
        seasons=_parse_seasons(args.seasons),
        api_key=os.getenv("API_FOOTBALL_KEY"),
    )
    payload = {
        "enabled": result.enabled,
        "seasons": result.seasons,
        "completed_matches_considered": result.completed_matches_considered,
        "api_matches_found": result.api_matches_found,
        "matches_enriched": result.matches_enriched,
        "requests_used": result.requests_used,
        "quota_remaining": result.quota_remaining,
        "warnings": result.warnings,
        "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    path = Path(args.summary_output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
