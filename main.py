from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backtesting import backtest_poisson_model
from database import (
    DATABASE_PATH,
    count_fixtures,
    count_odds_snapshots,
    get_completed_fixtures,
    get_completed_fixtures_before_date,
    get_fixture_by_id,
    get_latest_odds_snapshots,
    get_odds_snapshots,
    get_saved_fixtures,
    initialize_database,
    save_fixtures,
)
from ensemble_value_service import (
    analyze_match_value_1x2,
    build_ensemble_context,
    predict_match_ensemble,
)
from football_api import api_get
from odds_service import (
    save_manual_1x2_snapshot,
    sync_fixture_1x2_odds,
)
from poisson_model import predict_match
from team_analysis import (
    calculate_home_away_statistics,
    calculate_team_statistics,
)


SUPER_LEAGUE_ID = 197
ALLOWED_FREE_SEASONS = {
    2022,
    2023,
    2024,
}

DEFAULT_MIN_EDGE_PERCENT = 3.0
DEFAULT_MIN_EXPECTED_VALUE_PERCENT = 3.0
DEFAULT_KELLY_MULTIPLIER = 0.25
DEFAULT_MAX_BANKROLL_FRACTION = 0.02


app = FastAPI(
    title="BetAnalytic API",
    version="0.6.1",
    description=(
        "Football probability analysis, Poisson models, "
        "ensemble predictions, 1-X-2 value analysis "
        "and timestamped odds collection."
    ),
)


class ValueAnalysisRequest(BaseModel):
    """
    Δεδομένα που στέλνει το Android app για ανάλυση
    ενός αγώνα και των αποδόσεων 1-X-2.
    """

    home_team_id: int = Field(
        gt=0,
        description="API-Football ID γηπεδούχου.",
    )

    away_team_id: int = Field(
        gt=0,
        description="API-Football ID φιλοξενούμενου.",
    )

    home_odds: float = Field(
        gt=1.0,
        description="Δεκαδική απόδοση άσου.",
    )

    draw_odds: float = Field(
        gt=1.0,
        description="Δεκαδική απόδοση ισοπαλίας.",
    )

    away_odds: float = Field(
        gt=1.0,
        description="Δεκαδική απόδοση διπλού.",
    )

    min_edge_percent: float = Field(
        default=DEFAULT_MIN_EDGE_PERCENT,
        ge=0.0,
        description=(
            "Ελάχιστη διαφορά πιθανότητας μοντέλου "
            "και fair πιθανότητας αγοράς."
        ),
    )

    min_expected_value_percent: float = Field(
        default=(
            DEFAULT_MIN_EXPECTED_VALUE_PERCENT
        ),
        ge=0.0,
        description=(
            "Ελάχιστο Expected Value σε ποσοστό."
        ),
    )

    kelly_multiplier: float = Field(
        default=DEFAULT_KELLY_MULTIPLIER,
        ge=0.0,
        le=1.0,
        description=(
            "Τμήμα του πλήρους Kelly. "
            "0.25 σημαίνει quarter Kelly."
        ),
    )

    max_bankroll_fraction: float = Field(
        default=DEFAULT_MAX_BANKROLL_FRACTION,
        ge=0.0,
        le=1.0,
        description=(
            "Μέγιστο προτεινόμενο κλάσμα bankroll. "
            "0.02 σημαίνει 2%."
        ),
    )

    force_refit: bool = Field(
        default=False,
        description=(
            "Όταν είναι true, επανεκπαιδεύει "
            "το ensemble ακόμη και αν υπάρχει cache."
        ),
    )


class ManualOddsSnapshotRequest(BaseModel):
    """
    Χειροκίνητη τρέχουσα παρατήρηση αποδόσεων 1-X-2.

    Το timestamp δημιουργείται από τον server και δεν μπορεί
    να δηλωθεί από τον χρήστη, ώστε να μην γίνεται backdating.
    """

    bookmaker_name: str = Field(
        min_length=1,
        max_length=120,
        description="Όνομα bookmaker ή πηγής αποδόσεων.",
    )

    bookmaker_id: int | None = Field(
        default=None,
        gt=0,
        description="Προαιρετικό API-Football bookmaker ID.",
    )

    home_odds: float = Field(
        gt=1.0,
        description="Τρέχουσα δεκαδική απόδοση άσου.",
    )

    draw_odds: float = Field(
        gt=1.0,
        description="Τρέχουσα δεκαδική απόδοση ισοπαλίας.",
    )

    away_odds: float = Field(
        gt=1.0,
        description="Τρέχουσα δεκαδική απόδοση διπλού.",
    )


