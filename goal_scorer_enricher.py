from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Any
from zoneinfo import ZoneInfo

import requests

from database import (
    get_connection,
    initialize_database,
    save_fixture_goal_scorers,
)
from time_utils import parse_iso_datetime, to_iso_z, utc_now


SOURCE_NAME = "TheSportsDB API v1"
BASE_URL = "https://www.thesportsdb.com/api/v1/json"
FREE_KEY = "123"
LEAGUE_ID = 197
THESPORTSDB_LEAGUE_ID = "4336"
ATHENS_TZ = ZoneInfo("Europe/Athens")
DEFAULT_RECENT_DAYS = 60
# 8 matches x (up to 2 event searches + 1 timeline) = at most 24 requests/run,
# below TheSportsDB's documented 30 requests/minute free-tier limit.
DEFAULT_MAX_MATCHES_PER_RUN = 8


TEAM_ALIASES: dict[int, tuple[str, ...]] = {
    575: ("AEK Athens", "AEK"),
    1123: ("Aris Thessaloniki", "Aris"),
    955: ("Asteras Tripolis", "Asteras Aktor"),
    12260: ("Atromitos", "Atromitos Athens"),
    1026357653: ("Iraklis", "POT Iraklis"),
    1068316644: ("Kalamata",),
    957: ("Levadiakos", "Levadeiakos"),
    1124: ("OFI Crete", "OFI"),
    553: ("Olympiacos", "Olympiakos"),
    619: ("PAOK", "PAOK Thessaloniki"),
    949: ("Panetolikos",),
    2110: ("Volos NFC", "Volos"),
}


@dataclass
class GoalScorerEnrichmentResult:
    enabled: bool
    completed_matches_considered: int
    matches_searched: int
    events_found: int
    matches_saved: int
    requests_used: int
    pending_matches: int
    warnings: list[str] = field(default_factory=list)


def _normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"\b(fc|nfc|cf|ac|f\.c\.)\b", " ", text)
    return re.sub(r"[^a-z0-9α-ω]+", "", text)


def _name_candidates(team_id: int, canonical_name: str) -> tuple[str, ...]:
    raw = [canonical_name, *TEAM_ALIASES.get(int(team_id), ())]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = str(item or "").strip()
        key = _normalize_name(cleaned)
        if cleaned and key and key not in seen:
            out.append(cleaned)
            seen.add(key)
    return tuple(out)


def _names_match(api_name: Any, team_id: int, canonical_name: str) -> bool:
    api_norm = _normalize_name(api_name)
    if not api_norm:
        return False
    for candidate in _name_candidates(team_id, canonical_name):
        cand = _normalize_name(candidate)
        if cand and (api_norm == cand or api_norm in cand or cand in api_norm):
            return True
    return False


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _fixture_local_date(row: dict[str, Any]) -> str | None:
    try:
        dt = parse_iso_datetime(str(row.get("fixture_date") or ""))
    except ValueError:
        return None
    return dt.astimezone(ATHENS_TZ).date().isoformat()


def _pending_rows(
    *,
    season: int | None,
    recent_days: int,
    max_matches: int,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(recent_days)))
    query = """
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
            g.fixture_id AS scorer_fixture_id,
            g.score_verified AS scorer_score_verified
        FROM fixtures AS f
        LEFT JOIN fixture_goal_scorers AS g
          ON g.fixture_id = f.fixture_id
        WHERE f.league_id = ?
          AND f.status = 'FT'
          AND f.home_goals IS NOT NULL
          AND f.away_goals IS NOT NULL
          AND (f.home_goals + f.away_goals) > 0
          AND (g.fixture_id IS NULL OR g.score_verified <> 1)
    """
    params: list[Any] = [LEAGUE_ID]
    if season is not None:
        query += " AND f.season = ?"
        params.append(int(season))
    query += " ORDER BY f.fixture_date DESC, f.fixture_id DESC"

    with get_connection() as connection:
        rows = [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]

    recent: list[dict[str, Any]] = []
    for row in rows:
        try:
            if parse_iso_datetime(str(row.get("fixture_date") or "")) < cutoff:
                continue
        except ValueError:
            continue
        recent.append(row)
        if len(recent) >= max(0, int(max_matches)):
            break
    return recent


