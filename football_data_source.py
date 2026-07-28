from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from database import get_connection, save_fixtures
from fixtur_es_source import LEAGUE_ID, LEAGUE_NAME, _fixture_id, resolve_team
from match_statistics import has_complete_statistics, utc_now_iso
from time_utils import parse_iso_datetime


SOURCE_NAME = "Football-Data.co.uk"
BASE_URL = "https://www.football-data.co.uk/mmz4281/{season_code}/G1.csv"
REQUEST_TIMEOUT_SECONDS = 30
ATHENS_TZ = ZoneInfo("Europe/Athens")


@dataclass(frozen=True)
class FootballDataResult:
    fixtures: list[dict[str, Any]]
    statistics: list[dict[str, Any]]
    seasons_requested: list[int]
    seasons_loaded: list[int]
    urls_loaded: list[str]
    rows_loaded: int
    complete_statistics_rows: int
    warnings: list[str]


@dataclass(frozen=True)
class ReconciledFootballData:
    fixtures_saved: int
    statistics: list[dict[str, Any]]
    matched_existing_fixtures: int
    inserted_new_fixtures: int


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any) -> int | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def season_code(season: int) -> str:
    """Μετατρέπει το 2025 σε 2526, όπως ονομάζει τα CSV το Football-Data."""
    return f"{season % 100:02d}{(season + 1) % 100:02d}"


def _parse_date(value: Any) -> date | None:
    text = _clean(value)
    if not text:
        return None

    for pattern in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _parse_time(value: Any) -> time | None:
    text = _clean(value)
    if not text:
        return None

    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    return None


def _local_kickoff(row: dict[str, str]) -> tuple[datetime, bool] | None:
    match_date = _parse_date(row.get("Date"))
    if match_date is None:
        return None

    match_time = _parse_time(row.get("Time"))
    time_confirmed = match_time is not None
    if match_time is None:
        # Τεχνική ώρα μόνο για ταξινόμηση/ταύτιση. Δεν εμφανίζεται ως
        # επίσημη στην εφαρμογή, επειδή το flag παραμένει false.
        match_time = time(hour=12)

    return datetime.combine(match_date, match_time, tzinfo=ATHENS_TZ), time_confirmed


def _value(row: dict[str, str], *names: str) -> int | None:
    for name in names:
        if name in row:
            parsed = _as_int(row.get(name))
            if parsed is not None:
                return parsed
    return None


