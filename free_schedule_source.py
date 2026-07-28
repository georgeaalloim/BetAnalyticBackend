from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from fixtur_es_source import (
    LEAGUE_ID,
    LEAGUE_NAME,
    _fixture_id,
    resolve_team,
)
from time_utils import parse_iso_datetime


ATHENS_TZ = ZoneInfo("Europe/Athens")
MAX_DATE_DIFFERENCE_DAYS = 7
MAX_TIME_DIFFERENCE_MINUTES = 30


@dataclass(frozen=True)
class FreeScheduleResult:
    fixtures: list[dict[str, Any]]
    verification_counts: dict[str, int]
    source_counts: dict[str, int]
    warnings: list[str]


@dataclass(frozen=True)
class _Candidate:
    source: str
    payload: dict[str, Any]
    kickoff_utc: datetime
    kickoff_local: datetime
    local_date: date
    time_confirmed: bool
    status: str
    home_goals: int | None
    away_goals: int | None


def _candidate(source: str, payload: dict[str, Any]) -> _Candidate | None:
    fixture = payload.get("fixture") or {}
    try:
        kickoff_utc = parse_iso_datetime(str(fixture.get("date") or ""))
    except ValueError:
        return None
    kickoff_utc = kickoff_utc.astimezone(timezone.utc)
    kickoff_local = kickoff_utc.astimezone(ATHENS_TZ)
    goals = payload.get("goals") or {}
    return _Candidate(
        source=source,
        payload=payload,
        kickoff_utc=kickoff_utc,
        kickoff_local=kickoff_local,
        local_date=kickoff_local.date(),
        time_confirmed=bool(fixture.get("time_confirmed")),
        status=str((fixture.get("status") or {}).get("short") or "").upper(),
        home_goals=_as_int(goals.get("home")),
        away_goals=_as_int(goals.get("away")),
    )


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _team_key(payload: dict[str, Any]) -> tuple[int, int, int]:
    league = payload["league"]
    teams = payload["teams"]
    return (
        int(league["season"]),
        int(teams["home"]["id"]),
        int(teams["away"]["id"]),
    )


def _closest_unused(
    anchor: _Candidate,
    candidates: list[_Candidate],
    used: set[int],
) -> tuple[int, _Candidate] | None:
    ranked: list[tuple[int, int, int, _Candidate]] = []
    for index, item in enumerate(candidates):
        if index in used:
            continue
        days = abs((item.local_date - anchor.local_date).days)
        if days > MAX_DATE_DIFFERENCE_DAYS:
            continue
        seconds = abs(int((item.kickoff_utc - anchor.kickoff_utc).total_seconds()))
        ranked.append((days, seconds, index, item))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    _, _, index, selected = ranked[0]
    return index, selected


def _consensus_date(items: list[_Candidate], primary: _Candidate) -> tuple[date, bool]:
    counts = Counter(item.local_date for item in items)
    best_date, best_count = counts.most_common(1)[0]
    if best_count >= 2:
        return best_date, True
    return primary.local_date, False


def _technical_kickoff(consensus_date: date, candidates: list[_Candidate]) -> datetime:
    same_day_confirmed = [
        item.kickoff_local
        for item in candidates
        if item.local_date == consensus_date and item.time_confirmed
    ]
    if same_day_confirmed:
        values = sorted(item.timestamp() for item in same_day_confirmed)
        return datetime.fromtimestamp(median(values), tz=ATHENS_TZ)
    return datetime.combine(consensus_date, time(hour=12), tzinfo=ATHENS_TZ)


