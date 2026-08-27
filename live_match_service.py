from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from goal_scorer_enricher import (
    BASE_URL,
    FREE_KEY,
    SOURCE_NAME,
    _event_items,
    _name_candidates,
    _names_match,
    _parse_minute,
    _request_json,
    _timeline_items,
)
from time_utils import parse_iso_datetime, to_iso_z, utc_now

ATHENS_TZ = ZoneInfo("Europe/Athens")
THESPORTSDB_LEAGUE_ID = "4336"
ACTIVE_STATUSES = frozenset({"1H", "HT", "2H", "ET", "BT", "PT", "LIVE", "IN PLAY", "INPLAY"})
FINISHED_STATUSES = frozenset({"FT", "AET", "AP", "AW"})
INACTIVE_STATUSES = frozenset({"NS", "TBD", "PST", "POST", "CANC", "CANCELLED", "ABD", "INTR"})
MAX_LIVE_MATCHES = 6


@dataclass
class LiveRefreshResult:
    enabled: bool
    candidates_considered: int
    matches_live: int
    requests_used: int
    matches: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip().replace("%", "")))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None


def _candidate_datetime(candidate: dict[str, Any]) -> datetime | None:
    try:
        return parse_iso_datetime(str(candidate.get("fixture_date") or ""))
    except (TypeError, ValueError):
        return None


def select_live_candidates(
    feed: dict[str, Any],
    *,
    as_of: datetime,
    before_hours: float = 4.0,
    after_hours: float = 2.0,
    max_matches: int = MAX_LIVE_MATCHES,
) -> list[dict[str, Any]]:
    """Select only confirmed-kickoff fixtures near the current clock.

    The full feed publishes ``live_candidates`` specifically so a match is not
    lost from the live pipeline once its kickoff has passed and it disappears
    from the normal upcoming-fixtures list.
    """
    raw = feed.get("live_candidates")
    if not isinstance(raw, list):
        raw = [
            fixture
            for season in feed.get("seasons", [])
            if isinstance(season, dict)
            for fixture in season.get("fixtures", [])
            if isinstance(fixture, dict)
        ]

    lower = as_of - timedelta(hours=max(0.5, float(before_hours)))
    upper = as_of + timedelta(hours=max(0.5, float(after_hours)))
    selected: list[tuple[datetime, dict[str, Any]]] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        fixture_id = _as_int(item.get("fixture_id"))
        if fixture_id is None or fixture_id in seen:
            continue
        if item.get("kickoff_time_confirmed") is False:
            continue
        kickoff = _candidate_datetime(item)
        if kickoff is None or not (lower <= kickoff <= upper):
            continue
        status = str(item.get("status") or "").upper()
        if status in FINISHED_STATUSES or status in {"PST", "POST", "CANC", "CANCELLED", "ABD"}:
            continue
        selected.append((kickoff, item))
        seen.add(fixture_id)

    selected.sort(key=lambda pair: pair[0])
    return [item for _, item in selected[: max(0, int(max_matches))]]


def _team(candidate: dict[str, Any], side: str) -> tuple[int, str]:
    team = candidate.get(f"{side}_team")
    if not isinstance(team, dict):
        return -1, ""
    return _as_int(team.get("team_id")) or -1, str(team.get("team_name") or "").strip()


def _event_matches(candidate: dict[str, Any], event: dict[str, Any]) -> bool:
    home_id, home_name = _team(candidate, "home")
    away_id, away_name = _team(candidate, "away")
    if home_id <= 0 or away_id <= 0 or not home_name or not away_name:
        return False
    if not _names_match(event.get("strHomeTeam"), home_id, home_name):
        return False
    if not _names_match(event.get("strAwayTeam"), away_id, away_name):
        return False
    league_value = str(event.get("idLeague") or "").strip()
    if league_value and league_value != THESPORTSDB_LEAGUE_ID:
        return False
    kickoff = _candidate_datetime(candidate)
    api_date = str(event.get("dateEventLocal") or event.get("dateEvent") or "").strip()
    if kickoff is not None and api_date:
        local_date = kickoff.astimezone(ATHENS_TZ).date().isoformat()
        if api_date != local_date:
            return False
    return bool(event.get("idEvent"))