def _fixture_payload(
    row: dict[str, str],
    *,
    season: int,
    as_of: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    home_raw = _clean(row.get("HomeTeam"))
    away_raw = _clean(row.get("AwayTeam"))
    if not home_raw or not away_raw:
        return None

    kickoff_info = _local_kickoff(row)
    if kickoff_info is None:
        return None
    kickoff_local, time_confirmed = kickoff_info
    kickoff_utc = kickoff_local.astimezone(timezone.utc)

    home_team_id, home_team_name = resolve_team(home_raw)
    away_team_id, away_team_name = resolve_team(away_raw)
    home_goals = _value(row, "FTHG")
    away_goals = _value(row, "FTAG")

    completed = home_goals is not None and away_goals is not None
    if completed:
        status = "FT"
    elif kickoff_utc > as_of.astimezone(timezone.utc):
        status = "NS" if time_confirmed else "TBD"
    else:
        status = "TBD"

    fixture_id = _fixture_id(season, kickoff_local, home_team_name, away_team_name)
    fixture_date = kickoff_utc.isoformat()

    fixture_payload = {
        "fixture": {
            "id": fixture_id,
            "date": fixture_date,
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
        "goals": {"home": home_goals, "away": away_goals},
    }

    if not completed:
        return fixture_payload, None

    statistics_record: dict[str, Any] = {
        "fixture_id": fixture_id,
        "league_id": LEAGUE_ID,
        "season": season,
        "fixture_date": fixture_date,
        "status": "FT",
        "home_team_id": home_team_id,
        "home_team_name": home_team_name,
        "away_team_id": away_team_id,
        "away_team_name": away_team_name,
        "home_corners": _value(row, "HC"),
        "away_corners": _value(row, "AC"),
        "home_yellow_cards": _value(row, "HY"),
        "away_yellow_cards": _value(row, "AY"),
        "home_red_cards": _value(row, "HR"),
        "away_red_cards": _value(row, "AR"),
        "home_total_shots": _value(row, "HS"),
        "away_total_shots": _value(row, "AS"),
        "home_shots_on_target": _value(row, "HST"),
        "away_shots_on_target": _value(row, "AST"),
        "home_fouls": _value(row, "HF"),
        "away_fouls": _value(row, "AF"),
        "home_offsides": _value(row, "HO"),
        "away_offsides": _value(row, "AO"),
        "referee": _clean(row.get("Referee")) or None,
        "statistics_available": False,
        "unavailable_reason": None,
        "source": SOURCE_NAME,
        "collected_at": utc_now_iso(),
    }
    statistics_record["statistics_available"] = has_complete_statistics(
        statistics_record
    )
    if not statistics_record["statistics_available"]:
        statistics_record["unavailable_reason"] = (
            "Το CSV δεν περιείχε πλήρη κόρνερ και κίτρινες κάρτες."
        )

    return fixture_payload, statistics_record


def parse_football_data_csv(
    csv_text: str,
    *,
    season: int,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    fixtures: list[dict[str, Any]] = []
    statistics: list[dict[str, Any]] = []

    field_names = set(reader.fieldnames or [])
    if not {"Date", "HomeTeam", "AwayTeam"}.issubset(field_names):
        raise ValueError(
            "Το CSV Football-Data δεν περιέχει τα πεδία Date/HomeTeam/AwayTeam."
        )

    for row in reader:
        parsed = _fixture_payload(row, season=season, as_of=as_of)
        if parsed is None:
            continue
        fixture, record = parsed
        fixtures.append(fixture)
        if record is not None:
            statistics.append(record)

    return fixtures, statistics


def _decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "iso-8859-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_football_data(
    *,
    seasons: Iterable[int],
    as_of: datetime,
    session: requests.Session | None = None,
) -> FootballDataResult:
    own_session = session is None
    http = session or requests.Session()
    http.headers.update(
        {
            "User-Agent": (
                "BetAnalytic/1.0 (+https://github.com/"
                "georgeaalloim/BetAnalyticBackend)"
            ),
            "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.8",
        }
    )

    requested = sorted(set(int(item) for item in seasons))
    loaded: list[int] = []
    urls_loaded: list[str] = []
    warnings: list[str] = []
    all_fixtures: list[dict[str, Any]] = []
    all_statistics: list[dict[str, Any]] = []

    try:
        for season in requested:
            url = BASE_URL.format(season_code=season_code(season))
            try:
                response = http.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                if response.status_code == 404:
                    warnings.append(
                        f"Δεν έχει δημοσιευτεί ακόμη CSV για τη σεζόν {season}."
                    )
                    continue
                response.raise_for_status()
                fixtures, statistics = parse_football_data_csv(
                    _decode_csv(response.content),
                    season=season,
                    as_of=as_of,
                )
                if not fixtures:
                    warnings.append(
                        f"Το CSV της σεζόν {season} δεν περιείχε αγώνες."
                    )
                    continue

                loaded.append(season)
                urls_loaded.append(str(response.url))
                all_fixtures.extend(fixtures)
                all_statistics.extend(statistics)
            except (requests.RequestException, ValueError) as error:
                warnings.append(f"Αποτυχία Football-Data {url}: {error}")
    finally:
        if own_session:
            http.close()

    fixtures_by_id = {int(item["fixture"]["id"]): item for item in all_fixtures}
    statistics_by_id = {
        int(item["fixture_id"]): item for item in all_statistics
    }
    normalized_fixtures = sorted(
        fixtures_by_id.values(), key=lambda item: str(item["fixture"]["date"])
    )
    normalized_statistics = sorted(
        statistics_by_id.values(),
        key=lambda item: str(item.get("fixture_date") or ""),
    )

    return FootballDataResult(
        fixtures=normalized_fixtures,
        statistics=normalized_statistics,
        seasons_requested=requested,
        seasons_loaded=loaded,
        urls_loaded=urls_loaded,
        rows_loaded=len(normalized_fixtures),
        complete_statistics_rows=sum(
            1 for item in normalized_statistics if has_complete_statistics(item)
        ),
        warnings=warnings,
    )


def _athens_date(value: Any) -> date | None:
    try:
        return parse_iso_datetime(str(value or "")).astimezone(ATHENS_TZ).date()
    except (TypeError, ValueError):
        return None


def reconcile_and_save_football_data(
    result: FootballDataResult,
) -> ReconciledFootballData:
    """
    Ταυτίζει τις εγγραφές CSV με τα ήδη υπάρχοντα fixture IDs.

    Έτσι δεν δημιουργούνται διπλοί ιστορικοί αγώνες όταν το ίδιο ματς έχει
    διαφορετικό ID ή όταν το CSV δεν περιλαμβάνει ακριβή ώρα έναρξης.
    """
    seasons = tuple(result.seasons_loaded)
    existing_index: dict[tuple[int, int, int, date], dict[str, Any]] = {}
    if seasons:
        placeholders = ",".join("?" for _ in seasons)
        with get_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM fixtures
                WHERE league_id = ?
                  AND season IN ({placeholders})
                """,
                (LEAGUE_ID, *seasons),
            ).fetchall()
        for row in rows:
            item = dict(row)
            local_date = _athens_date(item.get("fixture_date"))
            if local_date is None:
                continue
            existing_index[
                (
                    int(item["season"]),
                    int(item["home_team_id"]),
                    int(item["away_team_id"]),
                    local_date,
                )
            ] = item

    id_mapping: dict[int, int] = {}
    canonical_date: dict[int, str] = {}
    fixtures_to_save: list[dict[str, Any]] = []
    matched = 0
    inserted = 0

    for payload in result.fixtures:
        fixture = dict(payload["fixture"])
        league = payload["league"]
        teams = payload["teams"]
        external_id = int(fixture["id"])
        local_date = _athens_date(fixture.get("date"))
        key = (
            int(league["season"]),
            int(teams["home"]["id"]),
            int(teams["away"]["id"]),
            local_date,
        )
        existing = existing_index.get(key) if local_date is not None else None

        if existing is not None:
            canonical_id = int(existing["fixture_id"])
            fixture["id"] = canonical_id
            # A completed Football-Data row may contain a more accurate kickoff
            # than an earlier date-only schedule record. Keep the existing date
            # only when it was already confirmed and the incoming row is not.
            existing_confirmed = bool(existing.get("kickoff_time_confirmed"))
            incoming_confirmed = bool(fixture.get("time_confirmed"))
            if existing_confirmed and not incoming_confirmed:
                canonical_date[canonical_id] = str(
                    existing.get("fixture_date") or fixture.get("date") or ""
                )
            else:
                canonical_date[canonical_id] = str(fixture.get("date") or "")
            matched += 1
        else:
            canonical_id = external_id
            inserted += 1

        id_mapping[external_id] = canonical_id
        fixtures_to_save.append({**payload, "fixture": fixture})

    fixtures_saved = save_fixtures(fixtures_to_save)

    reconciled_statistics: list[dict[str, Any]] = []
    for record in result.statistics:
        external_id = int(record["fixture_id"])
        canonical_id = id_mapping.get(external_id, external_id)
        normalized = dict(record)
        normalized["fixture_id"] = canonical_id
        if canonical_id in canonical_date and canonical_date[canonical_id]:
            normalized["fixture_date"] = canonical_date[canonical_id]
        reconciled_statistics.append(normalized)

    return ReconciledFootballData(
        fixtures_saved=fixtures_saved,
        statistics=reconciled_statistics,
        matched_existing_fixtures=matched,
        inserted_new_fixtures=inserted,
    )
