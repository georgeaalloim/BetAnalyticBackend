from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from database import get_connection, initialize_database, save_fixture_goal_scorers
from goal_scorer_enricher import _names_match
from time_utils import parse_iso_datetime, to_iso_z, utc_now


SOURCE_NAME = "Sofascore public match incidents"
LEAGUE_ID = 197
SOFASCORE_UNIQUE_TOURNAMENT_ID = 185
ATHENS_TZ = ZoneInfo("Europe/Athens")
DEFAULT_RECENT_DAYS = 60
DEFAULT_MAX_MATCHES = 40
REQUEST_TIMEOUT_SECONDS = 18

# Two public hosts are supported by Sofascore clients. If one host is
# temporarily challenged, the other is tried automatically.
BASE_URLS = (
    "https://api.sofascore.com/api/v1",
    "https://www.sofascore.com/api/v1",
)


@dataclass
class SofascoreScorerResult:
    enabled: bool
    completed_matches_considered: int
    dates_searched: int
    matches_searched: int
    events_found: int
    matches_saved: int
    requests_used: int
    pending_matches: int
    warnings: list[str] = field(default_factory=list)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _score_value(block: Any) -> int | None:
    if isinstance(block, dict):
        for key in ("current", "normaltime", "display"):
            value = _as_int(block.get(key))
            if value is not None:
                return value
        return None
    return _as_int(block)


def _fixture_local_date(row: dict[str, Any]) -> str | None:
    try:
        return parse_iso_datetime(
            str(row.get("fixture_date") or "")
        ).astimezone(ATHENS_TZ).date().isoformat()
    except ValueError:
        return None


def _pending_rows(
    *,
    season: int | None,
    recent_days: int,
    max_matches: int,
    as_of: datetime,
) -> list[dict[str, Any]]:
    cutoff = as_of.astimezone(timezone.utc) - timedelta(
        days=max(1, int(recent_days))
    )
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

    # Oldest unresolved first prevents one stubborn new match from starving
    # older pending matches forever. The date endpoint is batched, so a larger
    # safe window remains inexpensive.
    query += " ORDER BY f.fixture_date ASC, f.fixture_id ASC"

    with get_connection() as connection:
        rows = [
            dict(row)
            for row in connection.execute(query, tuple(params)).fetchall()
        ]

    selected: list[dict[str, Any]] = []
    for row in rows:
        try:
            fixture_time = parse_iso_datetime(
                str(row.get("fixture_date") or "")
            ).astimezone(timezone.utc)
        except ValueError:
            continue
        if fixture_time < cutoff:
            continue
        selected.append(row)
        if len(selected) >= max(0, int(max_matches)):
            break
    return selected


def _request_json(
    session: requests.Session,
    path: str,
) -> tuple[dict[str, Any] | None, int, str | None]:
    attempts = 0
    errors: list[str] = []
    for base in BASE_URLS:
        attempts += 1
        url = f"{base}{path}"
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("unexpected non-object JSON")
            return payload, attempts, None
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{base}: {exc}")
    return None, attempts, " | ".join(errors)


def _event_unique_tournament_id(event: dict[str, Any]) -> int | None:
    tournament = event.get("tournament")
    if not isinstance(tournament, dict):
        return None
    unique = tournament.get("uniqueTournament")
    if isinstance(unique, dict):
        value = _as_int(unique.get("id"))
        if value is not None:
            return value
    return None


def _event_is_greek_superleague(event: dict[str, Any]) -> bool:
    tournament = event.get("tournament")
    if not isinstance(tournament, dict):
        return False

    unique_id = _event_unique_tournament_id(event)
    if unique_id is not None:
        return unique_id == SOFASCORE_UNIQUE_TOURNAMENT_ID

    # Defensive fallback if the provider omits uniqueTournament in a daily
    # payload. Still require Greece + Super League wording.
    category = tournament.get("category")
    category_name = (
        str(category.get("name") or "").casefold()
        if isinstance(category, dict)
        else ""
    )
    tournament_name = str(tournament.get("name") or "").casefold()
    return (
        "greece" in category_name
        and "super league" in tournament_name
    )


def _event_status_finished(event: dict[str, Any]) -> bool:
    status = event.get("status")
    if not isinstance(status, dict):
        return False
    status_type = str(status.get("type") or "").casefold()
    description = str(status.get("description") or "").casefold()
    return status_type == "finished" or description in {
        "ended",
        "finished",
        "after penalties",
        "after extra time",
    }