def _search_event(
    session: requests.Session,
    key: str,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any] | None, int, list[str]]:
    home_id, home_name = _team(candidate, "home")
    away_id, away_name = _team(candidate, "away")
    kickoff = _candidate_datetime(candidate)
    if home_id <= 0 or away_id <= 0 or kickoff is None:
        return None, 0, ["LIVE candidate χωρίς έγκυρα στοιχεία ομάδων/ώρας."]

    local_date = kickoff.astimezone(ATHENS_TZ).date().isoformat()
    home_candidates = _name_candidates(home_id, home_name)
    away_candidates = _name_candidates(away_id, away_name)
    pairs: list[tuple[str, str]] = []
    # Keep the free-tier budget bounded: at most two searches for a brand-new match.
    for home in home_candidates[:2]:
        for away in away_candidates[:2]:
            if (home, away) not in pairs:
                pairs.append((home, away))
            if len(pairs) >= 2:
                break
        if len(pairs) >= 2:
            break

    used = 0
    warnings: list[str] = []
    for home, away in pairs:
        try:
            payload = _request_json(
                session,
                key,
                "searchevents.php",
                {"e": f"{home}_vs_{away}".replace(" ", "_"), "d": local_date},
            )
            used += 1
        except (requests.RequestException, ValueError) as exc:
            used += 1
            warnings.append(f"LIVE event search απέτυχε για {home_name} - {away_name}: {exc}")
            continue
        for event in _event_items(payload):
            if _event_matches(candidate, event):
                return event, used, warnings
    return None, used, warnings


def _lookup_event(
    session: requests.Session,
    key: str,
    event_id: str,
) -> tuple[dict[str, Any] | None, int, str | None]:
    try:
        payload = _request_json(session, key, "lookupevent.php", {"id": event_id})
        events = _event_items(payload)
        return (events[0] if events else None), 1, None
    except (requests.RequestException, ValueError) as exc:
        return None, 1, str(exc)


