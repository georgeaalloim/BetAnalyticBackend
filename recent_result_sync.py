from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from database import get_connection, initialize_database, save_fixture_goal_scorers
from fixtur_es_source import LEAGUE_ID, resolve_team
from goal_scorer_enricher import (
    FREE_KEY,
    SOURCE_NAME as THESPORTSDB_SOURCE_NAME,
    _parse_minute,
    _request_json,
    _timeline_items,
)
from thesportsdb_recent import discover_events, event_matches_fixture
from time_utils import parse_iso_datetime, to_iso_z, utc_now

ATHENS_TZ = ZoneInfo("Europe/Athens")
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
FINISHED_STATUSES = frozenset({"FT", "AET", "AP", "PEN", "AW", "MATCH FINISHED", "FINISHED"})


@dataclass
class RecentResultSyncResult:
    checked: int
    updated: int
    thesportsdb_matches: int
    api_football_matches: int
    scorer_sets_saved: int
    requests_used: int
    updated_fixture_ids: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _fixture_rows(*, as_of: datetime, recent_hours: float) -> list[dict[str, Any]]:
    lower = as_of - timedelta(hours=max(4.0, float(recent_hours)))
    upper = as_of + timedelta(minutes=30)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM fixtures
            WHERE league_id = ?
              AND status <> 'FT'
              AND fixture_date IS NOT NULL
            ORDER BY fixture_date ASC, fixture_id ASC
            """,
            (LEAGUE_ID,),
        ).fetchall()

    selected: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        try:
            kickoff = parse_iso_datetime(str(row.get("fixture_date") or ""))
        except ValueError:
            continue
        if lower <= kickoff <= upper:
            selected.append(row)
    return selected


def _event_is_finished(event: dict[str, Any]) -> bool:
    status = str(event.get("strStatus") or "").upper().strip()
    progress = str(event.get("strProgress") or "").upper().strip()
    if status in FINISHED_STATUSES or progress in FINISHED_STATUSES:
        return True
    combined = f"{status} {progress}"
    return "FULL TIME" in combined or "MATCH FINISHED" in combined


def _event_score(event: dict[str, Any]) -> tuple[int, int] | None:
    home = _as_int(event.get("intHomeScore"))
    away = _as_int(event.get("intAwayScore"))
    if home is None or away is None:
        return None
    return home, away


def _update_fixture_result(
    fixture_id: int,
    *,
    home_goals: int,
    away_goals: int,
) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE fixtures
            SET status = 'FT',
                home_goals = ?,
                away_goals = ?
            WHERE fixture_id = ?
              AND (
                    status <> 'FT'
                 OR home_goals IS NULL
                 OR away_goals IS NULL
                 OR home_goals <> ?
                 OR away_goals <> ?
              )
            """,
            (
                int(home_goals),
                int(away_goals),
                int(fixture_id),
                int(home_goals),
                int(away_goals),
            ),
        )
        return cursor.rowcount > 0