def _event_matches_fixture(
    event: dict[str, Any],
    row: dict[str, Any],
    expected_date: str,
) -> bool:
    if not _event_is_greek_superleague(event):
        return False
    if not _event_status_finished(event):
        return False

    home = event.get("homeTeam")
    away = event.get("awayTeam")
    if not isinstance(home, dict) or not isinstance(away, dict):
        return False

    if not _names_match(
        home.get("name"),
        int(row["home_team_id"]),
        str(row["home_team_name"]),
    ):
        return False
    if not _names_match(
        away.get("name"),
        int(row["away_team_id"]),
        str(row["away_team_name"]),
    ):
        return False

    timestamp = _as_int(event.get("startTimestamp"))
    if timestamp is not None:
        event_date = datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ).astimezone(ATHENS_TZ).date().isoformat()
        if event_date != expected_date:
            return False

    home_score = _score_value(event.get("homeScore"))
    away_score = _score_value(event.get("awayScore"))
    if home_score != int(row["home_goals"]):
        return False
    if away_score != int(row["away_goals"]):
        return False

    return _as_int(event.get("id")) is not None


def _event_items(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get("events")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _fetch_day_events(
    session: requests.Session,
    local_date: str,
    *,
    include_inverse: bool,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    requests_used = 0
    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    paths = [
        f"/sport/football/scheduled-events/{local_date}",
    ]
    if include_inverse:
        paths.append(
            f"/sport/football/scheduled-events/{local_date}/inverse"
        )

    for path in paths:
        payload, used, error = _request_json(session, path)
        requests_used += used
        if error:
            warnings.append(
                f"Sofascore schedule {local_date} απέτυχε: {error}"
            )
            continue
        for event in _event_items(payload):
            event_id = _as_int(event.get("id"))
            if event_id is None or event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            events.append(event)

    return events, requests_used, warnings


def _goal_incidents(
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    incidents = payload.get("incidents")
    if not isinstance(incidents, list):
        return []

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in incidents:
        if not isinstance(item, dict):
            continue
        if str(item.get("incidentType") or "").casefold() != "goal":
            continue
        if bool(item.get("rescinded")):
            continue

        incident_class = str(
            item.get("incidentClass") or ""
        ).casefold()
        if incident_class in {
            "missed",
            "disallowed",
            "cancelled",
            "canceled",
        }:
            continue

        incident_id = str(item.get("id") or "")
        if incident_id and incident_id in seen_ids:
            continue
        if incident_id:
            seen_ids.add(incident_id)
        out.append(item)
    return out


def _parse_scorers(
    payload: dict[str, Any] | None,
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    goals = _goal_incidents(payload)
    if not goals:
        return []

    raw: list[dict[str, Any]] = []
    for item in goals:
        player = item.get("player")
        player_name = ""
        if isinstance(player, dict):
            player_name = str(player.get("name") or "").strip()
        if not player_name:
            player_name = str(item.get("playerName") or "").strip()
        if not player_name:
            return []

        home_score = _as_int(item.get("homeScore"))
        away_score = _as_int(item.get("awayScore"))
        if home_score is None or away_score is None:
            # Score progression is mandatory; this avoids guessing own-goal
            # attribution or side from incomplete incidents.
            return []

        minute = _as_int(item.get("time"))
        added = _as_int(item.get("addedTime"))
        if added is not None and added <= 0:
            added = None

        incident_class_raw = str(
            item.get("incidentClass") or ""
        ).strip()
        incident_class = incident_class_raw.casefold()
        if "own" in incident_class:
            detail = "Own Goal"
        elif "pen" in incident_class:
            detail = "Penalty"
        else:
            detail = "Goal"

        raw.append(
            {
                "player_name": player_name,
                "home_score": home_score,
                "away_score": away_score,
                "minute": minute,
                "extra_minute": added,
                "detail": detail,
            }
        )

    # Sofascore normally returns newest incidents first. Score total gives a
    # provider-independent chronological order even when minute metadata is
    # duplicated or injury time is involved.
    raw.sort(
        key=lambda item: (
            int(item["home_score"]) + int(item["away_score"]),
            int(item["minute"] or 0),
            int(item["extra_minute"] or 0),
        )
    )

    previous_home = 0
    previous_away = 0
    scorers: list[dict[str, Any]] = []
    for item in raw:
        score_home = int(item["home_score"])
        score_away = int(item["away_score"])

        if (
            score_home == previous_home + 1
            and score_away == previous_away
        ):
            side = "home"
        elif (
            score_away == previous_away + 1
            and score_home == previous_home
        ):
            side = "away"
        else:
            # A duplicate, reverted VAR goal, or malformed progression is not
            # accepted. The entire match stays pending for a later retry.
            return []

        scorers.append(
            {
                "player_name": str(item["player_name"]),
                "side": side,
                "team_id": int(
                    row["home_team_id"]
                    if side == "home"
                    else row["away_team_id"]
                ),
                "team_name": str(
                    row["home_team_name"]
                    if side == "home"
                    else row["away_team_name"]
                ),
                "minute": item["minute"],
                "extra_minute": item["extra_minute"],
                "detail": str(item["detail"]),
            }
        )
        previous_home = score_home
        previous_away = score_away

    expected_home = int(row["home_goals"])
    expected_away = int(row["away_goals"])
    if previous_home != expected_home or previous_away != expected_away:
        return []
    if len(scorers) != expected_home + expected_away:
        return []
    return scorers


def enrich_goal_scorers_from_sofascore(
    *,
    season: int | None = None,
    recent_days: int = DEFAULT_RECENT_DAYS,
    max_matches: int = DEFAULT_MAX_MATCHES,
    as_of: datetime | None = None,
    session: requests.Session | None = None,
) -> SofascoreScorerResult:
    """Automatically fill every still-missing FT scorer set.

    There are no match/player hard-coded rows. A scorer set is persisted only
    if provider date, teams, final score and the complete goal-score progression
    all agree with the BetAnalytic fixture.
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
        return SofascoreScorerResult(
            True, 0, 0, 0, 0, 0, 0, 0, []
        )

    own_session = session is None
    http = session or requests.Session()
    http.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": "https://www.sofascore.com/",
            "Origin": "https://www.sofascore.com",
        }
    )

    requests_used = 0
    warnings: list[str] = []
    events_found = 0
    matches_saved = 0
    matches_searched = 0

    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        local_date = _fixture_local_date(row)
        if local_date:
            by_date[local_date].append(row)

    try:
        for local_date, date_rows in by_date.items():
            # First request is compact. If even one BetAnalytic fixture on that
            # date cannot be found, the "inverse" all-events response is loaded
            # once and reused for every pending match on that day.
            events, used, day_warnings = _fetch_day_events(
                http,
                local_date,
                include_inverse=False,
            )
            requests_used += used
            warnings.extend(day_warnings)

            matched: dict[int, dict[str, Any]] = {}
            for row in date_rows:
                for event in events:
                    if _event_matches_fixture(
                        event,
                        row,
                        local_date,
                    ):
                        matched[int(row["fixture_id"])] = event
                        break

            missing_rows = [
                row
                for row in date_rows
                if int(row["fixture_id"]) not in matched
            ]
            if missing_rows:
                inverse_events, used, inverse_warnings = (
                    _fetch_day_events(
                        http,
                        local_date,
                        include_inverse=True,
                    )
                )
                requests_used += used
                warnings.extend(inverse_warnings)
                for row in missing_rows:
                    for event in inverse_events:
                        if _event_matches_fixture(
                            event,
                            row,
                            local_date,
                        ):
                            matched[int(row["fixture_id"])] = event
                            break

            for row in date_rows:
                matches_searched += 1
                fixture_id = int(row["fixture_id"])
                event = matched.get(fixture_id)
                if event is None:
                    warnings.append(
                        "Pending scorers: Sofascore event δεν βρέθηκε για "
                        f"{row['home_team_name']} - "
                        f"{row['away_team_name']} ({local_date})."
                    )
                    continue

                events_found += 1
                event_id = _as_int(event.get("id"))
                if event_id is None:
                    continue

                payload, used, error = _request_json(
                    http,
                    f"/event/{event_id}/incidents",
                )
                requests_used += used
                if error:
                    warnings.append(
                        "Pending scorers: Sofascore incidents απέτυχαν για "
                        f"{row['home_team_name']} - "
                        f"{row['away_team_name']}: {error}"
                    )
                    continue

                scorers = _parse_scorers(payload, row)
                expected = int(row["home_goals"]) + int(
                    row["away_goals"]
                )
                if len(scorers) != expected:
                    warnings.append(
                        "Pending scorers: Sofascore incidents "
                        "ελλιπή/ασύμφωνα για "
                        f"{row['home_team_name']} - "
                        f"{row['away_team_name']} "
                        f"(βρέθηκαν {len(scorers)}, "
                        f"αναμένονται {expected})."
                    )
                    continue

                matches_saved += save_fixture_goal_scorers(
                    [
                        {
                            "fixture_id": fixture_id,
                            "goal_scorers_json": scorers,
                            "source": SOURCE_NAME,
                            "provider_event_id": str(event_id),
                            "score_verified": True,
                            "collected_at": to_iso_z(current),
                        }
                    ]
                )
    finally:
        if own_session:
            http.close()

    return SofascoreScorerResult(
        enabled=True,
        completed_matches_considered=len(rows),
        dates_searched=len(by_date),
        matches_searched=matches_searched,
        events_found=events_found,
        matches_saved=matches_saved,
        requests_used=requests_used,
        pending_matches=max(0, len(rows) - matches_saved),
        warnings=warnings,
    )
