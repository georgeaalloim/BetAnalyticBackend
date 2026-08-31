from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from fixtur_es_source import resolve_team
from goal_scorer_enricher import (
    FREE_KEY,
    THESPORTSDB_LEAGUE_ID,
    _event_items,
    _name_candidates,
    _names_match,
    _request_json,
)
from time_utils import parse_iso_datetime

ATHENS_TZ = ZoneInfo("Europe/Athens")

# Provider-facing names only. BetAnalytic keeps its canonical names in the feed/UI.
# The first value is the preferred TheSportsDB spelling used for title search.
PREFERRED_PROVIDER_NAMES: dict[int, tuple[str, ...]] = {
    575: ("AEK Athens", "AEK"),
    1123: ("Aris Thessaloniki", "Aris"),
    955: ("Asteras Tripolis", "Asteras Aktor"),
    12260: ("Atromitos", "Atromitos Athens"),
    1026357653: ("Iraklis 1908", "Iraklis", "POT Iraklis"),
    1068316644: ("Kalamata",),
    5050: ("Kifisia",),
    957: ("Levadiakos", "Levadeiakos"),
    1124: ("OFI Crete", "OFI"),
    553: ("Olympiacos", "Olympiakos"),
    617: ("Panathinaikos",),
    949: ("Panetolikos",),
    619: ("PAOK",),
    2110: ("Volos", "Volos NFC"),
}


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def fixture_identity(fixture: dict[str, Any]) -> tuple[int, int, str, int, str] | None:
    """Return fixture_id, home_id/name, away_id/name for feed or SQLite-shaped rows."""
    fixture_id = _as_int(fixture.get("fixture_id"))
    if fixture_id is None:
        return None

    home_block = fixture.get("home_team")
    away_block = fixture.get("away_team")
    if isinstance(home_block, dict) and isinstance(away_block, dict):
        home_id = _as_int(home_block.get("team_id"))
        away_id = _as_int(away_block.get("team_id"))
        home_name = str(home_block.get("team_name") or "").strip()
        away_name = str(away_block.get("team_name") or "").strip()
    else:
        home_id = _as_int(fixture.get("home_team_id"))
        away_id = _as_int(fixture.get("away_team_id"))
        home_name = str(fixture.get("home_team_name") or "").strip()
        away_name = str(fixture.get("away_team_name") or "").strip()

    if home_id is None or away_id is None or not home_name or not away_name:
        return None
    return fixture_id, home_id, home_name, away_id, away_name


def fixture_local_date(fixture: dict[str, Any]) -> str | None:
    try:
        kickoff = parse_iso_datetime(str(fixture.get("fixture_date") or ""))
    except (TypeError, ValueError):
        return None
    return kickoff.astimezone(ATHENS_TZ).date().isoformat()


