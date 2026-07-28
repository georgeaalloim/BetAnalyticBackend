from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from fixtur_es_source import (
    LEAGUE_ID,
    LEAGUE_NAME,
    _fixture_id,
    resolve_team,
)


SOURCE_NAME = "OpenFootball CC0"
BASE_URL = (
    "https://raw.githubusercontent.com/openfootball/europe/"
    "master/greece/{season_label}_gr1.txt"
)
REQUEST_TIMEOUT_SECONDS = 30
ATHENS_TZ = ZoneInfo("Europe/Athens")

_DATE_WITH_YEAR = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})$"
)
_DATE_WITHOUT_YEAR = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"([A-Z][a-z]{2})\s+(\d{1,2})$"
)
_TIME_PREFIX = re.compile(r"^(\d{1,2}:\d{2})\s+(.+)$")
_SCORE_SUFFIX = re.compile(r"\s+(\d{1,2})-(\d{1,2})(?:\s+\([^)]*\))?$")
_NOTE_SUFFIX = re.compile(r"\s+\[([^]]+)\]$")
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass(frozen=True)
class OpenFootballResult:
    fixtures: list[dict[str, Any]]
    seasons_requested: list[int]
    seasons_loaded: list[int]
    urls_loaded: list[str]
    warnings: list[str]


def season_label(season: int) -> str:
    return f"{season}-{(season + 1) % 100:02d}"


def _clean_line(value: str) -> str:
    return " ".join(value.strip().split())


def _parse_date_line(line: str, *, current_year: int | None) -> tuple[date | None, int | None]:
    match = _DATE_WITH_YEAR.match(line)
    if match:
        month, day, year = match.groups()
        parsed = date(int(year), _MONTHS[month], int(day))
        return parsed, parsed.year

    match = _DATE_WITHOUT_YEAR.match(line)
    if match and current_year is not None:
        month, day = match.groups()
        parsed_month = _MONTHS[month]
        year = current_year
        # The season crosses New Year. If dates move from Dec to Jan, advance.
        return date(year, parsed_month, int(day)), year

    return None, current_year


def _season_year_for_month(season: int, month: int) -> int:
    return season if month >= 7 else season + 1