ENSEMBLE_CONTEXT_CACHE: dict[
    int,
    dict[str, Any],
] = {}


def invalidate_ensemble_cache(
    season: int | None = None,
) -> None:
    """
    Καθαρίζει το προσωρινό ensemble cache.

    Με season=None καθαρίζονται όλες οι σεζόν.
    """

    if season is None:
        ENSEMBLE_CONTEXT_CACHE.clear()
        return

    ENSEMBLE_CONTEXT_CACHE.pop(
        season,
        None,
    )


def create_fixture_signature(
    fixtures: list[dict[str, Any]],
) -> int:
    """
    Δημιουργεί προσωρινή υπογραφή των αγώνων.

    Έτσι το MLE μοντέλο επανεκπαιδεύεται αυτόματα
    όταν αλλάξει πλήθος αγώνων, ημερομηνία ή σκορ.
    """

    signature_rows = sorted(
        (
            str(
                fixture.get(
                    "fixture_id",
                    "",
                )
            ),
            str(
                fixture.get(
                    "fixture_date",
                    "",
                )
            ),
            int(
                fixture.get(
                    "home_goals",
                    0,
                )
            ),
            int(
                fixture.get(
                    "away_goals",
                    0,
                )
            ),
        )
        for fixture in fixtures
    )

    return hash(
        tuple(signature_rows)
    )


def get_completed_season_fixtures(
    season: int,
) -> list[dict[str, Any]]:
    """
    Διαβάζει ολοκληρωμένους αγώνες μιας σεζόν
    και επιστρέφει 404 όταν δεν υπάρχουν δεδομένα.
    """

    fixtures = get_completed_fixtures(
        league_id=SUPER_LEAGUE_ID,
        season=season,
    )

    if not fixtures:
        raise HTTPException(
            status_code=404,
            detail=(
                "Δεν βρέθηκαν ολοκληρωμένοι αγώνες "
                "για τη συγκεκριμένη σεζόν."
            ),
        )

    return fixtures


def get_ensemble_context_for_season(
    season: int,
    force_refit: bool = False,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    bool,
]:
    """
    Επιστρέφει fixtures, ensemble context και ένδειξη
    αν χρησιμοποιήθηκε ήδη εκπαιδευμένο context.

    Το MLE fit είναι ακριβός υπολογισμός, επομένως
    αποθηκεύεται προσωρινά στη μνήμη του server.
    """

    fixtures = get_completed_season_fixtures(
        season=season,
    )

    fixture_signature = (
        create_fixture_signature(
            fixtures=fixtures,
        )
    )

    cached_entry = (
        ENSEMBLE_CONTEXT_CACHE.get(
            season
        )
    )

    cache_is_valid = (
        not force_refit
        and cached_entry is not None
        and cached_entry.get(
            "fixture_signature"
        )
        == fixture_signature
    )

    if cache_is_valid:
        return (
            fixtures,
            cached_entry["context"],
            True,
        )

    try:
        context = build_ensemble_context(
            fixtures=fixtures,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    except RuntimeError as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "Αποτυχία εκπαίδευσης του "
                f"Poisson MLE: {error}"
            ),
        ) from error

    ENSEMBLE_CONTEXT_CACHE[
        season
    ] = {
        "fixture_signature": (
            fixture_signature
        ),
        "context": context,
    }

    return (
        fixtures,
        context,
        False,
    )


def validate_cutoff_date(
    cutoff_date: str,
) -> str:
    """
    Ελέγχει ότι το cutoff_date είναι έγκυρη
    ημερομηνία ή ISO datetime.

    Παραδείγματα:
        2025-01-10
        2025-01-10T18:30:00+00:00
        2025-01-10T18:30:00Z
    """

    cleaned_cutoff_date = (
        cutoff_date.strip()
    )

    if not cleaned_cutoff_date:
        raise HTTPException(
            status_code=400,
            detail=(
                "Το cutoff_date δεν μπορεί "
                "να είναι κενό."
            ),
        )

    normalized_for_validation = (
        cleaned_cutoff_date.replace(
            "Z",
            "+00:00",
        )
    )

    try:
        datetime.fromisoformat(
            normalized_for_validation
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=(
                "Το cutoff_date πρέπει να έχει "
                "μορφή ISO, π.χ. 2025-01-10 ή "
                "2025-01-10T18:30:00+00:00."
            ),
        ) from error

    return cleaned_cutoff_date


