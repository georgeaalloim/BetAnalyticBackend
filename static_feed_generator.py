from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import exp
from pathlib import Path
from typing import Any, Iterable

from count_market_model import (
    CountMarketContext,
    build_count_market_context,
    predict_count_market,
    unavailable_market,
    walk_forward_backtest,
)
from database import get_connection
from ensemble_value_service import build_ensemble_context, predict_match_ensemble
from time_utils import parse_iso_datetime, to_iso_z


SCHEMA_VERSION = 2
COMPLETED_STATUS = "FT"
TRAINING_SEASON_WINDOW = 3


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
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _safe_parse_date(value: Any) -> datetime | None:
    try:
        return parse_iso_datetime(str(value or ""))
    except (TypeError, ValueError):
        return None


def _safe_parse_fixture_date(fixture: dict[str, Any]) -> datetime | None:
    return _safe_parse_date(fixture.get("fixture_date"))


def select_training_fixtures(
    fixtures: Iterable[dict[str, Any]],
    cutoff: datetime,
    target_fixture_id: int | None = None,
) -> list[dict[str, Any]]:
    """Strict temporal filter: only completed fixtures with date < cutoff."""
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


def select_training_statistics(
    records: Iterable[dict[str, Any]],
    cutoff: datetime,
) -> list[dict[str, Any]]:
    selected: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        fixture_datetime = _safe_parse_date(record.get("fixture_date"))
        if fixture_datetime is None or fixture_datetime >= cutoff:
            continue
        selected.append((fixture_datetime, record))
    selected.sort(key=lambda item: item[0])
    return [record for _, record in selected]


def _load_training_candidates(league_id: int, target_season: int) -> list[dict[str, Any]]:
    first_season = target_season - TRAINING_SEASON_WINDOW + 1
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM fixtures
            WHERE league_id = ?
              AND season BETWEEN ? AND ?
            ORDER BY fixture_date ASC
            """,
            (league_id, first_season, target_season),
        ).fetchall()
    return [dict(row) for row in rows]


def _load_statistics_candidates(league_id: int, target_season: int) -> list[dict[str, Any]]:
    first_season = target_season - TRAINING_SEASON_WINDOW + 1
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM fixture_statistics
            WHERE league_id = ?
              AND season BETWEEN ? AND ?
            ORDER BY fixture_date ASC, fixture_id ASC
            """,
            (league_id, first_season, target_season),
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


