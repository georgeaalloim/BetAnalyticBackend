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
    save_fixture_goal_scorers,
    save_fixture_history_details,
)
from fixtur_es_source import LEAGUE_ID, resolve_team, season_from_local_date
from match_statistics import STATISTIC_ALIASES, utc_now_iso
from statistics_source_policy import available_stat_pairs, source_key
from time_utils import parse_iso_datetime

BASE_URL = "https://v3.football.api-sports.io"
SOURCE_NAME = "API-Football Free fixture details"
ATHENS_TZ = ZoneInfo("Europe/Athens")
REQUEST_TIMEOUT_SECONDS = 30
MAX_MATCHES_PER_RUN = 40
MAX_DATE_FALLBACK_REQUESTS = 7


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
    score_mismatches: int = 0
    pending_matches: int = 0


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
                f.home_goals,
                f.away_goals,
                h.goal_scorers_json,
                g.goal_scorers_json AS dedicated_goal_scorers_json,
                g.score_verified AS dedicated_score_verified,
                g.source AS dedicated_scorer_source,
                h.home_total_shots,
                h.away_total_shots,
                h.home_shots_on_target,
                h.away_shots_on_target,
                h.home_fouls,
                h.away_fouls,
                h.home_yellow_cards,
                h.away_yellow_cards,
                h.home_red_cards,
                h.away_red_cards,
                h.home_offsides,
                h.away_offsides,
                h.home_corners,
                h.away_corners,
                h.source AS history_source,
                h.score_verified,
                h.available_stat_pairs
            FROM fixtures AS f
            LEFT JOIN fixture_history_details AS h
              ON h.fixture_id = f.fixture_id
            LEFT JOIN fixture_goal_scorers AS g
              ON g.fixture_id = f.fixture_id
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