def get_ensemble_context_before_date(
    season: int,
    cutoff_date: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
]:
    """
    Κατασκευάζει ensemble context αποκλειστικά
    από αγώνες που ολοκληρώθηκαν πριν από
    το cutoff_date.

    Δεν χρησιμοποιεί το κανονικό season cache,
    επειδή κάθε ημερομηνία αντιστοιχεί σε
    διαφορετικό ιστορικό δείγμα.
    """

    validated_cutoff_date = (
        validate_cutoff_date(
            cutoff_date=cutoff_date,
        )
    )

    fixtures = (
        get_completed_fixtures_before_date(
            league_id=SUPER_LEAGUE_ID,
            season=season,
            cutoff_date=(
                validated_cutoff_date
            ),
        )
    )

    if not fixtures:
        raise HTTPException(
            status_code=404,
            detail=(
                "Δεν υπάρχουν ολοκληρωμένοι "
                "αγώνες πριν από τη συγκεκριμένη "
                "ημερομηνία."
            ),
        )

    try:
        context = build_ensemble_context(
            fixtures=fixtures,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    except RuntimeError as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "Αποτυχία εκπαίδευσης του "
                f"Poisson MLE: {error}"
            ),
        ) from error

    return fixtures, context


@app.get("/")
def read_root() -> dict[str, str]:
    """
    Απλός έλεγχος ότι λειτουργεί ο server.
    """

    return {
        "app": "BetAnalytic",
        "version": "0.6.1",
        "status": "Backend is running",
    }


@app.post("/database/init")
def create_database() -> dict[str, str]:
    """
    Δημιουργεί τη βάση και τους απαραίτητους πίνακες.
    """

    initialize_database()
    invalidate_ensemble_cache()

    return {
        "status": "Database initialized",
        "database_path": str(
            DATABASE_PATH
        ),
    }


@app.post(
    "/sync/fixtures/super-league/{season}"
)
def sync_super_league_fixtures(
    season: int,
) -> dict[str, Any]:
    """
    Κατεβάζει όλους τους αγώνες μιας σεζόν
    της Super League 1 και τους αποθηκεύει στη SQLite.
    """

    if season not in ALLOWED_FREE_SEASONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Με το δωρεάν πλάνο χρησιμοποίησε "
                "σεζόν 2022, 2023 ή 2024."
            ),
        )

    try:
        data = api_get(
            endpoint="/fixtures",
            params={
                "league": SUPER_LEAGUE_ID,
                "season": season,
            },
        )

        fixtures = data.get(
            "response",
            [],
        )

        processed = save_fixtures(
            fixtures
        )

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=502,
            detail=str(error),
        ) from error

    invalidate_ensemble_cache(
        season=season
    )

    total_saved = count_fixtures(
        league_id=SUPER_LEAGUE_ID,
        season=season,
    )

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "received_from_api": len(
            fixtures
        ),
        "processed": processed,
        "total_saved_in_database": (
            total_saved
        ),
        "ensemble_cache_invalidated": (
            True
        ),
    }


