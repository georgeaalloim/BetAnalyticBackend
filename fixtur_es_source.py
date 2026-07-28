from __future__ import annotations

import html
import re
import zlib
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from database import get_connection, save_fixtures


ATHENS_TZ = ZoneInfo("Europe/Athens")
LEAGUE_ID = 197
LEAGUE_NAME = "Super League 1"

# Η νέα σεζόν εμφανίζεται στο icx, ενώ η παλαιότερη ενεργή σεζόν
# παραμένει συνήθως διαθέσιμη στο βασικό fixtur.es. Αν και οι δύο
# διευθύνσεις δείχνουν την ίδια σεζόν, τα διπλότυπα αφαιρούνται.
LANDING_PAGES = (
    "https://www.icx.fixtur.es/en/super-league-greece",
    "https://fixtur.es/en/matches/super-league-greece",
)

REQUEST_TIMEOUT_SECONDS = 30
SYNTHETIC_FIXTURE_ID_MIN = 1_200_000_000
SYNTHETIC_FIXTURE_ID_SPAN = 800_000_000
SYNTHETIC_TEAM_ID_MIN = 1_000_000_000
SYNTHETIC_TEAM_ID_SPAN = 150_000_000

# Αντιστοίχιση των ονομάτων του Fixtur.es στα IDs που χρησιμοποιεί ήδη
# η ιστορική βάση του API-Football. Έτσι το μοντέλο αναγνωρίζει τις
# ίδιες ομάδες σε διαφορετικές πηγές δεδομένων.
TEAM_ALIASES: dict[str, tuple[int, str]] = {
    # AEK
    "aek": (575, "AEK Athens FC"),
    "aek athen": (575, "AEK Athens FC"),
    "aek athene": (575, "AEK Athens FC"),
    "aek athens": (575, "AEK Athens FC"),
    "aek athens fc": (575, "AEK Athens FC"),
    "α ε κ": (575, "AEK Athens FC"),
    "αεκ": (575, "AEK Athens FC"),
    # Aris
    "aris": (1123, "Aris Thessalonikis"),
    "aris saloniki": (1123, "Aris Thessalonikis"),
    "aris thessalonikis": (1123, "Aris Thessalonikis"),
    "αρης": (1123, "Aris Thessalonikis"),
    # Asteras
    "asteras aktor": (955, "Asteras Tripolis"),
    "asteras tripolis": (955, "Asteras Tripolis"),
    "αστερας aktor": (955, "Asteras Tripolis"),
    "αστερας τριπολης": (955, "Asteras Tripolis"),
    # Atromitos
    "atromitos": (12260, "Atromitos"),
    "atromitos ath": (12260, "Atromitos"),
    "atromitos athens": (12260, "Atromitos"),
    "ατρομητος": (12260, "Atromitos"),
    "ατρομητος αθηνων": (12260, "Atromitos"),
    # Other clubs
    "iraklis": (1026357653, "Iraklis 1908"),
    "iraklis 1908": (1026357653, "Iraklis 1908"),
    "pot iraklis": (1026357653, "Iraklis 1908"),
    "π ο τ ηρακλης": (1026357653, "Iraklis 1908"),
    "ηρακλης": (1026357653, "Iraklis 1908"),
    "kalamata": (1068316644, "Kalamata"),
    "καλαματα": (1068316644, "Kalamata"),
    "kallithea": (2095, "Kallithea"),
    "athens kallithea": (2095, "Kallithea"),
    "athens kallithea fc": (2095, "Kallithea"),
    "καλλιθεα": (2095, "Kallithea"),
    "kifisia": (5050, "Kifisia"),
    "ae kifisia": (5050, "Kifisia"),
    "ae kifisias": (5050, "Kifisia"),
    "α ε κηφισια": (5050, "Kifisia"),
    "κηφισια": (5050, "Kifisia"),
    "ionikos": (7513, "Ionikos"),
    "ionikos nikeas": (7513, "Ionikos"),
    "ιωνικος": (7513, "Ionikos"),
    "lamia": (956, "Lamia"),
    "πας λαμια": (956, "Lamia"),
    "larissa": (951, "Larissa"),
    "ael": (951, "Larissa"),
    "ae larissa": (951, "Larissa"),
    "ael larissa": (951, "Larissa"),
    "ael novibet": (951, "Larissa"),
    "αελ": (951, "Larissa"),
    "αελ novibet": (951, "Larissa"),
    "levadiakos": (957, "Levadiakos"),
    "λεβαδειακος": (957, "Levadiakos"),
    "ofi": (1124, "OFI"),
    "ofi crete": (1124, "OFI"),
    "ofi heraklion": (1124, "OFI"),
    "ο φ η": (1124, "OFI"),
    "οφη": (1124, "OFI"),
    "olympiakos": (553, "Olympiakos Piraeus"),
    "olympiakos piraeus": (553, "Olympiakos Piraeus"),
    "olympiacos": (553, "Olympiakos Piraeus"),
    "ολυμπιακος": (553, "Olympiakos Piraeus"),
    "ολυμπιακος σ φ π": (553, "Olympiakos Piraeus"),
    "paok": (619, "PAOK"),
    "paok salonika": (619, "PAOK"),
    "paok saloniki": (619, "PAOK"),
    "π α ο κ": (619, "PAOK"),
    "παοκ": (619, "PAOK"),
    "pas giannina": (950, "PAS Giannina"),
    "giannina": (950, "PAS Giannina"),
    "πας γιαννινα": (950, "PAS Giannina"),
    "panathinaikos": (617, "Panathinaikos"),
    "panathinaikos ao": (617, "Panathinaikos"),
    "παναθηναικος": (617, "Panathinaikos"),
    "παναθηναικος α ο": (617, "Panathinaikos"),
    "panetolikos": (949, "Panetolikos"),
    "παναιτωλικος": (949, "Panetolikos"),
    "panserraikos": (2099, "Panserraikos"),
    "panserraikos 1946": (2099, "Panserraikos"),
    "πανσερραικος": (2099, "Panserraikos"),
    "πανσερραικος 1946": (2099, "Panserraikos"),
    "volos": (2110, "Volos NFC"),
    "volos nfc": (2110, "Volos NFC"),
    "volos nps": (2110, "Volos NFC"),
    "βολος": (2110, "Volos NFC"),
    "βολος ν π σ": (2110, "Volos NFC"),
}

