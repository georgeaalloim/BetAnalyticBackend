from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from database import get_connection, initialize_database, save_fixture_goal_scorers
from goal_scorer_enricher import _names_match
from time_utils import parse_iso_datetime, to_iso_z, utc_now


SOURCE_NAME = "OFStats.com scorer fallback"
BASE_URL = "https://ofstats.com"
LEAGUE_ID = 197
DEFAULT_RECENT_DAYS = 10
DEFAULT_MAX_MATCHES = 10
REQUEST_TIMEOUT_SECONDS = 20

# Provider-facing slugs only. Canonical BetAnalytic names/IDs stay unchanged.
TEAM_SLUGS: dict[int, tuple[str, ...]] = {
    575: ("aek-athens",),
    1123: ("aris",),
    955: ("asteras-tripolis",),
    12260: ("atromitos",),
    1026357653: ("iraklis-thessaloniki", "iraklis"),
    1068316644: ("kalamata",),
    5050: ("kifisia",),
    957: ("levadiakos",),
    1124: ("ofi",),
    553: ("olympiacos-fc", "olympiacos"),
    617: ("panathinaikos",),
    949: ("panaitolikos", "panetolikos"),
    619: ("paok",),
    2110: ("volos-nfc", "volos"),
}


@dataclass
class OFStatsScorerResult:
    enabled: bool
    completed_matches_considered: int
    matches_searched: int
    matches_found: int
    matches_saved: int
    requests_used: int
    pending_matches: int
    warnings: list[str] = field(default_factory=list)


def _slugify(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"\b(fc|nfc|cf|ac|f\.c\.)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return re.sub(r"-+", "-", text)


def _team_slugs(team_id: int, name: str) -> tuple[str, ...]:
    raw = [*TEAM_SLUGS.get(int(team_id), ()), _slugify(name)]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip("-")
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return tuple(out)


def _pending_rows(
    *,
    season: int | None,
    recent_days: int,
    max_matches: int,
    as_of: datetime,
) -> list[dict[str, Any]]:
    cutoff = as_of.astimezone(timezone.utc) - timedelta(days=max(1, int(recent_days)))
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

    selected: list[dict[str, Any]] = []
    for row in rows:
        try:
            fixture_time = parse_iso_datetime(str(row.get("fixture_date") or ""))
        except ValueError:
            continue
        if fixture_time.astimezone(timezone.utc) < cutoff:
            continue
        selected.append(row)
        if len(selected) >= max(0, int(max_matches)):
            break
    return selected


def _get_text(session: requests.Session, url: str) -> tuple[str | None, str, str | None]:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text, str(getattr(response, "url", url) or url), None
    except requests.RequestException as exc:
        return None, url, str(exc)


def _fixture_datetime(row: dict[str, Any]) -> datetime | None:
    try:
        return parse_iso_datetime(str(row.get("fixture_date") or ""))
    except ValueError:
        return None


def _fixture_date_strings(row: dict[str, Any]) -> tuple[str, ...]:
    dt = _fixture_datetime(row)
    if dt is None:
        return ()
    local = dt.astimezone(timezone(timedelta(hours=3)))
    return (
        local.strftime("%d.%m.%Y"),
        local.strftime("%d/%m/%Y"),
        local.strftime("%Y-%m-%d"),
    )


def _page_matches_fixture(html: str, row: dict[str, Any]) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.stripped_strings)
    if "finished" not in text.casefold():
        return False

    expected_home = int(row["home_goals"])
    expected_away = int(row["away_goals"])
    if not re.search(rf"(?<!\d){expected_home}\s*:\s*{expected_away}(?!\d)", text):
        return False

    pieces = [item.strip() for item in soup.stripped_strings if item.strip()]
    home_ok = any(
        _names_match(piece, int(row["home_team_id"]), str(row["home_team_name"]))
        for piece in pieces
    )
    away_ok = any(
        _names_match(piece, int(row["away_team_id"]), str(row["away_team_name"]))
        for piece in pieces
    )
    if not (home_ok and away_ok):
        return False

    dates = _fixture_date_strings(row)
    return not dates or any(value in text for value in dates)