def parse_openfootball_text(
    text: str,
    *,
    season: int,
    as_of: datetime,
) -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    current_date: date | None = None
    current_year: int | None = season
    normalized_as_of = as_of.astimezone(timezone.utc)

    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line or line.startswith(("#", "=", "▪", "::")):
            continue

        parsed_date, parsed_year = _parse_date_line(line, current_year=current_year)
        if parsed_date is not None:
            # Date lines without year need the season boundary rule, not merely
            # the previous numeric year.
            if not _DATE_WITH_YEAR.match(line):
                month_match = _DATE_WITHOUT_YEAR.match(line)
                assert month_match is not None
                month = _MONTHS[month_match.group(1)]
                parsed_date = date(
                    _season_year_for_month(season, month),
                    month,
                    int(month_match.group(2)),
                )
            current_date = parsed_date
            current_year = parsed_year
            continue

        if current_date is None:
            continue
        note = None
        note_match = _NOTE_SUFFIX.search(line)
        if note_match:
            note = note_match.group(1)
            line = line[: note_match.start()].strip()

        home_score = away_score = None
        score_match = _SCORE_SUFFIX.search(line)
        if score_match:
            home_score, away_score = score_match.groups()
            line = line[: score_match.start()].strip()

        raw_time = None
        time_match = _TIME_PREFIX.match(line)
        if time_match:
            raw_time, line = time_match.groups()

        if " v " not in line:
            continue
        home_raw, away_raw = (part.strip() for part in line.split(" v ", 1))
        if not home_raw or not away_raw:
            continue

        if raw_time:
            parsed_time = datetime.strptime(raw_time, "%H:%M").time()
            time_confirmed = True
        else:
            parsed_time = time(hour=12)
            time_confirmed = False

        kickoff_local = datetime.combine(current_date, parsed_time, tzinfo=ATHENS_TZ)
        kickoff_utc = kickoff_local.astimezone(timezone.utc)
        home_team_id, home_team_name = resolve_team(home_raw)
        away_team_id, away_team_name = resolve_team(away_raw)
        goals_home = int(home_score) if home_score is not None else None
        goals_away = int(away_score) if away_score is not None else None
        note_text = str(note or "").casefold()

        if "postpon" in note_text or "cancel" in note_text:
            status = "PST"
            goals_home = None
            goals_away = None
        elif goals_home is not None and goals_away is not None:
            status = "FT"
        elif kickoff_utc > normalized_as_of:
            status = "NS" if time_confirmed else "TBD"
        else:
            status = "TBD"

        fixtures.append(
            {
                "fixture": {
                    "id": _fixture_id(
                        season,
                        kickoff_local,
                        home_team_name,
                        away_team_name,
                    ),
                    "date": kickoff_utc.isoformat(),
                    "status": {"short": status},
                    "time_confirmed": time_confirmed,
                    "source": SOURCE_NAME,
                },
                "league": {
                    "id": LEAGUE_ID,
                    "name": LEAGUE_NAME,
                    "season": season,
                },
                "teams": {
                    "home": {"id": home_team_id, "name": home_team_name},
                    "away": {"id": away_team_id, "name": away_team_name},
                },
                "goals": {"home": goals_home, "away": goals_away},
            }
        )

    by_id: dict[int, dict[str, Any]] = {}
    for fixture in fixtures:
        fixture_id = int(fixture["fixture"]["id"])
        previous = by_id.get(fixture_id)
        if previous is None:
            by_id[fixture_id] = fixture
            continue
        if (
            previous["fixture"]["status"]["short"] != "FT"
            and fixture["fixture"]["status"]["short"] == "FT"
        ):
            by_id[fixture_id] = fixture

    return sorted(by_id.values(), key=lambda item: str(item["fixture"]["date"]))


def fetch_openfootball_fixtures(
    *,
    seasons: Iterable[int],
    as_of: datetime,
    session: requests.Session | None = None,
) -> OpenFootballResult:
    requested = sorted(set(int(item) for item in seasons))
    own_session = session is None
    http = session or requests.Session()
    http.headers.update(
        {
            "User-Agent": (
                "BetAnalytic/1.0 (+https://github.com/"
                "georgeaalloim/BetAnalyticBackend)"
            ),
            "Accept": "text/plain,*/*;q=0.8",
        }
    )

    loaded: list[int] = []
    urls_loaded: list[str] = []
    warnings: list[str] = []
    all_fixtures: list[dict[str, Any]] = []

    try:
        for season in requested:
            url = BASE_URL.format(season_label=season_label(season))
            try:
                response = http.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                if response.status_code == 404:
                    warnings.append(
                        f"Δεν έχει δημοσιευτεί ακόμη OpenFootball αρχείο για {season}."
                    )
                    continue
                response.raise_for_status()
                parsed = parse_openfootball_text(
                    response.text,
                    season=season,
                    as_of=as_of,
                )
                if not parsed:
                    warnings.append(
                        f"Το OpenFootball αρχείο της σεζόν {season} δεν αναγνωρίστηκε."
                    )
                    continue
                loaded.append(season)
                urls_loaded.append(str(response.url))
                all_fixtures.extend(parsed)
            except (requests.RequestException, ValueError) as error:
                warnings.append(f"Αποτυχία OpenFootball {url}: {error}")
    finally:
        if own_session:
            http.close()

    by_id = {int(item["fixture"]["id"]): item for item in all_fixtures}
    fixtures = sorted(by_id.values(), key=lambda item: str(item["fixture"]["date"]))
    return OpenFootballResult(
        fixtures=fixtures,
        seasons_requested=requested,
        seasons_loaded=loaded,
        urls_loaded=urls_loaded,
        warnings=warnings,
    )