_SCORE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,2})\s*[\-–—:]\s*(\d{1,2})(?!\d)"
)
_MATCH_SEPARATOR_PATTERN = re.compile(r"\s+[\-–—]\s+")
_SPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class SourceResult:
    fixtures: list[dict[str, Any]]
    pages_checked: list[str]
    calendar_urls: list[str]
    warnings: list[str]


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\\n", " ").replace("\n", " ")
    return _SPACE_PATTERN.sub(" ", text).strip()


def _normalize_team_key(name: str) -> str:
    value = _clean_text(name).casefold()
    # Αφαιρούμε τόνους, τελείες και εταιρικά/ποδοσφαιρικά επιθήματα ώστε
    # οι ίδιες ομάδες να ταυτίζονται μεταξύ διαφορετικών πηγών.
    value = "".join(
        character
        for character in unicodedata.normalize("NFD", value)
        if unicodedata.category(character) != "Mn"
    )
    value = re.sub(r"\b(f\.?c\.?|p\.?a\.?e\.?)\b", " ", value)
    value = re.sub(r"[^a-z0-9α-ω]+", " ", value)
    return _SPACE_PATTERN.sub(" ", value).strip()


def _stable_positive_id(text: str, minimum: int, span: int) -> int:
    checksum = zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF
    return minimum + checksum % span


_NORMALIZED_TEAM_ALIASES = {
    _normalize_team_key(alias): value
    for alias, value in TEAM_ALIASES.items()
}


def resolve_team(name: str) -> tuple[int, str]:
    cleaned = _clean_text(name)
    key = _normalize_team_key(cleaned)
    if key in _NORMALIZED_TEAM_ALIASES:
        return _NORMALIZED_TEAM_ALIASES[key]

    return (
        _stable_positive_id(
            f"team|{key}",
            SYNTHETIC_TEAM_ID_MIN,
            SYNTHETIC_TEAM_ID_SPAN,
        ),
        cleaned,
    )