def _candidate_links_from_team_page(html: str, row: dict[str, Any]) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    home_slugs = _team_slugs(int(row["home_team_id"]), str(row["home_team_name"]))
    away_slugs = _team_slugs(int(row["away_team_id"]), str(row["away_team_name"]))
    out: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if "/matches/" not in href:
            continue
        lowered = href.casefold()
        if not any(slug in lowered for slug in home_slugs):
            continue
        if not any(slug in lowered for slug in away_slugs):
            continue
        absolute = urljoin(BASE_URL + "/", href)
        absolute = re.sub(
            r"/matches/(?:preview|h2h|statistics|season|referee)/",
            "/matches/view/",
            absolute,
            count=1,
        )
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _constructed_match_urls(row: dict[str, Any]) -> list[str]:
    dt = _fixture_datetime(row)
    if dt is None:
        return []
    local_date = dt.astimezone(timezone(timedelta(hours=3))).date()
    home_slugs = _team_slugs(int(row["home_team_id"]), str(row["home_team_name"]))
    away_slugs = _team_slugs(int(row["away_team_id"]), str(row["away_team_name"]))

    out: list[str] = []
    seen: set[str] = set()
    # OFStats can keep the original scheduled date in the URL after rescheduling.
    # Kifisia-AEK on 30/08/2026 is currently exposed under a 29/08 slug.
    for delta in (0, -1, 1, -2, 2, -3, 3):
        date_slug = (local_date + timedelta(days=delta)).isoformat()
        for home in home_slugs[:2]:
            for away in away_slugs[:2]:
                url = f"{BASE_URL}/matches/view/{home}-{away}-{date_slug}"
                if url not in seen:
                    seen.add(url)
                    out.append(url)
                if len(out) >= 14:
                    return out
    return out


_GOAL_RE = re.compile(
    r"\bGoal!\s+(.*?)\s+(?:scores?|converts?|heads?|strikes?|fires?|nets?)\b"
    r".*?\b(?:make(?:s)?\s+it|to\s+make\s+it)\s*(\d+)\s*[-:]\s*(\d+)",
    re.IGNORECASE,
)
_OWN_GOAL_RE = re.compile(
    r"\bGoal!\s+Own\s+goal\s+by\s+(.*?)\b.*?"
    r"(?:make(?:s)?\s+it|to\s+make\s+it)\s*(\d+)\s*[-:]\s*(\d+)",
    re.IGNORECASE,
)
_MINUTE_RE = re.compile(r"(?<!\d)(\d{1,3})(?:\s*\+\s*(\d{1,2}))?\s*['’]?")


def _parse_goal_text(text: str) -> tuple[str, int, int, str] | None:
    compact = " ".join(str(text or "").split())
    own = _OWN_GOAL_RE.search(compact)
    if own:
        return own.group(1).strip(" .,-"), int(own.group(2)), int(own.group(3)), "Own Goal"

    match = _GOAL_RE.search(compact)
    if not match:
        return None
    player = match.group(1).strip(" .,-")
    detail = "Penalty" if "penalt" in compact.casefold() else "Goal"
    return player, int(match.group(2)), int(match.group(3)), detail