def _timeline_scorers(
    timeline_items: list[dict[str, Any]],
    fixture: dict[str, Any],
) -> list[dict[str, Any]]:
    home_id = int(fixture["home_team_id"])
    away_id = int(fixture["away_team_id"])
    scorers: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for item in timeline_items:
        kind = str(item.get("strTimeline") or item.get("strType") or "").strip()
        detail_text = " ".join(
            str(item.get(field) or "")
            for field in ("strTimelineDetail", "strDetail", "strComment")
        ).strip()
        combined = f"{kind} {detail_text}".casefold()
        if "goal" not in combined:
            continue
        if any(
            token in combined
            for token in ("missed", "disallowed", "cancelled", "canceled", "no goal")
        ):
            continue

        player = str(item.get("strPlayer") or item.get("strPlayerName") or "").strip()
        if not player:
            continue

        side: str | None = None
        home_flag = str(item.get("strHome") or "").strip().casefold()
        if home_flag in {"yes", "true", "1", "home"}:
            side = "home"
        elif home_flag in {"no", "false", "0", "away"}:
            side = "away"
        else:
            team_name = str(item.get("strTeam") or "").strip()
            if team_name:
                team_id, _ = resolve_team(team_name)
                if int(team_id) == home_id:
                    side = "home"
                elif int(team_id) == away_id:
                    side = "away"
        if side is None:
            continue

        minute, extra = _parse_minute(
            item.get("intTime") or item.get("strTime") or item.get("strTimelineTime")
        )
        own_goal = "own goal" in combined or "own-goal" in combined or "own_goal" in combined
        penalty = "penalty" in combined or "pen." in combined
        detail = "Own Goal" if own_goal else ("Penalty" if penalty else (kind or "Goal"))

        entry = {
            "player_name": player,
            "side": side,
            "team_id": home_id if side == "home" else away_id,
            "team_name": (
                str(fixture["home_team_name"])
                if side == "home"
                else str(fixture["away_team_name"])
            ),
            "minute": minute,
            "extra_minute": extra,
            "detail": detail,
        }
        key = (
            player.casefold(),
            side,
            minute,
            extra,
            detail.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        scorers.append(entry)

    scorers.sort(
        key=lambda item: (
            item.get("minute") if item.get("minute") is not None else 999,
            item.get("extra_minute") or 0,
        )
    )
    return scorers


def _try_save_scorers(
    fixture: dict[str, Any],
    event: dict[str, Any],
    *,
    api_key: str,
    session: requests.Session,
    collected_at: str,
) -> tuple[int, int, str | None]:
    score = _event_score(event)
    event_id = str(event.get("idEvent") or "").strip()
    if score is None or not event_id:
        return 0, 0, None

    home_goals, away_goals = score
    try:
        payload = _request_json(
            session,
            api_key,
            "lookuptimeline.php",
            {"id": event_id},
        )
        items = _timeline_items(payload)
    except (requests.RequestException, ValueError) as exc:
        return 0, 1, str(exc)

    scorers = _timeline_scorers(items, fixture)
    # Never publish a partial scorer list. The database repeats this check too.
    if (
        len(scorers) != home_goals + away_goals
        or sum(item["side"] == "home" for item in scorers) != home_goals
        or sum(item["side"] == "away" for item in scorers) != away_goals
    ):
        return 0, 1, None

    saved = save_fixture_goal_scorers(
        [
            {
                "fixture_id": int(fixture["fixture_id"]),
                "goal_scorers_json": scorers,
                "source": f"{THESPORTSDB_SOURCE_NAME} recent result sync",
                "provider_event_id": event_id,
                "score_verified": True,
                "collected_at": collected_at,
            }
        ]
    )
    return saved, 1, None


def _api_football_items_for_date(
    session: requests.Session,
    api_key: str,
    local_date: str,
) -> tuple[list[dict[str, Any]], int, str | None]:
    try:
        response = session.get(
            f"{API_FOOTBALL_BASE_URL}/fixtures",
            params={
                "league": LEAGUE_ID,
                "date": local_date,
                "timezone": "Europe/Athens",
            },
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("API-Football returned non-object JSON.")
        if payload.get("errors"):
            raise ValueError(f"API-Football errors: {payload['errors']}")
        items = payload.get("response")
        return (
            [item for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else [],
            1,
            None,
        )
    except (requests.RequestException, ValueError) as exc:
        return [], 1, str(exc)


def _api_item_matches_fixture(
    fixture: dict[str, Any],
    item: dict[str, Any],
) -> bool:
    teams = item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    home_id, _ = resolve_team(str(home.get("name") or ""))
    away_id, _ = resolve_team(str(away.get("name") or ""))
    if (
        int(home_id) != int(fixture["home_team_id"])
        or int(away_id) != int(fixture["away_team_id"])
    ):
        return False

    api_fixture = item.get("fixture") or {}
    raw_date = str(api_fixture.get("date") or "")
    try:
        api_date = parse_iso_datetime(raw_date).astimezone(ATHENS_TZ).date().isoformat()
        canonical_date = parse_iso_datetime(
            str(fixture.get("fixture_date") or "")
        ).astimezone(ATHENS_TZ).date().isoformat()
    except ValueError:
        return False
    return api_date == canonical_date


def _api_item_finished_score(item: dict[str, Any]) -> tuple[int, int] | None:
    api_fixture = item.get("fixture") or {}
    status = str((api_fixture.get("status") or {}).get("short") or "").upper().strip()
    if status not in {"FT", "AET", "PEN", "AWD", "WO"}:
        return None
    goals = item.get("goals") or {}
    home = _as_int(goals.get("home"))
    away = _as_int(goals.get("away"))
    if home is None or away is None:
        return None
    return home, away


def sync_recent_results(
    *,
    as_of: datetime | None = None,
    recent_hours: float = 18.0,
    thesportsdb_key: str | None = None,
    api_football_key: str | None = None,
) -> RecentResultSyncResult:
    initialize_database()
    current = as_of or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    rows = _fixture_rows(as_of=current, recent_hours=recent_hours)
    if not rows:
        return RecentResultSyncResult(0, 0, 0, 0, 0, 0)

    tdb_key = str(thesportsdb_key or os.getenv("THESPORTSDB_KEY") or FREE_KEY).strip()
    api_key = str(api_football_key or os.getenv("API_FOOTBALL_KEY") or "").strip()

    events, requests_used, warnings = discover_events(
        rows,
        api_key=tdb_key,
        max_searches_per_fixture=6,
    )

    updated_ids: list[int] = []
    matched_tdb = 0
    matched_api = 0
    scorer_sets_saved = 0
    resolved_ids: set[int] = set()
    collected_at = to_iso_z(current)

    scorer_http = requests.Session()
    scorer_http.headers.update(
        {"User-Agent": "BetAnalytic/1.2 (+free recent result/scorer sync)"}
    )
    try:
        for row in rows:
            fixture_id = int(row["fixture_id"])
            event = events.get(fixture_id)
            if event is None or not event_matches_fixture(row, event):
                continue
            if not _event_is_finished(event):
                continue
            score = _event_score(event)
            if score is None:
                continue

            matched_tdb += 1
            resolved_ids.add(fixture_id)
            if _update_fixture_result(
                fixture_id,
                home_goals=score[0],
                away_goals=score[1],
            ):
                updated_ids.append(fixture_id)

            saved, used, error = _try_save_scorers(
                row,
                event,
                api_key=tdb_key,
                session=scorer_http,
                collected_at=collected_at,
            )
            requests_used += used
            scorer_sets_saved += saved
            if error:
                warnings.append(f"Timeline {event.get('idEvent')} απέτυχε: {error}")
    finally:
        scorer_http.close()

    # Optional current-date fallback. It is only used for fixtures old enough
    # to plausibly have finished and that TheSportsDB did not confirm as final.
    unresolved = []
    for row in rows:
        fixture_id = int(row["fixture_id"])
        if fixture_id in resolved_ids:
            continue
        try:
            kickoff = parse_iso_datetime(str(row.get("fixture_date") or ""))
        except ValueError:
            continue
        if current - kickoff >= timedelta(minutes=75):
            unresolved.append(row)

    if unresolved and api_key:
        api_http = requests.Session()
        api_http.headers.update(
            {
                "x-apisports-key": api_key,
                "User-Agent": "BetAnalytic/1.2 (+recent result fallback)",
            }
        )
        try:
            by_date: dict[str, list[dict[str, Any]]] = {}
            for row in unresolved:
                local_date = parse_iso_datetime(
                    str(row["fixture_date"])
                ).astimezone(ATHENS_TZ).date().isoformat()
                by_date.setdefault(local_date, []).append(row)

            for local_date, date_rows in by_date.items():
                items, used, error = _api_football_items_for_date(
                    api_http, api_key, local_date
                )
                requests_used += used
                if error:
                    warnings.append(
                        f"API-Football date fallback {local_date} απέτυχε: {error}"
                    )
                    continue

                for row in date_rows:
                    for item in items:
                        if not _api_item_matches_fixture(row, item):
                            continue
                        score = _api_item_finished_score(item)
                        if score is None:
                            continue
                        matched_api += 1
                        fixture_id = int(row["fixture_id"])
                        resolved_ids.add(fixture_id)
                        if _update_fixture_result(
                            fixture_id,
                            home_goals=score[0],
                            away_goals=score[1],
                        ):
                            updated_ids.append(fixture_id)
                        break
        finally:
            api_http.close()

    return RecentResultSyncResult(
        checked=len(rows),
        updated=len(set(updated_ids)),
        thesportsdb_matches=matched_tdb,
        api_football_matches=matched_api,
        scorer_sets_saved=scorer_sets_saved,
        requests_used=requests_used,
        updated_fixture_ids=sorted(set(updated_ids)),
        warnings=warnings,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize very recent FT scores before feed generation."
    )
    parser.add_argument("--recent-hours", type=float, default=18.0)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--summary-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    as_of = parse_iso_datetime(args.as_of) if args.as_of else utc_now()
    result = sync_recent_results(
        as_of=as_of,
        recent_hours=args.recent_hours,
    )
    payload = asdict(result)
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    print(output)

    if args.summary_output is not None:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