@app.get(
    "/database/fixtures/super-league/{season}"
)
def read_super_league_fixtures(
    season: int,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Διαβάζει αγώνες μόνο από τη δική μας βάση.
    Δεν καταναλώνει αίτημα από το API-Football.
    """

    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=400,
            detail=(
                "Το limit πρέπει να είναι "
                "από 1 έως 500."
            ),
        )

    fixtures = get_saved_fixtures(
        league_id=SUPER_LEAGUE_ID,
        season=season,
        limit=limit,
    )

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "count": len(fixtures),
        "fixtures": fixtures,
    }


@app.post(
    "/sync/odds/1x2/fixture/{fixture_id}"
)
def sync_fixture_odds_1x2(
    fixture_id: int,
    bookmaker_id: int | None = None,
) -> dict[str, Any]:
    """
    Κατεβάζει τις διαθέσιμες pre-match αποδόσεις 1-X-2
    ενός fixture και αποθηκεύει timestamped snapshots.

    Αν ο αγώνας δεν υπάρχει στη βάση, γίνεται πρώτα ένα
    επιπλέον request στο endpoint /fixtures.
    """

    try:
        result = sync_fixture_1x2_odds(
            fixture_id=fixture_id,
            bookmaker_id=bookmaker_id,
        )

    except (
        requests.RequestException,
        RuntimeError,
    ) as error:
        raise HTTPException(
            status_code=502,
            detail=str(error),
        ) from error

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "market": "1X2",
        **result,
    }


@app.post(
    "/database/odds/manual/fixture/{fixture_id}"
)
def create_manual_odds_snapshot(
    fixture_id: int,
    request: ManualOddsSnapshotRequest,
) -> dict[str, Any]:
    """
    Αποθηκεύει μία χειροκίνητη τρέχουσα παρατήρηση 1-X-2.

    Ο server ορίζει αυτόματα captured_at και
    market_updated_at στην τρέχουσα ώρα UTC.
    """

    fixture = get_fixture_by_id(
        fixture_id=fixture_id,
    )

    if fixture is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Ο αγώνας δεν υπάρχει στη βάση. "
                "Συγχρόνισέ τον πρώτα από το API-Football."
            ),
        )

    try:
        result = save_manual_1x2_snapshot(
            fixture_id=fixture_id,
            bookmaker_name=request.bookmaker_name,
            bookmaker_id=request.bookmaker_id,
            home_odds=request.home_odds,
            draw_odds=request.draw_odds,
            away_odds=request.away_odds,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return result


@app.get(
    "/database/odds/fixture/{fixture_id}"
)
def read_fixture_odds_history(
    fixture_id: int,
    bookmaker_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Επιστρέφει το αποθηκευμένο ιστορικό αποδόσεων
    ενός αγώνα, από το νεότερο προς το παλαιότερο.
    """

    try:
        snapshots = get_odds_snapshots(
            fixture_id=fixture_id,
            bookmaker_id=bookmaker_id,
            limit=limit,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "fixture_id": fixture_id,
        "bookmaker_filter": bookmaker_id,
        "count": len(snapshots),
        "snapshots": snapshots,
    }


@app.get(
    "/database/odds/latest/fixture/{fixture_id}"
)
def read_latest_fixture_odds(
    fixture_id: int,
    bookmaker_id: int | None = None,
) -> dict[str, Any]:
    """
    Επιστρέφει το νεότερο αποθηκευμένο 1-X-2 snapshot
    ανά bookmaker για έναν αγώνα.
    """

    snapshots = get_latest_odds_snapshots(
        fixture_id=fixture_id,
        bookmaker_id=bookmaker_id,
    )

    return {
        "fixture_id": fixture_id,
        "bookmaker_filter": bookmaker_id,
        "bookmakers_count": len(snapshots),
        "snapshots": snapshots,
    }


@app.get("/database/odds/count")
def read_odds_snapshot_count(
    fixture_id: int | None = None,
) -> dict[str, Any]:
    """
    Μετρά όλα τα odds snapshots ή μόνο εκείνα ενός fixture.
    """

    return {
        "fixture_id": fixture_id,
        "odds_snapshots": count_odds_snapshots(
            fixture_id=fixture_id,
        ),
    }


@app.get("/leagues/greece")
def get_greek_leagues() -> dict[str, Any]:
    """
    Ζητά από το API-Football όλες τις ελληνικές
    διοργανώσεις και επιστρέφει τα βασικά στοιχεία.
    """

    try:
        data = api_get(
            endpoint="/leagues",
            params={
                "country": "Greece",
            },
        )

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=502,
            detail=str(error),
        ) from error

    leagues: list[
        dict[str, Any]
    ] = []

    for item in data.get(
        "response",
        [],
    ):
        league = item.get(
            "league",
            {},
        )

        country = item.get(
            "country",
            {},
        )

        seasons = item.get(
            "seasons",
            [],
        )

        selected_season = next(
            (
                season
                for season in seasons
                if season.get(
                    "current"
                )
                is True
            ),
            None,
        )

        if (
            selected_season is None
            and seasons
        ):
            selected_season = max(
                seasons,
                key=lambda season: (
                    season.get(
                        "year",
                        0,
                    )
                ),
            )

        leagues.append(
            {
                "league_id": (
                    league.get("id")
                ),
                "league_name": (
                    league.get("name")
                ),
                "league_type": (
                    league.get("type")
                ),
                "country": (
                    country.get("name")
                ),
                "logo": (
                    league.get("logo")
                ),
                "season": (
                    selected_season.get(
                        "year"
                    )
                    if selected_season
                    else None
                ),
                "is_current": (
                    selected_season.get(
                        "current",
                        False,
                    )
                    if selected_season
                    else False
                ),
                "coverage": (
                    selected_season.get(
                        "coverage",
                        {},
                    )
                    if selected_season
                    else {}
                ),
            }
        )

    return {
        "count": len(leagues),
        "leagues": leagues,
    }