def _parse_scorers(html: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    raw_goals: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    nodes = [*soup.find_all("tr"), *soup.find_all("li")]
    nodes.extend(
        tag for tag in soup.find_all(["div", "p"])
        if "Goal!" in tag.get_text(" ", strip=True)
    )

    for node in nodes:
        text = node.get_text(" ", strip=True)
        if "Goal!" not in text:
            continue
        parsed = _parse_goal_text(text)
        if parsed is None:
            continue
        player, score_home, score_away, detail = parsed

        prefix = text.split("Goal!", 1)[0]
        minute_matches = list(_MINUTE_RE.finditer(prefix))
        if not minute_matches:
            continue
        minute_match = minute_matches[-1]
        minute = int(minute_match.group(1))
        extra = int(minute_match.group(2)) if minute_match.group(2) else None

        key = (player.casefold(), minute, extra, score_home, score_away)
        if key in seen:
            continue
        seen.add(key)
        raw_goals.append({
            "player_name": player,
            "minute": minute,
            "extra_minute": extra,
            "score_home": score_home,
            "score_away": score_away,
            "detail": detail,
        })

    raw_goals.sort(key=lambda item: (
        int(item["minute"]),
        int(item["extra_minute"] or 0),
        int(item["score_home"]) + int(item["score_away"]),
    ))

    previous_home = 0
    previous_away = 0
    scorers: list[dict[str, Any]] = []

    for item in raw_goals:
        score_home = int(item["score_home"])
        score_away = int(item["score_away"])
        if score_home == previous_home + 1 and score_away == previous_away:
            side = "home"
        elif score_away == previous_away + 1 and score_home == previous_home:
            side = "away"
        else:
            continue

        scorers.append({
            "player_name": str(item["player_name"]),
            "side": side,
            "team_id": int(row["home_team_id"] if side == "home" else row["away_team_id"]),
            "team_name": str(row["home_team_name"] if side == "home" else row["away_team_name"]),
            "minute": int(item["minute"]),
            "extra_minute": item["extra_minute"],
            "detail": str(item["detail"]),
        })
        previous_home = score_home
        previous_away = score_away

    expected_home = int(row["home_goals"])
    expected_away = int(row["away_goals"])
    if previous_home != expected_home or previous_away != expected_away:
        return []
    if len(scorers) != expected_home + expected_away:
        return []
    return scorers


def _find_match_page(
    session: requests.Session,
    row: dict[str, Any],
) -> tuple[str | None, str | None, int, list[str]]:
    requests_used = 0
    warnings: list[str] = []
    candidates: list[str] = []
    seen: set[str] = set()

    for slug in _team_slugs(int(row["home_team_id"]), str(row["home_team_name"]))[:2]:
        team_url = f"{BASE_URL}/team/{slug}"
        html, _, error = _get_text(session, team_url)
        requests_used += 1
        if error:
            warnings.append(f"OFStats team page απέτυχε ({team_url}): {error}")
            continue
        if html:
            for url in _candidate_links_from_team_page(html, row):
                if url not in seen:
                    seen.add(url)
                    candidates.append(url)
        if candidates:
            break

    for url in _constructed_match_urls(row):
        if url not in seen:
            seen.add(url)
            candidates.append(url)

    for url in candidates[:16]:
        html, final_url, error = _get_text(session, url)
        requests_used += 1
        if error:
            continue
        if html and _page_matches_fixture(html, row):
            return html, final_url, requests_used, warnings

    return None, None, requests_used, warnings


def enrich_goal_scorers_from_ofstats(
    *,
    season: int | None = None,
    recent_days: int = DEFAULT_RECENT_DAYS,
    max_matches: int = DEFAULT_MAX_MATCHES,
    as_of: datetime | None = None,
    session: requests.Session | None = None,
) -> OFStatsScorerResult:
    """Fill scorer rows still missing after the primary TheSportsDB pass.

    A scorer set is accepted only if the page matches the fixture date, both
    teams and exact FT score, and the goal progression reconstructs that score.
    The database layer performs the final strict scorer-count verification too.
    """
    initialize_database()
    current = as_of or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    rows = _pending_rows(
        season=season,
        recent_days=recent_days,
        max_matches=max_matches,
        as_of=current,
    )
    if not rows:
        return OFStatsScorerResult(True, 0, 0, 0, 0, 0, 0, [])

    own_session = session is None
    http = session or requests.Session()
    http.headers.update({
        "User-Agent": "BetAnalytic/1.2 (+free scorer fallback)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
    })

    requests_used = 0
    matches_found = 0
    matches_saved = 0
    warnings: list[str] = []

    try:
        for row in rows:
            html, page_url, used, search_warnings = _find_match_page(http, row)
            requests_used += used
            warnings.extend(search_warnings)
            if not html or not page_url:
                warnings.append(
                    "Pending scorers: OFStats match page δεν βρέθηκε για "
                    f"{row['home_team_name']} - {row['away_team_name']}."
                )
                continue

            matches_found += 1
            scorers = _parse_scorers(html, row)
            expected = int(row["home_goals"]) + int(row["away_goals"])
            if len(scorers) != expected:
                warnings.append(
                    "Pending scorers: OFStats goal events ελλιπή/ασύμφωνα για "
                    f"{row['home_team_name']} - {row['away_team_name']} "
                    f"(βρέθηκαν {len(scorers)}, αναμένονται {expected})."
                )
                continue

            saved = save_fixture_goal_scorers([{
                "fixture_id": int(row["fixture_id"]),
                "goal_scorers_json": scorers,
                "source": SOURCE_NAME,
                "provider_event_id": page_url,
                "score_verified": True,
                "collected_at": to_iso_z(current),
            }])
            matches_saved += int(saved)
    finally:
        if own_session:
            http.close()

    return OFStatsScorerResult(
        enabled=True,
        completed_matches_considered=len(rows),
        matches_searched=len(rows),
        matches_found=matches_found,
        matches_saved=matches_saved,
        requests_used=requests_used,
        pending_matches=max(0, len(rows) - matches_saved),
        warnings=warnings,
    )