def _verified_kickoff(
    consensus_date: date,
    candidates: list[_Candidate],
    *,
    date_verified: bool,
) -> tuple[datetime, bool, str]:
    """Select a safe, displayable kickoff time.

    Two timed sources that agree remain the strongest case.  A single source
    with an explicit time is also displayable when a second independent
    source confirms the match date.  This avoids hiding every kickoff merely
    because date-only datasets do not publish hours.
    """
    exact = [
        item.kickoff_local
        for item in candidates
        if item.local_date == consensus_date and item.time_confirmed
    ]
    if not exact:
        return _technical_kickoff(consensus_date, candidates), False, "none"

    timestamps = sorted(item.timestamp() for item in exact)
    spread_minutes = (timestamps[-1] - timestamps[0]) / 60.0
    if spread_minutes > MAX_TIME_DIFFERENCE_MINUTES:
        return _technical_kickoff(consensus_date, candidates), False, "conflict"

    selected = datetime.fromtimestamp(median(timestamps), tz=ATHENS_TZ)
    if len(exact) >= 2:
        return selected, True, "cross_checked"
    if date_verified:
        return selected, True, "date_checked_single_time"
    return selected, False, "single_source"


def _select_result(candidates: list[_Candidate]) -> tuple[str, int | None, int | None]:
    completed = [
        item
        for item in candidates
        if item.status == "FT"
        and item.home_goals is not None
        and item.away_goals is not None
    ]
    if completed:
        score_counts = Counter(
            (item.home_goals, item.away_goals) for item in completed
        )
        (home, away), _ = score_counts.most_common(1)[0]
        return "FT", home, away
    if any(item.status == "PST" for item in candidates):
        return "PST", None, None
    return "", None, None


def _verification_label(
    candidates: list[_Candidate],
    *,
    date_verified: bool,
    time_verified: bool,
    time_basis: str,
) -> str:
    source_count = len({item.source for item in candidates})
    if time_verified and time_basis == "cross_checked":
        return "time_verified"
    if time_verified and time_basis == "date_checked_single_time":
        return "date_verified_time_reported"
    if time_basis == "conflict":
        return "source_conflict"
    if date_verified:
        return "date_verified"
    if source_count >= 2:
        return "source_conflict"
    return "single_source"


def _source_text(
    candidates: list[_Candidate],
    verification: str,
) -> str:
    sources = sorted({item.source for item in candidates})
    label = {
        "time_verified": "cross-checked date and time",
        "date_verified_time_reported": "verified date; time reported by one source",
        "date_verified": "verified date; time pending",
        "source_conflict": "source conflict; time hidden",
        "single_source": "single source; time pending",
    }[verification]
    return f"Free sources: {' + '.join(sources)} ({label})"


def _merge_group(
    candidates: list[_Candidate],
    primary: _Candidate,
    *,
    as_of: datetime,
) -> tuple[dict[str, Any], str]:
    consensus_date, date_verified = _consensus_date(candidates, primary)
    kickoff_local, time_verified, time_basis = _verified_kickoff(
        consensus_date,
        candidates,
        date_verified=date_verified,
    )
    result_status, home_goals, away_goals = _select_result(candidates)
    if result_status:
        status = result_status
    elif kickoff_local.astimezone(timezone.utc) > as_of.astimezone(timezone.utc):
        status = "NS" if time_verified else "TBD"
    else:
        status = "TBD"

    verification = _verification_label(
        candidates,
        date_verified=date_verified,
        time_verified=time_verified,
        time_basis=time_basis,
    )
    teams = primary.payload["teams"]
    home_team_id, home_name = resolve_team(str(teams["home"]["name"]))
    away_team_id, away_name = resolve_team(str(teams["away"]["name"]))
    season = int(primary.payload["league"]["season"])
    kickoff_utc = kickoff_local.astimezone(timezone.utc)

    payload = {
        "fixture": {
            "id": _fixture_id(season, kickoff_local, home_name, away_name),
            "date": kickoff_utc.isoformat(),
            "status": {"short": status},
            "time_confirmed": time_verified,
            "source": _source_text(candidates, verification),
            "verification": verification,
            "sources": sorted({item.source for item in candidates}),
        },
        "league": {
            "id": LEAGUE_ID,
            "name": LEAGUE_NAME,
            "season": season,
        },
        "teams": {
            "home": {"id": home_team_id, "name": home_name},
            "away": {"id": away_team_id, "name": away_name},
        },
        "goals": {"home": home_goals, "away": away_goals},
    }
    return payload, verification