@app.get(
    "/leagues/super-league/seasons"
)
def get_super_league_seasons() -> dict[
    str,
    Any,
]:
    """
    Επιστρέφει όλες τις διαθέσιμες σεζόν
    της Super League 1 και την κάλυψη δεδομένων.
    """

    try:
        data = api_get(
            endpoint="/leagues",
            params={
                "id": SUPER_LEAGUE_ID,
            },
        )

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=502,
            detail=str(error),
        ) from error

    api_results = data.get(
        "response",
        [],
    )

    if not api_results:
        raise HTTPException(
            status_code=404,
            detail=(
                "Δεν βρέθηκε η Super League 1."
            ),
        )

    league_data = api_results[0]

    available_seasons: list[
        dict[str, Any]
    ] = []

    for season in league_data.get(
        "seasons",
        [],
    ):
        coverage = season.get(
            "coverage",
            {},
        )

        fixture_coverage = (
            coverage.get(
                "fixtures",
                {},
            )
        )

        available_seasons.append(
            {
                "year": (
                    season.get("year")
                ),
                "start": (
                    season.get("start")
                ),
                "end": (
                    season.get("end")
                ),
                "current": (
                    season.get(
                        "current",
                        False,
                    )
                ),
                "events": (
                    fixture_coverage.get(
                        "events",
                        False,
                    )
                ),
                "lineups": (
                    fixture_coverage.get(
                        "lineups",
                        False,
                    )
                ),
                "fixture_statistics": (
                    fixture_coverage.get(
                        "statistics_fixtures",
                        False,
                    )
                ),
                "player_statistics": (
                    fixture_coverage.get(
                        "statistics_players",
                        False,
                    )
                ),
                "standings": (
                    coverage.get(
                        "standings",
                        False,
                    )
                ),
                "players": (
                    coverage.get(
                        "players",
                        False,
                    )
                ),
                "injuries": (
                    coverage.get(
                        "injuries",
                        False,
                    )
                ),
                "predictions": (
                    coverage.get(
                        "predictions",
                        False,
                    )
                ),
                "odds": (
                    coverage.get(
                        "odds",
                        False,
                    )
                ),
            }
        )

    available_seasons.sort(
        key=lambda item: (
            item["year"] or 0
        ),
        reverse=True,
    )

    return {
        "league_id": SUPER_LEAGUE_ID,
        "league_name": "Super League 1",
        "seasons_count": len(
            available_seasons
        ),
        "seasons": available_seasons,
    }


@app.get(
    "/analysis/teams/super-league/{season}"
)
def get_super_league_team_statistics(
    season: int,
) -> dict[str, Any]:
    """
    Υπολογίζει τα βασικά στατιστικά των ομάδων
    χρησιμοποιώντας μόνο τη δική μας βάση.
    """

    fixtures = (
        get_completed_season_fixtures(
            season=season,
        )
    )

    teams = calculate_team_statistics(
        fixtures
    )

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "completed_fixtures": len(
            fixtures
        ),
        "teams_count": len(teams),
        "teams": teams,
    }


@app.get(
    "/analysis/home-away/super-league/{season}"
)
def get_super_league_home_away_statistics(
    season: int,
) -> dict[str, Any]:
    """
    Υπολογίζει την εντός και εκτός έδρας
    απόδοση των ομάδων.
    """

    fixtures = (
        get_completed_season_fixtures(
            season=season,
        )
    )

    analysis = (
        calculate_home_away_statistics(
            fixtures
        )
    )

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "completed_fixtures": len(
            fixtures
        ),
        **analysis,
    }


@app.get(
    "/analysis/poisson/super-league/{season}"
)
def get_poisson_match_prediction(
    season: int,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, Any]:
    """
    Δημιουργεί πρόβλεψη με το βασικό
    Bayesian-smoothed Poisson.
    """

    fixtures = (
        get_completed_season_fixtures(
            season=season,
        )
    )

    home_away_analysis = (
        calculate_home_away_statistics(
            fixtures
        )
    )

    try:
        prediction = predict_match(
            analysis=home_away_analysis,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "fixtures_used": len(
            fixtures
        ),
        "warning": (
            "Η πρόβλεψη βασίζεται αποκλειστικά "
            "στα ιστορικά δεδομένα της "
            "συγκεκριμένης σεζόν."
        ),
        **prediction,
    }