def _parsed_scorers(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = None
    if row.get("dedicated_goal_scorers_json") and bool(row.get("dedicated_score_verified")):
        raw = row.get("dedicated_goal_scorers_json")
    elif row.get("goal_scorers_json"):
        raw = row.get("goal_scorers_json")
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _scorers_complete(row: dict[str, Any]) -> bool:
    expected_goals = int(row.get("home_goals") or 0) + int(row.get("away_goals") or 0)
    if expected_goals == 0:
        return True
    return len(_parsed_scorers(row)) >= expected_goals


def _statistics_complete(row: dict[str, Any]) -> bool:
    return (
        source_key(row.get("history_source")) == "api_football"
        and bool(row.get("score_verified"))
        and int(row.get("available_stat_pairs") or 0) >= 5
    )


def _needs_enrichment(row: dict[str, Any], *, scorers_only: bool = False) -> bool:
    if scorers_only:
        return not _scorers_complete(row)
    return not (_statistics_complete(row) and _scorers_complete(row))


def _within_date_fallback_window(row: dict[str, Any], days: int | None) -> bool:
    if days is None:
        return True
    try:
        fixture_time = parse_iso_datetime(str(row.get("fixture_date") or ""))
    except ValueError:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return fixture_time >= cutoff


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
    remaining = _as_int(response.headers.get("x-ratelimit-requests-remaining"))
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
        home_id, _home_name = resolve_team(str(home.get("name") or ""))
        away_id, _away_name = resolve_team(str(away.get("name") or ""))
        normalized = dict(item)
        normalized["_api_fixture_id"] = api_id
        normalized["_local_date"] = _local_date(date_value)
        normalized["_canonical_home_id"] = home_id
        normalized["_canonical_away_id"] = away_id
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
    exact = [item for item in candidates if item.get("_local_date") == canonical_date]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1 and canonical_date is None:
        return candidates[0]
    if canonical_date is None:
        return None
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
            if difference <= 1:
                return ranked[0]
    except ValueError:
        pass
    return None


def _score_matches(canonical: dict[str, Any], api_item: dict[str, Any]) -> bool:
    goals = api_item.get("goals") or {}
    api_home = _as_int(goals.get("home"))
    api_away = _as_int(goals.get("away"))
    return (
        api_home is not None
        and api_away is not None
        and api_home == int(canonical["home_goals"])
        and api_away == int(canonical["away_goals"])
    )


def _statistics_by_api_team(response_items: Any) -> dict[int, dict[str, int | None]]:
    result: dict[int, dict[str, int | None]] = {}
    if not isinstance(response_items, list):
        return result
    for block in response_items:
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


def _goal_scorers(events: Any) -> list[dict[str, Any]]:
    scorers: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    if not isinstance(events, list):
        return scorers
    for event in events:
        if not isinstance(event, dict) or str(event.get("type") or "").lower() != "goal":
            continue
        detail = str(event.get("detail") or "")
        lowered_detail = detail.lower()
        if "missed" in lowered_detail or "cancel" in lowered_detail or "no goal" in lowered_detail:
            continue
        player = event.get("player") or {}
        team = event.get("team") or {}
        time_data = event.get("time") or {}
        player_name = str(player.get("name") or "").strip()
        if not player_name:
            continue
        item = {
            "player_name": player_name,
            "team_api_id": _as_int(team.get("id")),
            "team_name": str(team.get("name") or ""),
            "minute": _as_int(time_data.get("elapsed")),
            "extra_minute": _as_int(time_data.get("extra")),
            "detail": detail,
        }
        key = (
            item["player_name"].casefold(),
            item["team_api_id"],
            item["minute"],
            item["extra_minute"],
            item["detail"].casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        scorers.append(item)
    return scorers


def _existing_stat(canonical: dict[str, Any], field: str) -> int | None:
    value = canonical.get(field)
    return _as_int(value)


def _detail_record(
    canonical: dict[str, Any],
    api_item: dict[str, Any],
    statistics_response: Any | None,
    events_response: Any | None,
) -> dict[str, Any]:
    teams = api_item.get("teams") or {}
    api_home = teams.get("home") or {}
    api_away = teams.get("away") or {}
    home_api_id = _as_int(api_home.get("id"))
    away_api_id = _as_int(api_away.get("id"))

    by_team = _statistics_by_api_team(statistics_response)
    home = by_team.get(int(home_api_id), {}) if home_api_id is not None else {}
    away = by_team.get(int(away_api_id), {}) if away_api_id is not None else {}

    # Important for the free plan: scorer events are useful even if the
    # statistics endpoint is unavailable. Existing API-Football fields are
    # kept and Football-Data numeric statistics remain in their own table.
    scorers = _goal_scorers(events_response) if events_response is not None else []
    if not scorers:
        scorers = _parsed_scorers(canonical)

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

    def stat_value(side: str, alias: str, field: str) -> int | None:
        snapshot = home if side == "home" else away
        value = snapshot.get(alias)
        if value is not None:
            return _as_int(value)
        return _existing_stat(canonical, field)

    record = {
        "fixture_id": int(canonical["fixture_id"]),
        "provider_fixture_id": int(api_item["_api_fixture_id"]),
        "home_total_shots": stat_value("home", "total_shots", "home_total_shots"),
        "away_total_shots": stat_value("away", "total_shots", "away_total_shots"),
        "home_shots_on_target": stat_value("home", "shots_on_target", "home_shots_on_target"),
        "away_shots_on_target": stat_value("away", "shots_on_target", "away_shots_on_target"),
        "home_fouls": stat_value("home", "fouls", "home_fouls"),
        "away_fouls": stat_value("away", "fouls", "away_fouls"),
        "home_yellow_cards": stat_value("home", "yellow_cards", "home_yellow_cards"),
        "away_yellow_cards": stat_value("away", "yellow_cards", "away_yellow_cards"),
        "home_red_cards": stat_value("home", "red_cards", "home_red_cards"),
        "away_red_cards": stat_value("away", "red_cards", "away_red_cards"),
        "home_offsides": stat_value("home", "offsides", "home_offsides"),
        "away_offsides": stat_value("away", "offsides", "away_offsides"),
        "home_corners": stat_value("home", "corners", "home_corners"),
        "away_corners": stat_value("away", "corners", "away_corners"),
        "goal_scorers_json": json.dumps(scorers, ensure_ascii=False) if scorers else None,
        "score_verified": True,
        "source": SOURCE_NAME,
        "collected_at": utc_now_iso(),
    }
    pairs = available_stat_pairs(record)
    expected_goals = int(canonical["home_goals"]) + int(canonical["away_goals"])
    scorer_complete = expected_goals == 0 or len(scorers) >= expected_goals
    record["available_stat_pairs"] = pairs
    if pairs >= 5 and scorer_complete:
        record["data_quality"] = "complete"
    elif pairs > 0 or scorers:
        record["data_quality"] = "partial"
    else:
        record["data_quality"] = "score_only"
    return record


def enrich_history(
    *,
    seasons: Iterable[int],
    api_key: str | None,
    session: requests.Session | None = None,
    recent_days: int | None = None,
    max_detail_batches: int | None = None,
    scorers_only: bool = False,
    date_fallback_days: int | None = 45,
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
    rows = [
        row
        for row in _canonical_completed_rows(requested)
        if _needs_enrichment(row, scorers_only=scorers_only)
    ]
    if recent_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(recent_days)))
        rows = [
            row for row in rows
            if _safe_recent(str(row.get("fixture_date") or ""), cutoff)
        ]
    rows.sort(key=lambda row: str(row.get("fixture_date") or ""), reverse=True)

    max_matches = MAX_MATCHES_PER_RUN
    if max_detail_batches is not None:
        max_matches = min(max_matches, max(0, int(max_detail_batches)) * 20)
    rows = rows[:max_matches]

    if not rows:
        return EnrichmentResult(
            enabled=True,
            seasons=requested,
            completed_matches_considered=0,
            api_matches_found=0,
            matches_enriched=0,
            requests_used=0,
            quota_remaining=None,
            warnings=[],
            score_mismatches=0,
            pending_matches=0,
        )

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"x-apisports-key": cleaned_key, "User-Agent": "BetAnalytic/1.0"})
    requests_used = 0
    quota_remaining: int | None = None
    warnings: list[str] = []
    api_items: list[dict[str, Any]] = []
    score_mismatches = 0

    def remember_quota(remaining: int | None) -> None:
        nonlocal quota_remaining
        if remaining is not None:
            quota_remaining = remaining

    try:
        # Fast path: one league/season request. On some free-plan season
        # windows this can legitimately return no rows for the newest season.
        for season in requested:
            try:
                payload, remaining = _request_json(
                    http,
                    "/fixtures",
                    params={"league": LEAGUE_ID, "season": season, "timezone": "Europe/Athens"},
                )
                requests_used += 1
                remember_quota(remaining)
                response = payload.get("response")
                if isinstance(response, list):
                    api_items.extend(item for item in response if isinstance(item, dict))
            except (requests.RequestException, ValueError) as error:
                warnings.append(f"Αποτυχία λίστας API-Football για {season}: {error}")

        # Free-plan fallback for *current/recent* matches: if the season query
        # did not expose them, ask by exact local match date without a season
        # parameter. This is intentionally limited to a few dates so the 100/day
        # free quota is not consumed by old history.
        index = _api_match_index(api_items)
        missing_rows = [row for row in rows if _match_api_fixture(row, index) is None]
        fallback_dates: list[str] = []
        for row in missing_rows:
            if not _within_date_fallback_window(row, date_fallback_days):
                continue
            local_date = _local_date(str(row.get("fixture_date") or ""))
            if local_date and local_date not in fallback_dates:
                fallback_dates.append(local_date)
        fallback_dates.sort(reverse=True)

        for local_date in fallback_dates[:MAX_DATE_FALLBACK_REQUESTS]:
            if quota_remaining is not None and quota_remaining <= 3:
                warnings.append(
                    "Το ημερήσιο όριο API πλησιάζει στο τέλος· "
                    "σταμάτησε το date fallback για να μείνουν requests για events."
                )
                break
            try:
                payload, remaining = _request_json(
                    http,
                    "/fixtures",
                    params={
                        "league": LEAGUE_ID,
                        "date": local_date,
                        "timezone": "Europe/Athens",
                    },
                )
                requests_used += 1
                remember_quota(remaining)
                response = payload.get("response")
                if isinstance(response, list):
                    api_items.extend(item for item in response if isinstance(item, dict))
            except (requests.RequestException, ValueError) as error:
                warnings.append(
                    f"Αποτυχία API-Football date fallback για {local_date}: {error}"
                )

        index = _api_match_index(api_items)
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            matched = _match_api_fixture(row, index)
            if matched is None:
                continue
            if not _score_matches(row, matched):
                score_mismatches += 1
                warnings.append(
                    "Απόρριψη ασύμφωνου σκορ για "
                    f"{row['home_team_name']} - {row['away_team_name']} "
                    f"({row['fixture_date']})."
                )
                continue
            matches.append((row, matched))

        detail_records: list[dict[str, Any]] = []
        scorer_records: list[dict[str, Any]] = []
        for canonical, matched in matches:
            api_id = int(matched["_api_fixture_id"])
            need_scorers = not _scorers_complete(canonical)
            need_statistics = not _statistics_complete(canonical) and not scorers_only
            total_goals = int(canonical["home_goals"]) + int(canonical["away_goals"])

            events_response: Any | None = None
            statistics_response: Any | None = None
            fetched_any_detail = False

            # Scorers first. A statistics failure must never prevent the goal
            # names from reaching the Android history screen.
            if need_scorers and total_goals > 0:
                if quota_remaining is not None and quota_remaining <= 1:
                    warnings.append(
                        "Το ημερήσιο όριο API τελείωσε· οι υπόλοιποι σκόρερ μένουν pending."
                    )
                    break
                try:
                    events_payload, remaining = _request_json(
                        http,
                        "/fixtures/events",
                        params={"fixture": api_id, "type": "goal"},
                    )
                    requests_used += 1
                    remember_quota(remaining)
                    events_response = events_payload.get("response")
                    fetched_any_detail = True
                    if not _goal_scorers(events_response):
                        warnings.append(
                            "Το API-Football δεν επέστρεψε ονόματα σκόρερ για "
                            f"{canonical['home_team_name']} - {canonical['away_team_name']} "
                            f"(fixture={api_id})."
                        )
                except (requests.RequestException, ValueError) as error:
                    warnings.append(
                        f"Αποτυχία events API-Football για fixture={api_id}: {error}"
                    )

            if need_statistics:
                if quota_remaining is None or quota_remaining > 1:
                    try:
                        stats_payload, remaining = _request_json(
                            http,
                            "/fixtures/statistics",
                            params={"fixture": api_id},
                        )
                        requests_used += 1
                        remember_quota(remaining)
                        statistics_response = stats_payload.get("response")
                        fetched_any_detail = True
                    except (requests.RequestException, ValueError) as error:
                        warnings.append(
                            f"Αποτυχία statistics API-Football για fixture={api_id}: {error}"
                        )

            if fetched_any_detail:
                detail = _detail_record(
                    canonical,
                    matched,
                    statistics_response,
                    events_response,
                )
                scorer_json = detail.get("goal_scorers_json")
                parsed_scorers: list[dict[str, Any]] = []
                if scorer_json:
                    try:
                        candidate = json.loads(str(scorer_json))
                        if isinstance(candidate, list):
                            parsed_scorers = [item for item in candidate if isinstance(item, dict)]
                    except json.JSONDecodeError:
                        parsed_scorers = []
                expected_goals = int(canonical["home_goals"]) + int(canonical["away_goals"])
                scorer_complete = (
                    expected_goals == 0
                    or (
                        len(parsed_scorers) == expected_goals
                        and sum(1 for item in parsed_scorers if item.get("side") == "home")
                            == int(canonical["home_goals"])
                        and sum(1 for item in parsed_scorers if item.get("side") == "away")
                            == int(canonical["away_goals"])
                    )
                )
                if not scorers_only or scorer_complete:
                    detail_records.append(detail)
                if scorer_complete and expected_goals > 0:
                    scorer_records.append(
                        {
                            "fixture_id": int(canonical["fixture_id"]),
                            "goal_scorers_json": json.dumps(parsed_scorers, ensure_ascii=False),
                            "source": SOURCE_NAME,
                            "provider_event_id": str(api_id),
                            "score_verified": True,
                            "collected_at": utc_now_iso(),
                        }
                    )

        history_saved = save_fixture_history_details(detail_records)
        scorers_saved = save_fixture_goal_scorers(scorer_records)
        enriched = scorers_saved if scorers_only else max(history_saved, scorers_saved)
        return EnrichmentResult(
            enabled=True,
            seasons=requested,
            completed_matches_considered=len(rows),
            api_matches_found=len(matches),
            matches_enriched=enriched,
            requests_used=requests_used,
            quota_remaining=quota_remaining,
            warnings=warnings,
            score_mismatches=score_mismatches,
            pending_matches=max(0, len(rows) - scorers_saved - score_mismatches)
                if scorers_only
                else max(0, len(rows) - enriched - score_mismatches),
        )
    finally:
        if own_session:
            http.close()