def _request_json(
    session: requests.Session,
    key: str,
    endpoint: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = session.get(
        f"{BASE_URL}/{key}/{endpoint}",
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("TheSportsDB επέστρεψε μη αναμενόμενο JSON.")
    return payload


def _event_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("event", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _timeline_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("timeline", "timelines", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _candidate_matches(row: dict[str, Any], event: dict[str, Any], local_date: str) -> bool:
    date_value = str(event.get("dateEvent") or "").strip()
    if date_value and date_value != local_date:
        return False
    league_value = str(event.get("idLeague") or "").strip()
    if league_value and league_value != THESPORTSDB_LEAGUE_ID:
        return False

    if not _names_match(event.get("strHomeTeam"), int(row["home_team_id"]), str(row["home_team_name"])):
        return False
    if not _names_match(event.get("strAwayTeam"), int(row["away_team_id"]), str(row["away_team_name"])):
        return False

    api_home = _as_int(event.get("intHomeScore"))
    api_away = _as_int(event.get("intAwayScore"))
    if api_home is not None and api_away is not None:
        if api_home != int(row["home_goals"]) or api_away != int(row["away_goals"]):
            return False
    return bool(event.get("idEvent"))


def _search_event(
    session: requests.Session,
    key: str,
    row: dict[str, Any],
) -> tuple[dict[str, Any] | None, int, list[str]]:
    local_date = _fixture_local_date(row)
    if local_date is None:
        return None, 0, [f"Άκυρη ημερομηνία fixture={row['fixture_id']}."]

    home_names = _name_candidates(int(row["home_team_id"]), str(row["home_team_name"]))
    away_names = _name_candidates(int(row["away_team_id"]), str(row["away_team_name"]))
    # At most two title variants, so the free-tier request budget stays bounded.
    variants: list[tuple[str, str]] = []
    for home, away in product(home_names[:2], away_names[:2]):
        pair = (home, away)
        if pair not in variants:
            variants.append(pair)
        if len(variants) >= 2:
            break

    used = 0
    warnings: list[str] = []
    for home, away in variants:
        title = f"{home}_vs_{away}".replace(" ", "_")
        try:
            payload = _request_json(
                session,
                key,
                "searchevents.php",
                {"e": title, "d": local_date},
            )
            used += 1
        except (requests.RequestException, ValueError) as exc:
            used += 1
            warnings.append(
                f"TheSportsDB event search απέτυχε για {row['home_team_name']} - "
                f"{row['away_team_name']}: {exc}"
            )
            continue
        for event in _event_items(payload):
            if _candidate_matches(row, event, local_date):
                return event, used, warnings
    return None, used, warnings


def _parse_minute(value: Any) -> tuple[int | None, int | None]:
    text = str(value or "").strip().replace("’", "").replace("'", "")
    if not text:
        return None, None
    match = re.search(r"(\d{1,3})(?:\s*\+\s*(\d{1,2}))?", text)
    if not match:
        return None, None
    minute = int(match.group(1))
    extra = int(match.group(2)) if match.group(2) else None
    return minute, extra


def _side_from_timeline(item: dict[str, Any], row: dict[str, Any]) -> str | None:
    home_flag = str(item.get("strHome") or "").strip().casefold()
    if home_flag in {"yes", "true", "1", "home"}:
        return "home"
    if home_flag in {"no", "false", "0", "away"}:
        return "away"
    team_name = item.get("strTeam")
    if _names_match(team_name, int(row["home_team_id"]), str(row["home_team_name"])):
        return "home"
    if _names_match(team_name, int(row["away_team_id"]), str(row["away_team_name"])):
        return "away"
    return None


def _parse_goal_timeline(items: list[dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        event_type = str(item.get("strTimeline") or item.get("strType") or "").strip()
        detail_text = " ".join(
            str(item.get(field) or "")
            for field in ("strTimelineDetail", "strDetail", "strComment")
        ).strip()
        combined = f"{event_type} {detail_text}".casefold()
        if "goal" not in event_type.casefold() and "goal" not in combined:
            continue
        if any(token in combined for token in ("missed", "no goal", "cancelled", "canceled", "disallowed")):
            continue

        player = str(item.get("strPlayer") or item.get("strPlayerName") or "").strip()
        side = _side_from_timeline(item, row)
        if not player or side not in {"home", "away"}:
            continue
        minute, extra = _parse_minute(
            item.get("intTime") or item.get("strTime") or item.get("strTimelineTime")
        )
        own_goal = "own goal" in combined or "own-goal" in combined or "own_goal" in combined
        penalty = "penalty" in combined or "pen." in combined
        detail = "Own Goal" if own_goal else ("Penalty" if penalty else "Goal")
        goal = {
            "player_name": player,
            "side": side,
            "team_id": int(row["home_team_id"] if side == "home" else row["away_team_id"]),
            "team_name": str(row["home_team_name"] if side == "home" else row["away_team_name"]),
            "minute": minute,
            "extra_minute": extra,
            "detail": detail,
        }
        key = (player.casefold(), side, minute, extra, detail)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(goal)
    return parsed


def _counts_match(scorers: list[dict[str, Any]], row: dict[str, Any]) -> bool:
    expected_home = int(row["home_goals"])
    expected_away = int(row["away_goals"])
    return (
        len(scorers) == expected_home + expected_away
        and sum(1 for item in scorers if item.get("side") == "home") == expected_home
        and sum(1 for item in scorers if item.get("side") == "away") == expected_away
    )


def _repair_own_goal_sides(scorers: list[dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    if _counts_match(scorers, row):
        return scorers
    own_indexes = [
        index for index, scorer in enumerate(scorers)
        if str(scorer.get("detail") or "").casefold() == "own goal"
    ]
    # TheSportsDB timelines can attribute an own-goal event to the player's
    # team rather than the side receiving the score. Try the finite set of
    # own-goal side inversions and accept only the one matching the final score.
    for flips in product((False, True), repeat=len(own_indexes)):
        candidate = [dict(item) for item in scorers]
        for index, should_flip in zip(own_indexes, flips):
            if not should_flip:
                continue
            side = "away" if candidate[index]["side"] == "home" else "home"
            candidate[index]["side"] = side
            candidate[index]["team_id"] = int(
                row["home_team_id"] if side == "home" else row["away_team_id"]
            )
            candidate[index]["team_name"] = str(
                row["home_team_name"] if side == "home" else row["away_team_name"]
            )
        if _counts_match(candidate, row):
            return candidate
    return scorers


def enrich_goal_scorers(
    *,
    season: int | None = None,
    api_key: str | None = None,
    recent_days: int = DEFAULT_RECENT_DAYS,
    max_matches: int = DEFAULT_MAX_MATCHES_PER_RUN,
    session: requests.Session | None = None,
) -> GoalScorerEnrichmentResult:
    """
    Automatically enriches finished matches with scorer names/minutes.

    Missing or incomplete provider data is never persisted. The same match is
    retried automatically on the next backend run until a complete score-verified
    timeline becomes available.
    """
    initialize_database()
    key = str(api_key or os.getenv("THESPORTSDB_KEY") or FREE_KEY).strip()
    if not key:
        return GoalScorerEnrichmentResult(False, 0, 0, 0, 0, 0, 0, ["TheSportsDB key is empty."])

    rows = _pending_rows(season=season, recent_days=recent_days, max_matches=max_matches)
    if not rows:
        return GoalScorerEnrichmentResult(True, 0, 0, 0, 0, 0, 0, [])

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"User-Agent": "BetAnalytic/1.0 (+automatic scorer enrichment)"})

    requests_used = 0
    events_found = 0
    saved = 0
    warnings: list[str] = []
    try:
        for row in rows:
            event, used, search_warnings = _search_event(http, key, row)
            requests_used += used
            warnings.extend(search_warnings)
            if event is None:
                warnings.append(
                    f"Pending scorers: δεν βρέθηκε event για {row['home_team_name']} - "
                    f"{row['away_team_name']} ({_fixture_local_date(row)})."
                )
                continue
            events_found += 1
            event_id = str(event.get("idEvent") or "").strip()
            try:
                timeline_payload = _request_json(
                    http,
                    key,
                    "lookuptimeline.php",
                    {"id": event_id},
                )
                requests_used += 1
            except (requests.RequestException, ValueError) as exc:
                requests_used += 1
                warnings.append(f"Pending scorers: timeline {event_id} απέτυχε: {exc}")
                continue

            scorers = _parse_goal_timeline(_timeline_items(timeline_payload), row)
            scorers = _repair_own_goal_sides(scorers, row)
            if not _counts_match(scorers, row):
                warnings.append(
                    "Pending scorers: το timeline είναι ελλιπές/ασύμφωνο για "
                    f"{row['home_team_name']} - {row['away_team_name']} "
                    f"(βρέθηκαν {len(scorers)}, αναμένονται "
                    f"{int(row['home_goals']) + int(row['away_goals'])})."
                )
                continue

            saved += save_fixture_goal_scorers([
                {
                    "fixture_id": int(row["fixture_id"]),
                    "goal_scorers_json": json.dumps(scorers, ensure_ascii=False),
                    "source": SOURCE_NAME,
                    "provider_event_id": event_id,
                    "score_verified": True,
                    "collected_at": to_iso_z(utc_now()),
                }
            ])
    finally:
        if own_session:
            http.close()

    return GoalScorerEnrichmentResult(
        enabled=True,
        completed_matches_considered=len(rows),
        matches_searched=len(rows),
        events_found=events_found,
        matches_saved=saved,
        requests_used=requests_used,
        pending_matches=max(0, len(rows) - saved),
        warnings=warnings,
    )