def season_from_local_date(local_date: date) -> int:
    return local_date.year if local_date.month >= 7 else local_date.year - 1


def _as_athens_datetime(value: date | datetime) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ATHENS_TZ), False
        return value.astimezone(ATHENS_TZ), False

    # Ημερομηνία χωρίς ώρα: χρησιμοποιούμε 12:00 μόνο ως τεχνικό σημείο
    # ταξινόμησης και χαρακτηρίζουμε τον αγώνα TBD, ώστε η εφαρμογή να
    # μπορεί να κρύψει την ώρα μέχρι να οριστεί επίσημα.
    return datetime.combine(value, time(hour=12), tzinfo=ATHENS_TZ), True


def _extract_score(*texts: str) -> tuple[int, int] | None:
    for text_value in texts:
        match = _SCORE_PATTERN.search(_clean_text(text_value))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _strip_score(text_value: str) -> str:
    return _SPACE_PATTERN.sub(
        " ",
        _SCORE_PATTERN.sub(" ", _clean_text(text_value)),
    ).strip(" -–—:|")


def _extract_teams(*texts: str) -> tuple[str, str] | None:
    for text_value in texts:
        cleaned = _strip_score(text_value)
        cleaned = re.sub(
            r"\b(super league greece|super league 1|round\s+\d+)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = _SPACE_PATTERN.sub(" ", cleaned).strip(" -–—:|")
        parts = _MATCH_SEPARATOR_PATTERN.split(cleaned, maxsplit=1)
        if len(parts) != 2:
            continue

        home_name, away_name = (part.strip() for part in parts)
        if not home_name or not away_name:
            continue
        if home_name.isdigit() or away_name.isdigit():
            continue
        return home_name, away_name

    return None


def _fixture_status(
    kickoff_local: datetime,
    date_only: bool,
    score: tuple[int, int] | None,
    event_status: str,
    text: str,
    as_of: datetime,
) -> tuple[str, int | None, int | None]:
    normalized_status = _clean_text(event_status).upper()
    normalized_text = _clean_text(text).casefold()

    if normalized_status == "CANCELLED" or any(
        marker in normalized_text
        for marker in ("postponed", "cancelled", "canceled", "αναβ")
    ):
        return "PST", None, None

    as_of_athens = as_of.astimezone(ATHENS_TZ)
    safely_finished = (
        kickoff_local.date() < as_of_athens.date()
        if date_only
        else as_of_athens >= kickoff_local + timedelta(hours=4)
    )

    if score is not None and safely_finished:
        return "FT", score[0], score[1]

    if kickoff_local > as_of_athens:
        return ("TBD" if date_only else "NS"), None, None

    # Δεν εισάγουμε σκορ αγώνα που ίσως βρίσκεται ακόμη σε εξέλιξη.
    return "LIVE", None, None


def _fixture_id(
    season: int,
    kickoff_local: datetime,
    home_name: str,
    away_name: str,
) -> int:
    key = "|".join(
        (
            str(season),
            kickoff_local.date().isoformat(),
            _normalize_team_key(home_name),
            _normalize_team_key(away_name),
        )
    )
    return _stable_positive_id(
        f"fixture|{key}",
        SYNTHETIC_FIXTURE_ID_MIN,
        SYNTHETIC_FIXTURE_ID_SPAN,
    )


def _to_api_fixture(
    kickoff_local: datetime,
    date_only: bool,
    home_name: str,
    away_name: str,
    score: tuple[int, int] | None,
    event_status: str,
    source_text: str,
    as_of: datetime,
) -> dict[str, Any]:
    season = season_from_local_date(kickoff_local.date())
    home_team_id, canonical_home_name = resolve_team(home_name)
    away_team_id, canonical_away_name = resolve_team(away_name)
    status, home_goals, away_goals = _fixture_status(
        kickoff_local=kickoff_local,
        date_only=date_only,
        score=score,
        event_status=event_status,
        text=source_text,
        as_of=as_of,
    )

    kickoff_utc = kickoff_local.astimezone(timezone.utc)
    return {
        "fixture": {
            "id": _fixture_id(
                season,
                kickoff_local,
                canonical_home_name,
                canonical_away_name,
            ),
            "date": kickoff_utc.isoformat(),
            "status": {"short": status},
            "time_confirmed": not date_only,
            "source": "Fixtur.es calendar feed",
        },
        "league": {
            "id": LEAGUE_ID,
            "name": LEAGUE_NAME,
            "season": season,
        },
        "teams": {
            "home": {
                "id": home_team_id,
                "name": canonical_home_name,
            },
            "away": {
                "id": away_team_id,
                "name": canonical_away_name,
            },
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
    }


def _candidate_calendar_urls(page_url: str, page_html: str) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    candidates: list[tuple[int, str]] = []

    for link in soup.find_all("a", href=True):
        raw_href = html.unescape(str(link.get("href") or "")).strip()
        if not raw_href:
            continue

        href = raw_href.replace("webcal://", "https://")
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        lowered = absolute.casefold()

        score = 0
        if "ics.fixtur.es" in parsed.netloc.casefold():
            score += 100
        if lowered.endswith(".ics") or ".ics?" in lowered:
            score += 80
        if "super-league-greece" in lowered:
            score += 40
        if "calendar" in lowered or "ical" in lowered:
            score += 10
        if any(host in parsed.netloc.casefold() for host in ("google.", "outlook.")):
            score -= 100

        if score > 0:
            candidates.append((score, absolute))

    # Επιπλέον fallback για URL που βρίσκεται σε script ή data attribute.
    for match in re.findall(
        r"(?:webcal|https?)://[^\"'<>\s]+",
        page_html,
        flags=re.IGNORECASE,
    ):
        candidate = html.unescape(match).replace("webcal://", "https://")
        lowered = candidate.casefold()
        if "ics.fixtur.es" in lowered or ".ics" in lowered:
            candidates.append((120, candidate))

    ordered: list[str] = []
    for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _unfold_ics_lines(calendar_bytes: bytes) -> list[str]:
    text = calendar_bytes.decode("utf-8-sig", errors="replace")
    physical_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    logical_lines: list[str] = []
    for line in physical_lines:
        if line.startswith((" ", "\t")) and logical_lines:
            logical_lines[-1] += line[1:]
        else:
            logical_lines.append(line)
    return logical_lines


def _unescape_ics_text(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ics_dtstart(
    property_name: str,
    raw_value: str,
) -> tuple[datetime, bool] | None:
    parameters: dict[str, str] = {}
    parts = property_name.split(";")
    for parameter in parts[1:]:
        if "=" not in parameter:
            continue
        key, value = parameter.split("=", 1)
        parameters[key.upper()] = value

    value = raw_value.strip()
    date_only = parameters.get("VALUE", "").upper() == "DATE" or (
        len(value) == 8 and value.isdigit()
    )

    if date_only:
        try:
            parsed_date = datetime.strptime(value[:8], "%Y%m%d").date()
        except ValueError:
            return None
        return _as_athens_datetime(parsed_date)

    timezone_name = parameters.get("TZID")
    event_tz = ATHENS_TZ
    if timezone_name:
        try:
            event_tz = ZoneInfo(timezone_name)
        except Exception:
            event_tz = ATHENS_TZ

    formats = (
        "%Y%m%dT%H%M%SZ",
        "%Y%m%dT%H%M%S",
        "%Y%m%dT%H%MZ",
        "%Y%m%dT%H%M",
    )
    for value_format in formats:
        try:
            parsed = datetime.strptime(value, value_format)
        except ValueError:
            continue

        if value.endswith("Z"):
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.replace(tzinfo=event_tz)
        return parsed.astimezone(ATHENS_TZ), False

    return None


def _parse_calendar(
    calendar_bytes: bytes,
    as_of: datetime,
) -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    current_event: dict[str, tuple[str, str]] | None = None

    for line in _unfold_ics_lines(calendar_bytes):
        upper_line = line.upper()
        if upper_line == "BEGIN:VEVENT":
            current_event = {}
            continue
        if upper_line == "END:VEVENT":
            if current_event is None:
                continue

            dtstart_item = current_event.get("DTSTART")
            if dtstart_item is not None:
                parsed_dtstart = _parse_ics_dtstart(
                    dtstart_item[0],
                    dtstart_item[1],
                )
                if parsed_dtstart is not None:
                    summary = _clean_text(
                        _unescape_ics_text(
                            current_event.get("SUMMARY", ("", ""))[1]
                        )
                    )
                    description = _clean_text(
                        _unescape_ics_text(
                            current_event.get("DESCRIPTION", ("", ""))[1]
                        )
                    )
                    location = _clean_text(
                        _unescape_ics_text(
                            current_event.get("LOCATION", ("", ""))[1]
                        )
                    )
                    event_status = _clean_text(
                        current_event.get("STATUS", ("", ""))[1]
                    )
                    combined_text = " | ".join(
                        text
                        for text in (summary, description, location)
                        if text
                    )
                    teams = _extract_teams(summary, description)
                    if teams is not None:
                        score = _extract_score(summary, description)
                        fixtures.append(
                            _to_api_fixture(
                                kickoff_local=parsed_dtstart[0],
                                date_only=parsed_dtstart[1],
                                home_name=teams[0],
                                away_name=teams[1],
                                score=score,
                                event_status=event_status,
                                source_text=combined_text,
                                as_of=as_of,
                            )
                        )

            current_event = None
            continue

        if current_event is None or ":" not in line:
            continue

        property_name, raw_value = line.split(":", 1)
        base_name = property_name.split(";", 1)[0].upper()
        if base_name in {
            "DTSTART",
            "SUMMARY",
            "DESCRIPTION",
            "LOCATION",
            "STATUS",
        }:
            current_event[base_name] = (property_name, raw_value)

    return fixtures


def _parse_datetime_text(text_value: str) -> tuple[datetime, bool] | None:
    cleaned = _clean_text(text_value)
    cleaned = re.sub(
        r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|"
        r"Thursday|Friday|Saturday|Sunday)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    formats = (
        "%d %b %Y %H:%M %z",
        "%d %B %Y %H:%M %z",
        "%d %b %Y %H:%M",
        "%d %B %Y %H:%M",
        "%d %b %Y",
        "%d %B %Y",
    )
    for value_format in formats:
        try:
            parsed = datetime.strptime(cleaned, value_format)
        except ValueError:
            continue

        date_only = "%H" not in value_format
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ATHENS_TZ)
        else:
            parsed = parsed.astimezone(ATHENS_TZ)
        if date_only:
            parsed = parsed.replace(hour=12, minute=0, second=0, microsecond=0)
        return parsed, date_only

    return None


def _parse_html_fallback(
    page_html: str,
    as_of: datetime,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page_html, "html.parser")
    fixtures: list[dict[str, Any]] = []

    # Πρώτα δοκιμάζουμε γραμμές πίνακα.
    for row in soup.find_all("tr"):
        cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
        cells = [cell for cell in cells if cell]
        if not cells:
            continue

        date_value: tuple[datetime, bool] | None = None
        teams: tuple[str, str] | None = None
        score: tuple[int, int] | None = None
        for cell in cells:
            date_value = date_value or _parse_datetime_text(cell)
            teams = teams or _extract_teams(cell)
            score = score or _extract_score(cell)

        if date_value is None or teams is None:
            continue

        fixtures.append(
            _to_api_fixture(
                kickoff_local=date_value[0],
                date_only=date_value[1],
                home_name=teams[0],
                away_name=teams[1],
                score=score,
                event_status="",
                source_text=" | ".join(cells),
                as_of=as_of,
            )
        )

    if fixtures:
        return fixtures

    # Fallback σε διαδοχικές ορατές συμβολοσειρές.
    strings = [_clean_text(value) for value in soup.stripped_strings]
    for index, value in enumerate(strings):
        date_value = _parse_datetime_text(value)
        if date_value is None:
            continue

        window = strings[index + 1 : index + 7]
        teams: tuple[str, str] | None = None
        score: tuple[int, int] | None = None
        for candidate in window:
            teams = teams or _extract_teams(candidate)
            score = score or _extract_score(candidate)

        if teams is None:
            continue

        fixtures.append(
            _to_api_fixture(
                kickoff_local=date_value[0],
                date_only=date_value[1],
                home_name=teams[0],
                away_name=teams[1],
                score=score,
                event_status="",
                source_text=" | ".join(window),
                as_of=as_of,
            )
        )

    return fixtures


def _deduplicate(fixtures: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for fixture in fixtures:
        fixture_id = int(fixture["fixture"]["id"])
        previous = by_id.get(fixture_id)
        if previous is None:
            by_id[fixture_id] = fixture
            continue

        previous_status = previous["fixture"]["status"]["short"]
        current_status = fixture["fixture"]["status"]["short"]
        if previous_status != "FT" and current_status == "FT":
            by_id[fixture_id] = fixture

    return sorted(
        by_id.values(),
        key=lambda item: str(item["fixture"]["date"]),
    )


def fetch_super_league_fixtures(
    as_of: datetime,
    landing_pages: Iterable[str] = LANDING_PAGES,
) -> SourceResult:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "BetAnalytic/1.0 (+https://github.com/"
                "georgeaalloim/BetAnalyticBackend)"
            ),
            "Accept": "text/html,application/xhtml+xml,"
            "text/calendar;q=0.9,*/*;q=0.8",
        }
    )

    all_fixtures: list[dict[str, Any]] = []
    pages_checked: list[str] = []
    calendar_urls: list[str] = []
    warnings: list[str] = []

    for landing_page in landing_pages:
        try:
            response = session.get(
                landing_page,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            pages_checked.append(response.url)
        except requests.RequestException as error:
            warnings.append(f"Αποτυχία σελίδας {landing_page}: {error}")
            continue

        page_html = response.text
        page_fixtures: list[dict[str, Any]] = []

        for calendar_url in _candidate_calendar_urls(response.url, page_html):
            try:
                calendar_response = session.get(
                    calendar_url,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                calendar_response.raise_for_status()
                if b"BEGIN:VCALENDAR" not in calendar_response.content[:4096]:
                    continue

                parsed = _parse_calendar(calendar_response.content, as_of)
                if not parsed:
                    continue

                calendar_urls.append(calendar_response.url)
                page_fixtures.extend(parsed)
                break
            except (requests.RequestException, ValueError) as error:
                warnings.append(
                    f"Αποτυχία ημερολογίου {calendar_url}: {error}"
                )

        if not page_fixtures:
            page_fixtures = _parse_html_fallback(page_html, as_of)
            if not page_fixtures:
                warnings.append(
                    f"Δεν αναγνωρίστηκαν αγώνες στη σελίδα {response.url}."
                )

        all_fixtures.extend(page_fixtures)

    fixtures = _deduplicate(all_fixtures)
    if not fixtures:
        raise RuntimeError(
            "Το Fixtur.es δεν επέστρεψε αναγνωρίσιμους αγώνες. "
            "Το workflow σταμάτησε για να μη δημοσιευτεί κενό feed."
        )

    return SourceResult(
        fixtures=fixtures,
        pages_checked=pages_checked,
        calendar_urls=calendar_urls,
        warnings=warnings,
    )


def replace_source_fixtures(fixtures: list[dict[str, Any]]) -> int:
    seasons = sorted(
        {
            int(item["league"]["season"])
            for item in fixtures
        }
    )
    if not seasons:
        return 0

    placeholders = ",".join("?" for _ in seasons)
    with get_connection() as connection:
        connection.execute(
            f"""
            DELETE FROM fixtures
            WHERE fixture_id >= ?
              AND season IN ({placeholders})
            """,
            (SYNTHETIC_FIXTURE_ID_MIN, *seasons),
        )
        connection.commit()

    return save_fixtures(fixtures)