@app.get(
    "/analysis/poisson-before-date/"
    "super-league/{season}"
)
def get_poisson_prediction_before_date(
    season: int,
    cutoff_date: str,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, Any]:
    """
    Δημιουργεί πρόβλεψη χρησιμοποιώντας μόνο
    αγώνες πριν από την ημερομηνία cutoff_date.
    """

    fixtures = (
        get_completed_fixtures_before_date(
            league_id=SUPER_LEAGUE_ID,
            season=season,
            cutoff_date=cutoff_date,
        )
    )

    if not fixtures:
        raise HTTPException(
            status_code=404,
            detail=(
                "Δεν υπάρχουν ολοκληρωμένοι αγώνες "
                "πριν από τη συγκεκριμένη ημερομηνία."
            ),
        )

    home_away_analysis = (
        calculate_home_away_statistics(
            fixtures
        )
    )

    try:
        prediction = predict_match(
            analysis=home_away_analysis,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "cutoff_date": cutoff_date,
        "fixtures_used": len(
            fixtures
        ),
        "warning": (
            "Χρησιμοποιούνται μόνο αποτελέσματα "
            "πριν από την ημερομηνία πρόβλεψης."
        ),
        **prediction,
    }


@app.get(
    "/analysis/ensemble/super-league/{season}"
)
def get_ensemble_match_prediction(
    season: int,
    home_team_id: int,
    away_team_id: int,
    force_refit: bool = False,
) -> dict[str, Any]:
    """
    Δημιουργεί πρόβλεψη Probability Ensemble v0.5:

        60% Bayesian-smoothed Poisson
        40% Poisson MLE
    """

    (
        fixtures,
        context,
        context_cache_used,
    ) = get_ensemble_context_for_season(
        season=season,
        force_refit=force_refit,
    )

    try:
        prediction = predict_match_ensemble(
            context=context,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "completed_fixtures": len(
            fixtures
        ),
        "context_cache_used": (
            context_cache_used
        ),
        "warning": (
            "Οι πιθανότητες αποτελούν στατιστικές "
            "εκτιμήσεις και όχι βεβαιότητα."
        ),
        **prediction,
    }


@app.post(
    "/analysis/value/1x2/super-league/{season}"
)
def post_ensemble_value_analysis(
    season: int,
    request: ValueAnalysisRequest,
) -> dict[str, Any]:
    """
    Δέχεται ομάδες και δεκαδικές αποδόσεις 1-X-2.

    Επιστρέφει:

    - τελική ensemble πρόβλεψη,
    - bookmaker overround,
    - fair πιθανότητες αγοράς,
    - edge,
    - Expected Value,
    - quarter Kelly με ανώτατο όριο bankroll.
    """

    if (
        request.home_team_id
        == request.away_team_id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Η γηπεδούχος και η "
                "φιλοξενούμενη ομάδα "
                "δεν μπορούν να είναι ίδιες."
            ),
        )

    (
        fixtures,
        context,
        context_cache_used,
    ) = get_ensemble_context_for_season(
        season=season,
        force_refit=(
            request.force_refit
        ),
    )

    try:
        analysis = (
            analyze_match_value_1x2(
                context=context,
                home_team_id=(
                    request.home_team_id
                ),
                away_team_id=(
                    request.away_team_id
                ),
                home_odds=(
                    request.home_odds
                ),
                draw_odds=(
                    request.draw_odds
                ),
                away_odds=(
                    request.away_odds
                ),
                min_edge_percent=(
                    request.min_edge_percent
                ),
                min_expected_value_percent=(
                    request
                    .min_expected_value_percent
                ),
                kelly_multiplier=(
                    request.kelly_multiplier
                ),
                max_bankroll_fraction=(
                    request
                    .max_bankroll_fraction
                ),
            )
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "completed_fixtures": len(
            fixtures
        ),
        "context_cache_used": (
            context_cache_used
        ),
        **analysis,
    }