def _progress_minute(event: dict[str, Any], timeline: list[dict[str, Any]], kickoff: datetime, as_of: datetime) -> tuple[int | None, bool]:
    progress = str(event.get("strProgress") or "").strip()
    minute, extra = _parse_minute(progress)
    if minute is not None:
        return min(130, minute + (extra or 0)), False

    timeline_minutes: list[int] = []
    for item in timeline:
        base, added = _parse_minute(
            item.get("intTime") or item.get("strTime") or item.get("strTimelineTime")
        )
        if base is not None:
            timeline_minutes.append(base + (added or 0))
    if timeline_minutes:
        return min(130, max(timeline_minutes)), False

    status = str(event.get("strStatus") or "").upper().strip()
    if status == "HT":
        return 45, False
    if status in FINISHED_STATUSES:
        return 90, False

    # Last-resort display/model fallback. It is explicitly marked estimated in
    # the JSON so the Android UI can avoid presenting it as provider truth.
    elapsed = int(max(0.0, (as_of - kickoff).total_seconds()) // 60)
    if 0 <= elapsed <= 130:
        return elapsed, True
    return None, True


def _timeline_side(item: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    home_id, home_name = _team(candidate, "home")
    away_id, away_name = _team(candidate, "away")
    home_flag = str(item.get("strHome") or "").strip().casefold()
    if home_flag in {"yes", "true", "1", "home"}:
        return "home"
    if home_flag in {"no", "false", "0", "away"}:
        return "away"
    team_name = item.get("strTeam")
    if _names_match(team_name, home_id, home_name):
        return "home"
    if _names_match(team_name, away_id, away_name):
        return "away"
    return None


def _parse_timeline(items: list[dict[str, Any]], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        event_type = str(item.get("strTimeline") or item.get("strType") or "").strip()
        detail_text = " ".join(
            str(item.get(field) or "")
            for field in ("strTimelineDetail", "strDetail", "strComment")
        ).strip()
        combined = f"{event_type} {detail_text}".casefold()
        kind: str | None = None
        if "goal" in combined and not any(token in combined for token in ("missed", "disallowed", "cancelled", "canceled", "no goal")):
            kind = "goal"
        elif "yellow" in combined:
            kind = "yellow_card"
        elif "red" in combined:
            kind = "red_card"
        if kind is None:
            continue
        player = str(item.get("strPlayer") or item.get("strPlayerName") or "").strip()
        side = _timeline_side(item, candidate)
        minute, extra = _parse_minute(
            item.get("intTime") or item.get("strTime") or item.get("strTimelineTime")
        )
        own_goal = kind == "goal" and ("own goal" in combined or "own-goal" in combined or "own_goal" in combined)
        penalty = kind == "goal" and ("penalty" in combined or "pen." in combined)
        detail = "Own Goal" if own_goal else ("Penalty" if penalty else event_type or kind)
        entry = {
            "type": kind,
            "player_name": player or None,
            "side": side,
            "minute": minute,
            "extra_minute": extra,
            "detail": detail,
        }
        key = (kind, (player or "").casefold(), side, minute, extra, detail.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    result.sort(key=lambda item: ((item.get("minute") or 999), (item.get("extra_minute") or 0)))
    return result


def _normalize_stat_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


STAT_ALIASES: dict[str, tuple[str, ...]] = {
    "corners": ("corners", "corner_kicks", "corner"),
    "shots": ("total_shots", "shots_total", "shots"),
    "shots_on_target": ("shots_on_goal", "shots_on_target", "on_target"),
    "possession": ("ball_possession", "possession"),
    "fouls": ("fouls", "fouls_committed"),
    "offsides": ("offsides", "offside"),
    "yellow_cards": ("yellow_cards", "yellow_card"),
    "red_cards": ("red_cards", "red_card"),
}


def _stat_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("eventstats", "eventStats", "statistics", "stats"):
        value = payload.get(key)
        if isinstance(value, list):
            # Some responses wrap the actual stats array inside one event object.
            if len(value) == 1 and isinstance(value[0], dict):
                nested = value[0].get("eventstats") or value[0].get("statistics")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            return [item for item in value if isinstance(item, dict)]
    return []


def _parse_statistics(payload: dict[str, Any]) -> dict[str, dict[str, float | int | None]]:
    parsed: dict[str, dict[str, float | int | None]] = {
        key: {"home": None, "away": None} for key in STAT_ALIASES
    }
    aliases = {
        alias: canonical
        for canonical, names in STAT_ALIASES.items()
        for alias in names
    }
    for item in _stat_items(payload):
        label = _normalize_stat_name(
            item.get("strStat") or item.get("strType") or item.get("name")
        )
        canonical = aliases.get(label)
        if canonical is None:
            for alias, target in aliases.items():
                if alias in label or label in alias:
                    canonical = target
                    break
        if canonical is None:
            continue
        home_raw = item.get("intHome") if "intHome" in item else item.get("home")
        away_raw = item.get("intAway") if "intAway" in item else item.get("away")
        if canonical == "possession":
            parsed[canonical] = {"home": _as_float(home_raw), "away": _as_float(away_raw)}
        else:
            parsed[canonical] = {"home": _as_int(home_raw), "away": _as_int(away_raw)}
    return parsed


def _poisson_pmf(lam: float, k: int) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _prediction_lambdas(candidate: dict[str, Any]) -> tuple[float, float]:
    prediction = candidate.get("prediction")
    if isinstance(prediction, dict):
        expected = prediction.get("expected_goals")
        if isinstance(expected, dict):
            home = _as_float(expected.get("home"))
            away = _as_float(expected.get("away"))
            if home is not None and away is not None and home >= 0 and away >= 0:
                return home, away
    return 1.35, 1.15


def _stat_pair(
    statistics: dict[str, dict[str, float | int | None]],
    key: str,
) -> tuple[float | None, float | None]:
    values = statistics.get(key)
    if not isinstance(values, dict):
        return None, None
    return _as_float(values.get("home")), _as_float(values.get("away"))


def _share_signal(home: float | None, away: float | None, smoothing: float) -> tuple[float, bool]:
    """Return a bounded -1..+1 home-vs-away signal.

    Smoothing stops tiny samples (for example 1 shot vs 0 in minute 3) from
    moving the model too aggressively. Missing pairs contribute nothing.
    """
    if home is None or away is None:
        return 0.0, False
    denominator = abs(home) + abs(away) + max(0.0, float(smoothing))
    if denominator <= 0.0:
        return 0.0, True
    return _clamp((home - away) / denominator, -1.0, 1.0), True


def _possession_signal(home: float | None, away: float | None) -> tuple[float, bool]:
    if home is None or away is None:
        return 0.0, False
    # A 75/25 split becomes +1.0. Smaller differences remain proportional.
    return _clamp((home - away) / 50.0, -1.0, 1.0), True


def _live_dominance(
    statistics: dict[str, dict[str, float | int | None]],
    *,
    minute: float,
) -> dict[str, Any]:
    """Build a conservative live-dominance signal from available free stats.

    Weighting deliberately favours chance quality over noisier indicators:
    shots on target > total shots > corners > possession. Red cards are not
    folded into this pressure number; they are applied separately because a
    sending-off has a much larger structural effect than normal match pressure.
    """
    home_sot, away_sot = _stat_pair(statistics, "shots_on_target")
    home_shots, away_shots = _stat_pair(statistics, "shots")
    home_corners, away_corners = _stat_pair(statistics, "corners")
    home_poss, away_poss = _stat_pair(statistics, "possession")

    sot_signal, sot_ok = _share_signal(home_sot, away_sot, 2.0)
    shots_signal, shots_ok = _share_signal(home_shots, away_shots, 5.0)
    corners_signal, corners_ok = _share_signal(home_corners, away_corners, 3.0)
    possession_signal, possession_ok = _possession_signal(home_poss, away_poss)

    # Strongest to weakest indicator. The sum is intentionally below 0.60 so
    # live data can move the pre-match prior without completely replacing it.
    weighted = {
        "shots_on_target": 0.24 * sot_signal,
        "shots": 0.14 * shots_signal,
        "corners": 0.09 * corners_signal,
        "possession": 0.07 * possession_signal,
    }
    available = {
        "shots_on_target": sot_ok,
        "shots": shots_ok,
        "corners": corners_ok,
        "possession": possession_ok,
    }

    raw_pressure = sum(weighted.values())

    # Early in the match a small sample is noisy. By minute 25 the evidence is
    # fully trusted; before then it is smoothly damped rather than ignored.
    evidence_scale = _clamp(max(0.35, minute / 25.0), 0.35, 1.0)
    pressure = _clamp(raw_pressure * evidence_scale, -0.42, 0.42)

    # Present a stable 0..100 dominance index for diagnostics/future UI use.
    home_index = _clamp(50.0 + pressure * 80.0, 10.0, 90.0)
    away_index = 100.0 - home_index
    abs_pressure = abs(pressure)
    if abs_pressure < 0.04:
        leader = "balanced"
        strength = "balanced"
    else:
        leader = "home" if pressure > 0 else "away"
        if abs_pressure < 0.10:
            strength = "slight"
        elif abs_pressure < 0.20:
            strength = "clear"
        else:
            strength = "strong"

    return {
        "pressure": pressure,
        "leader": leader,
        "strength": strength,
        "home_index_percent": round(home_index, 1),
        "away_index_percent": round(away_index, 1),
        "evidence_scale": round(evidence_scale, 3),
        "available_inputs": [key for key, ok in available.items() if ok],
        "components": {key: round(value, 4) for key, value in weighted.items()},
    }


def build_live_prediction(
    candidate: dict[str, Any],
    *,
    home_score: int,
    away_score: int,
    minute: int | None,
    statistics: dict[str, dict[str, float | int | None]],
) -> dict[str, Any]:
    """Transparent in-play update built on BetAnalytic pre-match xG.

    Version 0.2 combines the pre-match expected-goal prior with current score,
    remaining time and a bounded live-dominance signal. Dominance uses shots on
    target, total shots, corners and possession when available. Red cards are
    applied separately with a larger effect. Missing free-source statistics are
    simply ignored, preserving the previous safe fallback behaviour.
    """
    base_home, base_away = _prediction_lambdas(candidate)
    clock = _clamp(float(minute if minute is not None else 45), 0.0, 95.0)
    remaining_fraction = _clamp((95.0 - clock) / 95.0, 0.015, 1.0)

    dominance = _live_dominance(statistics, minute=clock)
    pressure = float(dominance["pressure"])

    home_red_raw, away_red_raw = _stat_pair(statistics, "red_cards")
    home_red = float(home_red_raw or 0.0)
    away_red = float(away_red_raw or 0.0)

    # Game-state adjustment: a trailing side normally takes more attacking risk,
    # while a leading side is slightly more conservative. This is deliberately
    # smaller than the live-pressure and red-card effects.
    score_gap = float(home_score - away_score)
    home_state = _clamp(-0.055 * score_gap, -0.16, 0.16)
    away_state = -home_state

    # Red cards are structural, so they are stronger than normal pressure.
    home_multiplier = _clamp(
        1.0 + pressure + home_state - 0.34 * home_red + 0.20 * away_red,
        0.30,
        1.85,
    )
    away_multiplier = _clamp(
        1.0 - pressure + away_state - 0.34 * away_red + 0.20 * home_red,
        0.30,
        1.85,
    )

    rem_home = max(0.01, base_home * remaining_fraction * home_multiplier)
    rem_away = max(0.01, base_away * remaining_fraction * away_multiplier)

    home_win = draw = away_win = over_25 = 0.0
    for hg in range(0, 8):
        ph = _poisson_pmf(rem_home, hg)
        for ag in range(0, 8):
            p = ph * _poisson_pmf(rem_away, ag)
            final_home = home_score + hg
            final_away = away_score + ag
            if final_home > final_away:
                home_win += p
            elif final_home == final_away:
                draw += p
            else:
                away_win += p
            if final_home + final_away >= 3:
                over_25 += p

    total = home_win + draw + away_win
    if total > 0:
        home_win, draw, away_win = home_win / total, draw / total, away_win / total
    goal_hazard = rem_home + rem_away
    no_more = math.exp(-goal_hazard)
    any_more = 1.0 - no_more
    next_home = any_more * rem_home / goal_hazard if goal_hazard > 0 else 0.0
    next_away = any_more * rem_away / goal_hazard if goal_hazard > 0 else 0.0

    return {
        "model": "BetAnalytic Live Update v0.2",
        "method": (
            "pre-match expected goals + current score + remaining time + "
            "live dominance (shots on target/shots/corners/possession) + red cards"
        ),
        "minute_used": int(round(clock)),
        "live_dominance": {
            "leader": dominance["leader"],
            "strength": dominance["strength"],
            "home_index_percent": dominance["home_index_percent"],
            "away_index_percent": dominance["away_index_percent"],
            "available_inputs": dominance["available_inputs"],
            "components": dominance["components"],
        },
        "adjustments": {
            "home_multiplier": round(home_multiplier, 3),
            "away_multiplier": round(away_multiplier, 3),
            "home_red_cards": int(home_red),
            "away_red_cards": int(away_red),
            "score_state_home": round(home_state, 3),
            "score_state_away": round(away_state, 3),
        },
        "remaining_expected_goals": {
            "home": round(rem_home, 3),
            "away": round(rem_away, 3),
            "total": round(rem_home + rem_away, 3),
        },
        "result_probabilities_percent": {
            "home_win": round(home_win * 100.0, 1),
            "draw": round(draw * 100.0, 1),
            "away_win": round(away_win * 100.0, 1),
        },
        "over_2_5_percent": round(_clamp(over_25, 0.0, 1.0) * 100.0, 1),
        "next_goal_percent": {
            "home": round(next_home * 100.0, 1),
            "away": round(next_away * 100.0, 1),
            "no_more_goal": round(no_more * 100.0, 1),
        },
        "disclaimer": "Ζωντανή εκτίμηση του BetAnalytic, όχι αποδόσεις bookmaker.",
    }

def _score_from_timeline(events: list[dict[str, Any]]) -> tuple[int, int]:
    return (
        sum(1 for item in events if item.get("type") == "goal" and item.get("side") == "home"),
        sum(1 for item in events if item.get("type") == "goal" and item.get("side") == "away"),
    )


def _status_is_live(status: str, *, score_known: bool, kickoff: datetime, as_of: datetime) -> bool:
    normalized = status.upper().strip()
    if normalized in ACTIVE_STATUSES:
        return True
    if normalized in FINISHED_STATUSES or normalized in INACTIVE_STATUSES:
        return False
    # Free v1 records can occasionally omit the status while already updating
    # the score/timeline. Accept that only in a narrow post-kickoff window.
    elapsed = as_of - kickoff
    return score_known and timedelta(minutes=1) <= elapsed <= timedelta(hours=3, minutes=30)


def refresh_live_matches(
    feed: dict[str, Any],
    *,
    previous_live: dict[str, Any] | None = None,
    as_of: datetime | None = None,
    api_key: str | None = None,
    session: requests.Session | None = None,
    max_matches: int = MAX_LIVE_MATCHES,
) -> LiveRefreshResult:
    current_time = as_of or utc_now()
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    key = str(api_key or os.getenv("THESPORTSDB_KEY") or FREE_KEY).strip()
    candidates = select_live_candidates(feed, as_of=current_time, max_matches=max_matches)
    if not key:
        return LiveRefreshResult(False, len(candidates), 0, 0, [], ["TheSportsDB key is empty."])

    previous_ids: dict[int, str] = {}
    if isinstance(previous_live, dict):
        for item in previous_live.get("matches", []):
            if not isinstance(item, dict):
                continue
            fixture_id = _as_int(item.get("fixture_id"))
            event_id = str(item.get("provider_event_id") or "").strip()
            if fixture_id and event_id:
                previous_ids[fixture_id] = event_id

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"User-Agent": "BetAnalytic/1.0 (+free live refresh)"})
    warnings: list[str] = []
    requests_used = 0
    matches: list[dict[str, Any]] = []
    try:
        for candidate in candidates:
            fixture_id = _as_int(candidate.get("fixture_id")) or -1
            kickoff = _candidate_datetime(candidate)
            if fixture_id <= 0 or kickoff is None:
                continue

            event: dict[str, Any] | None = None
            event_id = previous_ids.get(fixture_id, "")
            if event_id:
                event, used, error = _lookup_event(http, key, event_id)
                requests_used += used
                if error:
                    warnings.append(f"LIVE lookup {event_id} απέτυχε: {error}")
                if event is not None and not _event_matches(candidate, event):
                    event = None
                    event_id = ""

            if event is None:
                event, used, search_warnings = _search_event(http, key, candidate)
                requests_used += used
                warnings.extend(search_warnings)
                if event is None:
                    continue
                event_id = str(event.get("idEvent") or "").strip()
                # Search payloads can be stale/trimmed. One lookup gives the
                # freshest free-v1 event fields before deciding whether it is live.
                looked_up, used, error = _lookup_event(http, key, event_id)
                requests_used += used
                if error:
                    warnings.append(f"LIVE lookup {event_id} απέτυχε: {error}")
                if looked_up is not None:
                    event = looked_up

            if not event_id:
                continue

            # Fetch timeline and statistics only after we have a matched event id.
            timeline_items: list[dict[str, Any]] = []
            try:
                payload = _request_json(http, key, "lookuptimeline.php", {"id": event_id})
                requests_used += 1
                timeline_items = _timeline_items(payload)
            except (requests.RequestException, ValueError) as exc:
                requests_used += 1
                warnings.append(f"LIVE timeline {event_id} απέτυχε: {exc}")

            stats_payload: dict[str, Any] = {}
            try:
                stats_payload = _request_json(http, key, "lookupeventstats.php", {"id": event_id})
                requests_used += 1
            except (requests.RequestException, ValueError) as exc:
                requests_used += 1
                warnings.append(f"LIVE statistics {event_id} απέτυχαν: {exc}")

            timeline = _parse_timeline(timeline_items, candidate)
            timeline_home, timeline_away = _score_from_timeline(timeline)
            home_score = _as_int(event.get("intHomeScore"))
            away_score = _as_int(event.get("intAwayScore"))
            if home_score is None and timeline:
                home_score = timeline_home
            if away_score is None and timeline:
                away_score = timeline_away
            score_known = home_score is not None and away_score is not None
            status = str(event.get("strStatus") or "").upper().strip()
            if not _status_is_live(status, score_known=score_known, kickoff=kickoff, as_of=current_time):
                continue

            statistics = _parse_statistics(stats_payload)
            minute, estimated_minute = _progress_minute(event, timeline_items, kickoff, current_time)
            if home_score is None:
                home_score = 0
            if away_score is None:
                away_score = 0

            home_team = candidate.get("home_team") if isinstance(candidate.get("home_team"), dict) else {}
            away_team = candidate.get("away_team") if isinstance(candidate.get("away_team"), dict) else {}
            match = {
                "fixture_id": fixture_id,
                "provider_event_id": event_id,
                "provider": SOURCE_NAME,
                "provider_updated_at": event.get("updated") or event.get("strTimestamp"),
                "fixture_date": candidate.get("fixture_date"),
                "status": status or "LIVE",
                "progress": str(event.get("strProgress") or status or "LIVE"),
                "minute": minute,
                "minute_estimated": estimated_minute,
                "home_team": home_team,
                "away_team": away_team,
                "score": {"home": home_score, "away": away_score},
                "events": timeline,
                "statistics": statistics,
                "statistics_available": any(
                    pair.get("home") is not None or pair.get("away") is not None
                    for pair in statistics.values()
                ),
                "live_prediction": build_live_prediction(
                    candidate,
                    home_score=home_score,
                    away_score=away_score,
                    minute=minute,
                    statistics=statistics,
                ),
            }
            matches.append(match)
    finally:
        if own_session:
            http.close()

    return LiveRefreshResult(
        enabled=True,
        candidates_considered=len(candidates),
        matches_live=len(matches),
        requests_used=requests_used,
        matches=matches,
        warnings=warnings,
    )


def build_live_payload(
    feed: dict[str, Any],
    *,
    previous_live: dict[str, Any] | None = None,
    as_of: datetime | None = None,
    api_key: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    current_time = as_of or utc_now()
    result = refresh_live_matches(
        feed,
        previous_live=previous_live,
        as_of=current_time,
        api_key=api_key,
        session=session,
    )
    return {
        "schema_version": 1,
        "generated_at": to_iso_z(current_time),
        "refresh_interval_seconds": 300,
        "source": {
            "name": SOURCE_NAME,
            "mode": "free v1 event lookup + timeline + event statistics",
            "paid_subscription_required": False,
            "premium_livescore_endpoint_used": False,
        },
        "candidates_considered": result.candidates_considered,
        "matches_count": result.matches_live,
        "requests_used": result.requests_used,
        "matches": result.matches,
        "warnings": result.warnings,
    }
