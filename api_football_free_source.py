from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from fixtur_es_source import LEAGUE_ID, LEAGUE_NAME, resolve_team
from time_utils import parse_iso_datetime


SOURCE_NAME = "API-Football Free"
BASE_URL = "https://v3.football.api-sports.io"
REQUEST_TIMEOUT_SECONDS = 25
ATHENS_TZ = ZoneInfo("Europe/Athens")


@dataclass(frozen=True)
class ApiFootballFreeResult:
    fixtures: list[dict[str, Any]]
    enabled: bool
    seasons_requested: list[int]
    seasons_loaded: list[int]
    requests_used: int
    quota_remaining: int | None
    warnings: list[str]


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _time_is_confirmed(date_value: str, status: str) -> bool:
    normalized_status = status.upper()
    if normalized_status in {"TBD", "PST", "CANC", "ABD", "AWD", "WO"}:
        return False
    try:
        kickoff = parse_iso_datetime(date_value)
    except ValueError:
        return False
    # 00:00 is frequently a placeholder before the broadcaster fixes kickoff.
    local_kickoff = kickoff.astimezone(ATHENS_TZ)
    if (
        normalized_status in {"NS", "TBD"}
        and local_kickoff.hour == 0
        and local_kickoff.minute == 0
    ):
        return False
    return True


def parse_api_football_response(
    payload: dict[str, Any],
    *,
    season: int,
) -> list[dict[str, Any]]:
    response = payload.get("response")
    if not isinstance(response, list):
        raise ValueError("Το API-Football δεν επέστρεψε λίστα response.")

    fixtures: list[dict[str, Any]] = []
    for item in response:
        if not isinstance(item, dict):
            continue
        fixture = item.get("fixture") or {}
        league = item.get("league") or {}
        teams = item.get("teams") or {}
        goals = item.get("goals") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        status = str((fixture.get("status") or {}).get("short") or "TBD").upper()
        date_value = str(fixture.get("date") or "")
        fixture_id = _as_int(fixture.get("id"))
        if fixture_id is None or not date_value:
            continue
        if _as_int(league.get("id")) not in {None, LEAGUE_ID}:
            continue

        # Use the repository's canonical team IDs for cross-source matching.
        # The API fixture ID is preserved, but team IDs must remain identical
        # to Fixtur.es/OpenFootball/Football-Data aliases.
        home_id, home_name = resolve_team(str(home.get("name") or ""))
        away_id, away_name = resolve_team(str(away.get("name") or ""))

        fixtures.append(
            {
                "fixture": {
                    "id": fixture_id,
                    "date": date_value,
                    "status": {"short": status},
                    "time_confirmed": _time_is_confirmed(date_value, status),
                    "source": SOURCE_NAME,
                },
                "league": {
                    "id": LEAGUE_ID,
                    "name": LEAGUE_NAME,
                    "season": int(league.get("season") or season),
                },
                "teams": {
                    "home": {"id": home_id, "name": home_name},
                    "away": {"id": away_id, "name": away_name},
                },
                "goals": {
                    "home": _as_int(goals.get("home")),
                    "away": _as_int(goals.get("away")),
                },
            }
        )

    by_id = {int(item["fixture"]["id"]): item for item in fixtures}
    return sorted(by_id.values(), key=lambda item: str(item["fixture"]["date"]))


def fetch_api_football_fixtures(
    *,
    seasons: Iterable[int],
    api_key: str | None,
    session: requests.Session | None = None,
) -> ApiFootballFreeResult:
    requested = sorted(set(int(item) for item in seasons))
    cleaned_key = str(api_key or "").strip()
    if not cleaned_key:
        return ApiFootballFreeResult(
            fixtures=[],
            enabled=False,
            seasons_requested=requested,
            seasons_loaded=[],
            requests_used=0,
            quota_remaining=None,
            warnings=[
                "Δεν έχει οριστεί προαιρετικό API_FOOTBALL_KEY. "
                "Η διασταύρωση συνεχίζεται με τις πηγές χωρίς κλειδί."
            ],
        )

    own_session = session is None
    http = session or requests.Session()
    http.headers.update(
        {
            "x-apisports-key": cleaned_key,
            "User-Agent": (
                "BetAnalytic/1.0 (+https://github.com/"
                "georgeaalloim/BetAnalyticBackend)"
            ),
        }
    )
    all_fixtures: list[dict[str, Any]] = []
    loaded: list[int] = []
    warnings: list[str] = []
    requests_used = 0
    quota_remaining: int | None = None

    try:
        for season in requested:
            try:
                response = http.get(
                    f"{BASE_URL}/fixtures",
                    params={
                        "league": LEAGUE_ID,
                        "season": season,
                        "timezone": "Europe/Athens",
                    },
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                requests_used += 1
                response.raise_for_status()
                remaining = response.headers.get("x-ratelimit-requests-remaining")
                if remaining is not None:
                    try:
                        quota_remaining = int(remaining)
                    except ValueError:
                        pass
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Μη έγκυρο JSON object.")
                if payload.get("errors"):
                    raise ValueError(f"API errors: {payload['errors']}")
                parsed = parse_api_football_response(payload, season=season)
                if parsed:
                    loaded.append(season)
                    all_fixtures.extend(parsed)
                else:
                    warnings.append(
                        f"Το δωρεάν API-Football δεν επέστρεψε αγώνες για {season}."
                    )
            except (requests.RequestException, ValueError) as error:
                warnings.append(f"Αποτυχία API-Football Free για {season}: {error}")
    finally:
        if own_session:
            http.close()

    by_id = {int(item["fixture"]["id"]): item for item in all_fixtures}
    return ApiFootballFreeResult(
        fixtures=sorted(
            by_id.values(), key=lambda item: str(item["fixture"]["date"])
        ),
        enabled=True,
        seasons_requested=requested,
        seasons_loaded=loaded,
        requests_used=requests_used,
        quota_remaining=quota_remaining,
        warnings=warnings,
    )