def _predict_optional_count_market(
    context: CountMarketContext | None,
    context_error: str | None,
    *,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, Any]:
    if context is None:
        return unavailable_market(context_error or "Δεν υπάρχουν αρκετά δεδομένα.")
    try:
        return predict_count_market(
            context,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    except ValueError as error:
        return unavailable_market(str(error), fixtures_used=context.fixtures_used)


def _fixture_payload(
    fixture: dict[str, Any],
    as_of: datetime,
    goal_context: dict[str, Any] | None,
    goal_context_error: str | None,
    training_fixtures: list[dict[str, Any]],
    corners_context: CountMarketContext | None,
    corners_context_error: str | None,
    training_statistics: list[dict[str, Any]],
) -> dict[str, Any]:
    fixture_id = int(fixture["fixture_id"])
    kickoff = parse_iso_datetime(str(fixture["fixture_date"]))
    cutoff = min(as_of, kickoff)
    home_team_id = int(fixture["home_team_id"])
    away_team_id = int(fixture["away_team_id"])

    payload: dict[str, Any] = {
        "fixture_id": fixture_id,
        "season": int(fixture["season"]),
        "fixture_date": to_iso_z(kickoff),
        "kickoff_time_confirmed": bool(fixture.get("kickoff_time_confirmed")),
        "schedule_source": fixture.get("schedule_source"),
        "status": str(fixture.get("status") or ""),
        "home_team": {
            "team_id": home_team_id,
            "team_name": str(fixture["home_team_name"]),
        },
        "away_team": {
            "team_id": away_team_id,
            "team_name": str(fixture["away_team_name"]),
        },
        "prediction_calculated_at": to_iso_z(as_of),
        "training_cutoff": to_iso_z(cutoff),
        "training_rule": (
            "Χρησιμοποιούνται μόνο αγώνες και στατιστικά με fixture_date "
            "αυστηρά μικρότερο από την ώρα υπολογισμού."
        ),
        "training_season_window": TRAINING_SEASON_WINDOW,
        "training_fixtures_used": len(training_fixtures),
        "training_statistics_used": len(training_statistics),
        "latest_training_fixture_date": (
            str(training_fixtures[-1]["fixture_date"]) if training_fixtures else None
        ),
        "latest_training_statistics_date": (
            str(training_statistics[-1]["fixture_date"]) if training_statistics else None
        ),
    }

    if goal_context is None:
        payload.update(
            {
                "prediction_status": "unavailable",
                "prediction_error": goal_context_error
                or "Δεν υπάρχουν αρκετά προηγούμενα δεδομένα.",
                "prediction": None,
            }
        )
        return payload

    try:
        prediction = predict_match_ensemble(
            context=goal_context,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        # Σε cold-start πρόβλεψη το μοντέλο γνωρίζει μόνο το team_id.
        # Το feed διατηρεί πάντα τα κανονικά ονόματα του προγράμματος.
        prediction["home_team"] = {
            "team_id": home_team_id,
            "team_name": str(fixture["home_team_name"]),
        }
        prediction["away_team"] = {
            "team_id": away_team_id,
            "team_name": str(fixture["away_team_name"]),
        }
        prediction["derived_scoring_probabilities"] = _derive_scoring_probabilities(
            prediction
        )
        prediction["corners_market"] = _predict_optional_count_market(
            corners_context,
            corners_context_error,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
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


def _build_count_context(
    records: list[dict[str, Any]],
    *,
    market: str,
) -> tuple[CountMarketContext | None, str | None]:
    try:
        return build_count_market_context(records, market=market), None
    except ValueError as error:
        return None, str(error)


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
    normalized_seasons = tuple(sorted(set(int(season) for season in seasons)))
    upcoming_fixtures = _load_upcoming_fixtures(
        league_id=league_id,
        seasons=normalized_seasons,
        as_of=as_of,
        lookahead_days=lookahead_days,
        upcoming_statuses=upcoming_statuses,
    )

    goal_context_by_season: dict[int, dict[str, Any] | None] = {}
    goal_error_by_season: dict[int, str | None] = {}
    training_by_season: dict[int, list[dict[str, Any]]] = {}
    stats_by_season: dict[int, list[dict[str, Any]]] = {}
    corners_context_by_season: dict[int, CountMarketContext | None] = {}
    corners_error_by_season: dict[int, str | None] = {}
    validation_by_season: dict[str, Any] = {}

    target_seasons = sorted({int(fixture["season"]) for fixture in upcoming_fixtures})
    for season in target_seasons:
        candidates = _load_training_candidates(league_id, season)
        training_fixtures = select_training_fixtures(candidates, cutoff=as_of)
        training_by_season[season] = training_fixtures
        if not training_fixtures:
            goal_context_by_season[season] = None
            goal_error_by_season[season] = (
                "Δεν υπάρχει ολοκληρωμένος προηγούμενος αγώνας πριν από την ώρα υπολογισμού."
            )
        else:
            try:
                goal_context_by_season[season] = build_ensemble_context(
                    fixtures=training_fixtures
                )
                goal_error_by_season[season] = None
            except (ValueError, RuntimeError) as error:
                goal_context_by_season[season] = None
                goal_error_by_season[season] = str(error)

        stats_candidates = _load_statistics_candidates(league_id, season)
        training_statistics = select_training_statistics(stats_candidates, cutoff=as_of)
        stats_by_season[season] = training_statistics
        corners_context, corners_error = _build_count_context(
            training_statistics, market="corners"
        )
        corners_context_by_season[season] = corners_context
        corners_error_by_season[season] = corners_error
        validation_by_season[str(season)] = {
            "statistics_records": len(training_statistics),
            "corners": walk_forward_backtest(training_statistics, market="corners"),
        }

    fixture_payloads = [
        _fixture_payload(
            fixture=fixture,
            as_of=as_of,
            goal_context=goal_context_by_season.get(int(fixture["season"])),
            goal_context_error=goal_error_by_season.get(int(fixture["season"])),
            training_fixtures=training_by_season.get(int(fixture["season"]), []),
            corners_context=corners_context_by_season.get(int(fixture["season"])),
            corners_context_error=corners_error_by_season.get(int(fixture["season"])),
            training_statistics=stats_by_season.get(int(fixture["season"]), []),
        )
        for fixture in upcoming_fixtures
    ]

    seasons_payload: list[dict[str, Any]] = []
    for season in normalized_seasons:
        season_fixtures = [
            fixture for fixture in fixture_payloads if int(fixture["season"]) == season
        ]
        if season_fixtures:
            seasons_payload.append(
                {
                    "season": season,
                    "fixtures_count": len(season_fixtures),
                    "fixtures": season_fixtures,
                }
            )

    ready_count = sum(
        fixture["prediction_status"] == "ready" for fixture in fixture_payloads
    )
    unavailable_count = len(fixture_payloads) - ready_count
    feed_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": to_iso_z(as_of),
        "league": {"league_id": league_id, "league_name": league_name},
        "model": {
            "name": "Probability Ensemble v0.5",
            "baseline_weight": 0.60,
            "mle_weight": 0.40,
            "temporal_leakage_protection": True,
            "training_season_window": TRAINING_SEASON_WINDOW,
            "count_markets_model": "Bayesian-Smoothed Count Markets v0.1",
            "count_markets": ["corners"],
        },
        "count_market_validation": validation_by_season,
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
    feed_sha256 = hashlib.sha256(feed_path.read_bytes()).hexdigest()
    manifest_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "data_version": int(as_of.timestamp()),
        "model_version": "0.5-corners-only",
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