@app.get(
    "/analysis/ensemble-before-date/"
    "super-league/{season}"
)
def get_ensemble_prediction_before_date(
    season: int,
    cutoff_date: str,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, Any]:
    """
    Δημιουργεί ensemble πρόβλεψη χωρίς temporal leak.

    Χρησιμοποιεί αποκλειστικά αγώνες πριν από
    το cutoff_date και είναι κατάλληλο για
    ιστορικούς ελέγχους.
    """

    validated_cutoff_date = (
        validate_cutoff_date(
            cutoff_date=cutoff_date,
        )
    )

    (
        fixtures,
        context,
    ) = get_ensemble_context_before_date(
        season=season,
        cutoff_date=(
            validated_cutoff_date
        ),
    )

    try:
        prediction = predict_match_ensemble(
            context=context,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "cutoff_date": (
            validated_cutoff_date
        ),
        "fixtures_used": len(
            fixtures
        ),
        "context_cache_used": False,
        "historical_mode": True,
        "warning": (
            "Η πρόβλεψη χρησιμοποιεί μόνο "
            "αποτελέσματα πριν από το cutoff_date."
        ),
        **prediction,
    }


@app.post(
    "/analysis/value-before-date/1x2/"
    "super-league/{season}"
)
def post_value_analysis_before_date(
    season: int,
    cutoff_date: str,
    request: ValueAnalysisRequest,
) -> dict[str, Any]:
    """
    Ιστορική ανάλυση value χωρίς χρήση
    μεταγενέστερων αποτελεσμάτων.

    Οι αποδόσεις πρέπει να είναι εκείνες που
    ήταν διαθέσιμες πριν από τον αγώνα.
    """

    if (
        request.home_team_id
        == request.away_team_id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Η γηπεδούχος και η "
                "φιλοξενούμενη ομάδα "
                "δεν μπορούν να είναι ίδιες."
            ),
        )

    validated_cutoff_date = (
        validate_cutoff_date(
            cutoff_date=cutoff_date,
        )
    )

    (
        fixtures,
        context,
    ) = get_ensemble_context_before_date(
        season=season,
        cutoff_date=(
            validated_cutoff_date
        ),
    )

    try:
        analysis = (
            analyze_match_value_1x2(
                context=context,
                home_team_id=(
                    request.home_team_id
                ),
                away_team_id=(
                    request.away_team_id
                ),
                home_odds=(
                    request.home_odds
                ),
                draw_odds=(
                    request.draw_odds
                ),
                away_odds=(
                    request.away_odds
                ),
                min_edge_percent=(
                    request.min_edge_percent
                ),
                min_expected_value_percent=(
                    request
                    .min_expected_value_percent
                ),
                kelly_multiplier=(
                    request.kelly_multiplier
                ),
                max_bankroll_fraction=(
                    request
                    .max_bankroll_fraction
                ),
            )
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "cutoff_date": (
            validated_cutoff_date
        ),
        "fixtures_used": len(
            fixtures
        ),
        "context_cache_used": False,
        "historical_mode": True,
        "warning": (
            "Για έγκυρο ιστορικό έλεγχο, "
            "οι αποδόσεις πρέπει να είναι "
            "προγενέστερες της έναρξης του αγώνα."
        ),
        **analysis,
    }


@app.delete(
    "/analysis/ensemble-cache/"
    "super-league/{season}"
)
def clear_ensemble_cache(
    season: int,
) -> dict[str, Any]:
    """
    Καθαρίζει χειροκίνητα το ensemble cache
    μιας σεζόν.
    """

    was_cached = (
        season
        in ENSEMBLE_CONTEXT_CACHE
    )

    invalidate_ensemble_cache(
        season=season
    )

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "was_cached": was_cached,
        "cache_cleared": True,
    }


@app.get(
    "/backtest/poisson/super-league/{season}"
)
def run_poisson_backtest(
    season: int,
    min_previous_location_matches: int = 5,
) -> dict[str, Any]:
    """
    Ελέγχει το βασικό Poisson σε ιστορικούς αγώνες
    χωρίς χρήση μελλοντικών αποτελεσμάτων.
    """

    fixtures = (
        get_completed_season_fixtures(
            season=season,
        )
    )

    try:
        results = backtest_poisson_model(
            fixtures=fixtures,
            min_previous_location_matches=(
                min_previous_location_matches
            ),
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    return {
        "league_id": SUPER_LEAGUE_ID,
        "season": season,
        "method": (
            "Walk-forward backtesting χωρίς χρήση "
            "μελλοντικών αποτελεσμάτων."
        ),
        **results,
    }