def provider_name_variants(team_id: int, canonical_name: str) -> tuple[str, ...]:
    ordered = [
        *PREFERRED_PROVIDER_NAMES.get(int(team_id), ()),
        canonical_name,
        *_name_candidates(int(team_id), canonical_name),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for raw in ordered:
        value = str(raw or "").strip()
        key = value.casefold()
        if value and key not in seen:
            out.append(value)
            seen.add(key)
    return tuple(out)


def preferred_provider_name(team_id: int, canonical_name: str) -> str:
    variants = provider_name_variants(team_id, canonical_name)
    return variants[0] if variants else canonical_name


def _event_local_date(event: dict[str, Any]) -> str:
    return str(event.get("dateEventLocal") or event.get("dateEvent") or "").strip()


def event_matches_fixture(fixture: dict[str, Any], event: dict[str, Any]) -> bool:
    identity = fixture_identity(fixture)
    if identity is None:
        return False
    _, home_id, home_name, away_id, away_name = identity

    league = str(event.get("idLeague") or "").strip()
    if league and league != str(THESPORTSDB_LEAGUE_ID):
        return False

    expected_date = fixture_local_date(fixture)
    event_date = _event_local_date(event)
    if expected_date and event_date and expected_date != event_date:
        return False

    event_home = str(event.get("strHomeTeam") or "").strip()
    event_away = str(event.get("strAwayTeam") or "").strip()
    if not event_home or not event_away:
        return False

    resolved_home_id, _ = resolve_team(event_home)
    resolved_away_id, _ = resolve_team(event_away)
    if int(resolved_home_id) == home_id and int(resolved_away_id) == away_id:
        return bool(event.get("idEvent"))

    return (
        _names_match(event_home, home_id, home_name)
        and _names_match(event_away, away_id, away_name)
        and bool(event.get("idEvent"))
    )


def fetch_day_events(
    session: requests.Session,
    key: str,
    local_date: str,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """One cheap day lookup first, avoiding fragile title matching."""
    try:
        payload = _request_json(
            session,
            key,
            "eventsday.php",
            {"d": local_date, "l": str(THESPORTSDB_LEAGUE_ID)},
        )
        return _event_items(payload), 1, None
    except (requests.RequestException, ValueError) as exc:
        return [], 1, str(exc)


def search_event(
    session: requests.Session,
    key: str,
    fixture: dict[str, Any],
    *,
    max_searches: int = 6,
) -> tuple[dict[str, Any] | None, int, list[str]]:
    identity = fixture_identity(fixture)
    local_date = fixture_local_date(fixture)
    if identity is None or local_date is None:
        return None, 0, ["LIVE/result candidate χωρίς έγκυρα στοιχεία ομάδων/ημερομηνίας."]

    _, home_id, home_name, away_id, away_name = identity
    home_names = provider_name_variants(home_id, home_name)
    away_names = provider_name_variants(away_id, away_name)

    pairs: list[tuple[str, str]] = []
    for home in home_names:
        for away in away_names:
            pair = (home, away)
            if pair not in pairs:
                pairs.append(pair)
            if len(pairs) >= max(1, int(max_searches)):
                break
        if len(pairs) >= max(1, int(max_searches)):
            break

    requests_used = 0
    warnings: list[str] = []
    for home, away in pairs:
        try:
            payload = _request_json(
                session,
                key,
                "searchevents.php",
                {
                    "e": f"{home}_vs_{away}".replace(" ", "_"),
                    "d": local_date,
                },
            )
            requests_used += 1
        except (requests.RequestException, ValueError) as exc:
            requests_used += 1
            warnings.append(
                f"TheSportsDB search απέτυχε για {home_name} - {away_name}: {exc}"
            )
            continue

        for event in _event_items(payload):
            if event_matches_fixture(fixture, event):
                return event, requests_used, warnings

    return None, requests_used, warnings


def discover_events(
    fixtures: Iterable[dict[str, Any]],
    *,
    api_key: str | None = None,
    session: requests.Session | None = None,
    max_searches_per_fixture: int = 6,
) -> tuple[dict[int, dict[str, Any]], int, list[str]]:
    """Discover event IDs by day first, then robust title aliases for misses."""
    candidates = [item for item in fixtures if fixture_identity(item) is not None]
    key = str(api_key or FREE_KEY).strip()
    if not candidates or not key:
        return {}, 0, ([] if key else ["TheSportsDB key is empty."])

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({"User-Agent": "BetAnalytic/1.2 (+free recent/live sync)"})

    requests_used = 0
    warnings: list[str] = []
    found: dict[int, dict[str, Any]] = {}
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for fixture in candidates:
        local_date = fixture_local_date(fixture)
        if local_date:
            by_date[local_date].append(fixture)

    try:
        # First pass: one request can discover several same-day league events.
        for local_date, date_fixtures in by_date.items():
            events, used, error = fetch_day_events(http, key, local_date)
            requests_used += used
            if error:
                warnings.append(f"TheSportsDB eventsday {local_date} απέτυχε: {error}")
            for fixture in date_fixtures:
                identity = fixture_identity(fixture)
                if identity is None:
                    continue
                fixture_id = identity[0]
                for event in events:
                    if event_matches_fixture(fixture, event):
                        found[fixture_id] = event
                        break

        # Second pass: day endpoint can be free-tier limited, so search only misses.
        for fixture in candidates:
            identity = fixture_identity(fixture)
            if identity is None:
                continue
            fixture_id = identity[0]
            if fixture_id in found:
                continue
            event, used, search_warnings = search_event(
                http,
                key,
                fixture,
                max_searches=max_searches_per_fixture,
            )
            requests_used += used
            warnings.extend(search_warnings)
            if event is not None:
                found[fixture_id] = event
    finally:
        if own_session:
            http.close()

    return found, requests_used, warnings
