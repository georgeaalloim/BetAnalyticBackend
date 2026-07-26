from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import exp
from pathlib import Path
from typing import Any, Iterable

from database import get_connection
from ensemble_value_service import (
    build_ensemble_context,
    predict_match_ensemble,
)
from time_utils import parse_iso_datetime, to_iso_z


SCHEMA_VERSION = 1
COMPLETED_STATUS = "FT"


@dataclass(frozen=True)
class GeneratedFeed:
    feed_path: Path
    manifest_path: Path
    fixture_count: int
    ready_prediction_count: int
    unavailable_prediction_count: int
    sha256: str


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _safe_parse_fixture_date(fixture: dict[str, Any]) -> datetime | None:
    try:
        return parse_iso_datetime(str(fixture.get("fixture_date") or ""))
    except (TypeError, ValueError):
        return None


def select_training_fixtures(
    fixtures: Iterable[dict[str, Any]],
    cutoff: datetime,
    target_fixture_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Επιλέγει μόνο τελειωμένους αγώνες που ήταν γνωστοί πριν το cutoff.

    Ο κανόνας είναι αυστηρός: ``fixture_date < cutoff``. Ο αγώνας που
    προβλέπεται αποκλείεται ρητά, ακόμη και αν δοθεί κατά λάθος στη λίστα.
    """

    selected: list[tuple[datetime, dict[str, Any]]] = []

    for fixture in fixtures:
        fixture_id = int(fixture.get("fixture_id") or -1)
        if target_fixture_id is not None and fixture_id == target_fixture_id:
            continue

        if str(fixture.get("status") or "").upper() != COMPLETED_STATUS:
            continue

        if fixture.get("home_goals") is None or fixture.get("away_goals") is None:
            continue

        fixture_datetime = _safe_parse_fixture_date(fixture)
        if fixture_datetime is None or fixture_datetime >= cutoff:
            continue

        selected.append((fixture_datetime, fixture))

    selected.sort(key=lambda item: item[0])
    return [fixture for _, fixture in selected]


def _load_season_fixtures(
    league_id: int,
    season: int,
) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM fixtures
            WHERE league_id = ?
              AND season = ?
            ORDER BY fixture_date ASC
            """,
            (league_id, season),
        ).fetchall()

    return [dict(row) for row in rows]