def merge_free_schedule_sources(
    *,
    fixtur_es_fixtures: Iterable[dict[str, Any]],
    openfootball_fixtures: Iterable[dict[str, Any]],
    football_data_fixtures: Iterable[dict[str, Any]],
    api_football_fixtures: Iterable[dict[str, Any]] = (),
    as_of: datetime,
) -> FreeScheduleResult:
    source_payloads = {
        "Fixtur.es": list(fixtur_es_fixtures),
        "OpenFootball CC0": list(openfootball_fixtures),
        "Football-Data.co.uk": list(football_data_fixtures),
        "API-Football Free": list(api_football_fixtures),
    }
    grouped: dict[str, dict[tuple[int, int, int], list[_Candidate]]] = {}
    source_counts: dict[str, int] = {}
    warnings: list[str] = []

    for source, payloads in source_payloads.items():
        index: dict[tuple[int, int, int], list[_Candidate]] = defaultdict(list)
        for payload in payloads:
            try:
                key = _team_key(payload)
            except (KeyError, TypeError, ValueError):
                continue
            item = _candidate(source, payload)
            if item is not None:
                index[key].append(item)
        for items in index.values():
            items.sort(key=lambda item: item.kickoff_utc)
        grouped[source] = index
        source_counts[source] = sum(len(items) for items in index.values())

    anchors: list[_Candidate] = []
    for items in grouped["Fixtur.es"].values():
        anchors.extend(items)
    if not anchors:
        for source in (
            "API-Football Free",
            "OpenFootball CC0",
            "Football-Data.co.uk",
        ):
            for items in grouped[source].values():
                anchors.extend(items)
            if anchors:
                break

    used: dict[str, dict[tuple[int, int, int], set[int]]] = {
        source: defaultdict(set) for source in grouped
    }
    merged: list[dict[str, Any]] = []
    verification_counts: Counter[str] = Counter()

    for anchor in sorted(anchors, key=lambda item: item.kickoff_utc):
        key = _team_key(anchor.payload)
        candidates = [anchor]
        anchor_source_items = grouped[anchor.source][key]
        try:
            anchor_index = anchor_source_items.index(anchor)
            used[anchor.source][key].add(anchor_index)
        except ValueError:
            pass

        for source in grouped:
            if source == anchor.source:
                continue
            match = _closest_unused(
                anchor,
                grouped[source].get(key, []),
                used[source][key],
            )
            if match is not None:
                index, item = match
                used[source][key].add(index)
                candidates.append(item)

        payload, verification = _merge_group(candidates, anchor, as_of=as_of)
        merged.append(payload)
        verification_counts[verification] += 1
        if verification == "source_conflict":
            warnings.append(
                "Διαφωνία δωρεάν πηγών για "
                f"{payload['teams']['home']['name']} - "
                f"{payload['teams']['away']['name']}; η ώρα αποκρύφτηκε."
            )

    # Add fixtures that exist only in a secondary source.
    for source in (
        "API-Football Free",
        "OpenFootball CC0",
        "Football-Data.co.uk",
    ):
        for key, items in grouped[source].items():
            for index, item in enumerate(items):
                if index in used[source][key]:
                    continue
                payload, verification = _merge_group([item], item, as_of=as_of)
                merged.append(payload)
                verification_counts[verification] += 1
                used[source][key].add(index)

    # Stable de-duplication. Prefer a verified record over an unverified one.
    priority = {
        "time_verified": 5,
        "date_verified_time_reported": 4,
        "date_verified": 3,
        "source_conflict": 2,
        "single_source": 1,
    }
    by_id: dict[int, dict[str, Any]] = {}
    for payload in merged:
        fixture_id = int(payload["fixture"]["id"])
        current = by_id.get(fixture_id)
        if current is None:
            by_id[fixture_id] = payload
            continue
        current_level = str(current["fixture"].get("verification") or "single_source")
        new_level = str(payload["fixture"].get("verification") or "single_source")
        if priority.get(new_level, 0) > priority.get(current_level, 0):
            by_id[fixture_id] = payload

    normalized = sorted(by_id.values(), key=lambda item: str(item["fixture"]["date"]))
    return FreeScheduleResult(
        fixtures=normalized,
        verification_counts=dict(verification_counts),
        source_counts=source_counts,
        warnings=warnings,
    )