def _safe_recent(value: str, cutoff: datetime) -> bool:
    try:
        return parse_iso_datetime(value) >= cutoff
    except ValueError:
        return False


def _parse_seasons(value: str) -> list[int]:
    cleaned = str(value or "").strip().lower()
    if not cleaned or cleaned == "auto":
        current = season_from_local_date(datetime.now(ATHENS_TZ).date())
        return [current - 1, current]
    return sorted({int(item.strip()) for item in value.split(",") if item.strip()})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Συμπληρώνει ενιαία snapshots API-Football για ιστορικά στατιστικά και σκόρερ."
    )
    parser.add_argument("--seasons", default="auto")
    parser.add_argument("--summary-output", default="data/history_enrichment_summary.json")
    parser.add_argument("--scorers-only", action="store_true")
    parser.add_argument("--recent-days", type=int, default=None)
    parser.add_argument("--date-fallback-days", type=int, default=45)
    args = parser.parse_args()
    result = enrich_history(
        seasons=_parse_seasons(args.seasons),
        api_key=os.getenv("API_FOOTBALL_KEY"),
        recent_days=args.recent_days,
        scorers_only=args.scorers_only,
        date_fallback_days=args.date_fallback_days,
    )
    payload = {
        "enabled": result.enabled,
        "seasons": result.seasons,
        "completed_matches_considered": result.completed_matches_considered,
        "api_matches_found": result.api_matches_found,
        "matches_enriched": result.matches_enriched,
        "requests_used": result.requests_used,
        "quota_remaining": result.quota_remaining,
        "score_mismatches": result.score_mismatches,
        "pending_matches": result.pending_matches,
        "warnings": result.warnings,
        "finished_at": utc_now_iso(),
    }
    path = Path(args.summary_output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