def _load_upcoming_fixtures(
    league_id: int,
    seasons: Iterable[int],
    as_of: datetime,
    lookahead_days: int,
    upcoming_statuses: tuple[str, ...],
) -> list[dict[str, Any]]:
    season_values = tuple(sorted(set(int(season) for season in seasons)))
    if not season_values:
        return []

    placeholders = ",".join("?" for _ in season_values)
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM fixtures
            WHERE league_id = ?
              AND season IN ({placeholders})
            ORDER BY fixture_date ASC
            """,
            (league_id, *season_values),
        ).fetchall()

    upper_bound = as_of + timedelta(days=lookahead_days)
    allowed_statuses = {status.upper() for status in upcoming_statuses}
    upcoming: list[tuple[datetime, dict[str, Any]]] = []

    for row in rows:
        fixture = dict(row)
        if str(fixture.get("status") or "").upper() not in allowed_statuses:
            continue

        fixture_datetime = _safe_parse_fixture_date(fixture)
        if fixture_datetime is None:
            continue

        if as_of < fixture_datetime <= upper_bound:
            upcoming.append((fixture_datetime, fixture))

    upcoming.sort(key=lambda item: item[0])
    return [fixture for _, fixture in upcoming]


def _derive_scoring_probabilities(prediction: dict[str, Any]) -> dict[str, float]:
    expected_goals = prediction["expected_goals"]
    home_xg = float(expected_goals["home"])
    away_xg = float(expected_goals["away"])

    only_home = (1.0 - exp(-home_xg)) * exp(-away_xg) * 100.0
    only_away = exp(-home_xg) * (1.0 - exp(-away_xg)) * 100.0

    return {
        "only_home_scores_percent": round(only_home, 2),
        "only_away_scores_percent": round(only_away, 2),
    }


def _fixture_payload(
    fixture: dict[str, Any],
    as_of: datetime,
    context: dict[str, Any] | None,
    context_error: str | None,
    training_fixtures: list[dict[str, Any]],
) -> dict[str, Any]:
    fixture_id = int(fixture["fixture_id"])
    kickoff = parse_iso_datetime(str(fixture["fixture_date"]))

    payload: dict[str, Any] = {
        "fixture_id": fixture_id,
        "season": int(fixture["season"]),
        "fixture_date": to_iso_z(kickoff),
        "status": str(fixture.get("status") or ""),
        "home_team": {
            "team_id": int(fixture["home_team_id"]),
            "team_name": str(fixture["home_team_name"]),
        },
        "away_team": {
            "team_id": int(fixture["away_team_id"]),
            "team_name": str(fixture["away_team_name"]),
        },
        "prediction_calculated_at": to_iso_z(as_of),
        "training_cutoff": to_iso_z(as_of),
        "training_rule": (
            "Χρησιμοποιούνται μόνο αγώνες status=FT με fixture_date "
            "αυστηρά μικρότερο από την ώρα υπολογισμού."
        ),
        "training_fixtures_used": len(training_fixtures),
        "latest_training_fixture_date": (
            str(training_fixtures[-1]["fixture_date"])
            if training_fixtures
            else None
        ),
    }

    if context is None:
        payload.update(
            {
                "prediction_status": "unavailable",
                "prediction_error": context_error
                or "Δεν υπάρχουν αρκετά προηγούμενα δεδομένα.",
                "prediction": None,
            }
        )
        return payload

    try:
        prediction = predict_match_ensemble(
            context=context,
            home_team_id=int(fixture["home_team_id"]),
            away_team_id=int(fixture["away_team_id"]),
        )
        prediction["derived_scoring_probabilities"] = (
            _derive_scoring_probabilities(prediction)
        )

        payload.update(
            {
                "prediction_status": "ready",
                "prediction_error": None,
                "prediction": prediction,
            }
        )
    except (ValueError, RuntimeError) as error:
        payload.update(
            {
                "prediction_status": "unavailable",
                "prediction_error": str(error),
                "prediction": None,
            }
        )

    return payload


def generate_static_feed(
    output_dir: Path,
    league_id: int,
    league_name: str,
    seasons: Iterable[int],
    as_of: datetime,
    lookahead_days: int,
    upcoming_statuses: tuple[str, ...],
    feed_public_url: str,
    sync_summary: dict[str, Any],
) -> GeneratedFeed:
    """
    Δημιουργεί ``feed.json`` και ``manifest.json`` για την Android εφαρμογή.

    Το context κάθε σεζόν κατασκευάζεται μία φορά με τα αποτελέσματα που
    υπήρχαν πριν από την ώρα παραγωγής. Σε κάθε επόμενο workflow run,
    νέα τελικά αποτελέσματα μπαίνουν αυτόματα στο context.
    """

    normalized_seasons = tuple(sorted(set(int(season) for season in seasons)))
    upcoming_fixtures = _load_upcoming_fixtures(
        league_id=league_id,
        seasons=normalized_seasons,
        as_of=as_of,
        lookahead_days=lookahead_days,
        upcoming_statuses=upcoming_statuses,
    )

    context_by_season: dict[int, dict[str, Any] | None] = {}
    context_error_by_season: dict[int, str | None] = {}
    training_by_season: dict[int, list[dict[str, Any]]] = {}

    for season in sorted({int(fixture["season"]) for fixture in upcoming_fixtures}):
        season_fixtures = _load_season_fixtures(league_id, season)
        training_fixtures = select_training_fixtures(
            fixtures=season_fixtures,
            cutoff=as_of,
        )
        training_by_season[season] = training_fixtures

        if not training_fixtures:
            context_by_season[season] = None
            context_error_by_season[season] = (
                "Δεν υπάρχει ολοκληρωμένος αγώνας της ίδιας σεζόν "
                "πριν από την ώρα υπολογισμού."
            )
            continue

        try:
            context_by_season[season] = build_ensemble_context(
                fixtures=training_fixtures,
            )
            context_error_by_season[season] = None
        except (ValueError, RuntimeError) as error:
            context_by_season[season] = None
            context_error_by_season[season] = str(error)

    fixture_payloads = [
        _fixture_payload(
            fixture=fixture,
            as_of=as_of,
            context=context_by_season.get(int(fixture["season"])),
            context_error=context_error_by_season.get(int(fixture["season"])),
            training_fixtures=training_by_season.get(int(fixture["season"]), []),
        )
        for fixture in upcoming_fixtures
    ]

    seasons_payload: list[dict[str, Any]] = []
    for season in normalized_seasons:
        season_fixtures_payload = [
            fixture
            for fixture in fixture_payloads
            if int(fixture["season"]) == season
        ]
        if season_fixtures_payload:
            seasons_payload.append(
                {
                    "season": season,
                    "fixtures_count": len(season_fixtures_payload),
                    "fixtures": season_fixtures_payload,
                }
            )

    ready_count = sum(
        fixture["prediction_status"] == "ready" for fixture in fixture_payloads
    )
    unavailable_count = len(fixture_payloads) - ready_count

    feed_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": to_iso_z(as_of),
        "league": {
            "league_id": league_id,
            "league_name": league_name,
        },
        "model": {
            "name": "Probability Ensemble v0.5",
            "baseline_weight": 0.60,
            "mle_weight": 0.40,
            "temporal_leakage_protection": True,
        },
        "lookahead_days": lookahead_days,
        "sync_summary": sync_summary,
        "fixtures_count": len(fixture_payloads),
        "ready_predictions": ready_count,
        "unavailable_predictions": unavailable_count,
        "seasons": seasons_payload,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    feed_path = output_dir / "feed.json"
    manifest_path = output_dir / "manifest.json"
    _atomic_write_json(feed_path, feed_payload)

    feed_bytes = feed_path.read_bytes()
    feed_sha256 = hashlib.sha256(feed_bytes).hexdigest()
    data_version = int(as_of.timestamp())

    manifest_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "data_version": data_version,
        "model_version": "0.5",
        "generated_at": to_iso_z(as_of),
        "feed_url": feed_public_url,
        "feed_sha256": feed_sha256,
        "fixtures_count": len(fixture_payloads),
        "ready_predictions": ready_count,
    }
    _atomic_write_json(manifest_path, manifest_payload)

    return GeneratedFeed(
        feed_path=feed_path,
        manifest_path=manifest_path,
        fixture_count=len(fixture_payloads),
        ready_prediction_count=ready_count,
        unavailable_prediction_count=unavailable_count,
        sha256=feed_sha256,
    )
