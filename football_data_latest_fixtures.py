from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from fixtur_es_source import LEAGUE_ID, LEAGUE_NAME, _fixture_id, resolve_team, season_from_local_date


SOURCE_NAME = "Football-Data.co.uk Latest Fixtures"
FIXTURES_URL = "https://www.football-data.co.uk/matches/resources/fixtures.csv"
REQUEST_TIMEOUT_SECONDS = 30
ATHENS_TZ = ZoneInfo("Europe/Athens")
GREECE_DIVISION_CODES = frozenset({"G1"})


@dataclass(frozen=True)
class FootballDataLatestFixturesResult:
    fixtures: list[dict[str, Any]]
    url: str
    rows_loaded: int
    greek_rows_loaded: int
    warnings: list[str]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _parse_local_datetime(date_value: Any, time_value: Any) -> datetime | None:
    date_text = _clean(date_value)
    time_text = _clean(time_value)
    if not date_text or not time_text:
        return None

    match_date = None
    for pattern in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            match_date = datetime.strptime(date_text, pattern).date()
            break
        except ValueError:
            continue
    if match_date is None:
        return None

    match_time = None
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            match_time = datetime.strptime(time_text, pattern).time()
            break
        except ValueError:
            continue
    if match_time is None:
        return None

    return datetime.combine(match_date, match_time, tzinfo=ATHENS_TZ)


def parse_latest_fixtures_csv(csv_text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    field_names = set(reader.fieldnames or [])
    required = {"Div", "Date", "Time", "HomeTeam", "AwayTeam"}
    if not required.issubset(field_names):
        missing = ", ".join(sorted(required - field_names))
        raise ValueError(f"Το latest fixtures CSV δεν περιέχει: {missing}")

    fixtures: list[dict[str, Any]] = []
    for row in reader:
        if _clean(row.get("Div")).upper() not in GREECE_DIVISION_CODES:
            continue
        home_raw = _clean(row.get("HomeTeam"))
        away_raw = _clean(row.get("AwayTeam"))
        if not home_raw or not away_raw:
            continue
        kickoff_local = _parse_local_datetime(row.get("Date"), row.get("Time"))
        if kickoff_local is None:
            continue

        home_id, home_name = resolve_team(home_raw)
        away_id, away_name = resolve_team(away_raw)
        season = season_from_local_date(kickoff_local.date())
        kickoff_utc = kickoff_local.astimezone(timezone.utc)

        fixtures.append(
            {
                "fixture": {
                    "id": _fixture_id(season, kickoff_local, home_name, away_name),
                    "date": kickoff_utc.isoformat(),
                    "status": {"short": "NS"},
                    "time_confirmed": True,
                    "source": SOURCE_NAME,
                },
                "league": {
                    "id": LEAGUE_ID,
                    "name": LEAGUE_NAME,
                    "season": season,
                },
                "teams": {
                    "home": {"id": home_id, "name": home_name},
                    "away": {"id": away_id, "name": away_name},
                },
                "goals": {"home": None, "away": None},
            }
        )

    by_id = {int(item["fixture"]["id"]): item for item in fixtures}
    return sorted(by_id.values(), key=lambda item: str(item["fixture"]["date"]))


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "iso-8859-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_latest_football_data_fixtures(
    *,
    session: requests.Session | None = None,
) -> FootballDataLatestFixturesResult:
    own_session = session is None
    http = session or requests.Session()
    http.headers.update(
        {
            "User-Agent": "BetAnalytic/1.0",
            "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.8",
        }
    )
    warnings: list[str] = []
    try:
        response = http.get(FIXTURES_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        text = _decode(response.content)
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        all_rows = list(reader)
        fixtures = parse_latest_fixtures_csv(text)
        greek_rows = sum(
            1
            for row in all_rows
            if _clean(row.get("Div")).upper() in GREECE_DIVISION_CODES
        )
        if not fixtures:
            warnings.append(
                "Το latest fixtures CSV δεν περιείχε αυτή τη στιγμή "
                "ελληνικούς αγώνες με ρητή ώρα."
            )
        return FootballDataLatestFixturesResult(
            fixtures=fixtures,
            url=str(response.url),
            rows_loaded=len(all_rows),
            greek_rows_loaded=greek_rows,
            warnings=warnings,
        )
    except (requests.RequestException, ValueError) as error:
        warnings.append(f"Αποτυχία latest Football-Data fixtures: {error}")
        return FootballDataLatestFixturesResult(
            fixtures=[],
            url=FIXTURES_URL,
            rows_loaded=0,
            greek_rows_loaded=0,
            warnings=warnings,
        )
    finally:
        if own_session:
            http.close()
