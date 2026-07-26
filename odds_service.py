from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from database import (
    get_fixture_by_id,
    initialize_database,
    save_fixtures,
    save_odds_snapshots,
)
from football_api import (
    api_get,
    api_get_all_pages,
)


MATCH_WINNER_MARKET_NAMES = {
    "1x2",
    "full time result",
    "match result",
    "match winner",
    "winner",
}

HOME_VALUE_NAMES = {
    "1",
    "home",
}

DRAW_VALUE_NAMES = {
    "draw",
    "x",
}

AWAY_VALUE_NAMES = {
    "2",
    "away",
}


def utc_now_iso() -> str:
    """
    Επιστρέφει την τρέχουσα ώρα UTC σε ISO μορφή.
    """

    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_text(
    value: Any,
) -> str:
    """
    Κανονικοποιεί κείμενο για ασφαλείς συγκρίσεις.
    """

    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("-", " ")
        .split()
    )


def parse_decimal_odd(
    value: Any,
) -> float | None:
    """
    Μετατρέπει μία τιμή απόδοσης σε έγκυρο float.
    """

    try:
        odd = float(value)

    except (TypeError, ValueError):
        return None

    if not math.isfinite(odd) or odd <= 1.0:
        return None

    return odd


def classify_1x2_value(
    value_name: Any,
) -> str | None:
    """
    Μετατρέπει Home/Draw/Away ή 1/X/2
    στις εσωτερικές ετικέτες HOME/DRAW/AWAY.
    """

    normalized_value = normalize_text(
        value_name
    )

    if normalized_value in HOME_VALUE_NAMES:
        return "HOME"

    if normalized_value in DRAW_VALUE_NAMES:
        return "DRAW"

    if normalized_value in AWAY_VALUE_NAMES:
        return "AWAY"

    return None


def is_match_winner_market(
    bet_name: Any,
    values: list[dict[str, Any]],
) -> bool:
    """
    Ελέγχει αν ένα bet είναι η κανονική full-time αγορά 1-X-2.

    Δεν αρκεί να υπάρχουν τιμές Home/Draw/Away, επειδή
    την ίδια μορφή μπορεί να έχουν αγορές πρώτου ημιχρόνου
    ή άλλων χρονικών περιόδων.
    """

    del values

    normalized_bet_name = normalize_text(
        bet_name
    )

    return (
        normalized_bet_name
        in MATCH_WINNER_MARKET_NAMES
    )


def create_snapshot_key(
    snapshot: dict[str, Any],
) -> str:
    """
    Δημιουργεί σταθερό SHA-256 κλειδί για αποφυγή διπλοτύπων.
    """

    key_parts = (
        snapshot.get("fixture_id"),
        snapshot.get("bookmaker_id"),
        snapshot.get("bookmaker_name"),
        snapshot.get("bet_id"),
        snapshot.get("bet_name"),
        snapshot.get("home_odds"),
        snapshot.get("draw_odds"),
        snapshot.get("away_odds"),
        snapshot.get("market_updated_at"),
        snapshot.get("source"),
    )

    key_text = "|".join(
        "" if value is None else str(value)
        for value in key_parts
    )

    return hashlib.sha256(
        key_text.encode("utf-8")
    ).hexdigest()


def extract_1x2_snapshots(
    api_odds_items: list[dict[str, Any]],
    captured_at: str | None = None,
) -> list[dict[str, Any]]:
    """
    Μετατρέπει την απόκριση /odds σε κανονικοποιημένα
    snapshots HOME/DRAW/AWAY ανά bookmaker.
    """

    capture_timestamp = (
        captured_at or utc_now_iso()
    )

    snapshots: list[dict[str, Any]] = []

    for item in api_odds_items:
        fixture = item.get(
            "fixture",
            {},
        )
        league = item.get(
            "league",
            {},
        )

        fixture_id = fixture.get("id")

        if fixture_id is None:
            continue

        market_updated_at = item.get(
            "update"
        )

        for bookmaker in item.get(
            "bookmakers",
            [],
        ):
            bookmaker_name = bookmaker.get(
                "name"
            )

            if not bookmaker_name:
                continue

            for bet in bookmaker.get(
                "bets",
                [],
            ):
                values = bet.get(
                    "values",
                    [],
                )

                if not isinstance(values, list):
                    continue

                if not is_match_winner_market(
                    bet_name=bet.get("name"),
                    values=values,
                ):
                    continue

                odds_by_label: dict[str, float] = {}

                for value in values:
                    label = classify_1x2_value(
                        value.get("value")
                    )

                    odd = parse_decimal_odd(
                        value.get("odd")
                    )

                    if label is None or odd is None:
                        continue

                    odds_by_label[label] = odd

                if not {
                    "HOME",
                    "DRAW",
                    "AWAY",
                }.issubset(odds_by_label):
                    continue

                snapshot: dict[str, Any] = {
                    "fixture_id": int(fixture_id),
                    "league_id": league.get("id"),
                    "season": league.get("season"),
                    "fixture_date": fixture.get("date"),
                    "bookmaker_id": bookmaker.get("id"),
                    "bookmaker_name": str(bookmaker_name),
                    "bet_id": bet.get("id"),
                    "bet_name": str(
                        bet.get("name")
                        or "Match Winner"
                    ),
                    "market": "1X2",
                    "home_odds": odds_by_label["HOME"],
                    "draw_odds": odds_by_label["DRAW"],
                    "away_odds": odds_by_label["AWAY"],
                    "market_updated_at": market_updated_at,
                    "captured_at": capture_timestamp,
                    "source": "api-football-prematch",
                }

                snapshot["snapshot_key"] = (
                    create_snapshot_key(snapshot)
                )

                snapshots.append(snapshot)

    return snapshots


def ensure_fixture_saved(
    fixture_id: int,
) -> tuple[dict[str, Any], int]:
    """
    Βεβαιώνεται ότι ο αγώνας υπάρχει στη SQLite.

    Επιστρέφει τον αγώνα και τον αριθμό API requests
    που χρειάστηκαν για αυτό το βήμα.
    """

    existing_fixture = get_fixture_by_id(
        fixture_id=fixture_id,
    )

    if existing_fixture is not None:
        return existing_fixture, 0

    fixture_data = api_get(
        endpoint="/fixtures",
        params={
            "id": fixture_id,
        },
    )

    api_requests = 1
    fixture_items = fixture_data.get(
        "response",
        [],
    )

    if not fixture_items:
        raise ValueError(
            "Δεν βρέθηκε ο αγώνας στο API-Football."
        )

    saved = save_fixtures(
        api_fixtures=fixture_items,
    )

    if saved < 1:
        raise ValueError(
            "Ο αγώνας βρέθηκε αλλά δεν μπόρεσε "
            "να αποθηκευτεί στη βάση."
        )

    saved_fixture = get_fixture_by_id(
        fixture_id=fixture_id,
    )

    if saved_fixture is None:
        raise RuntimeError(
            "Ο αγώνας αποθηκεύτηκε αλλά δεν διαβάστηκε "
            "ξανά από τη βάση."
        )

    return saved_fixture, api_requests


def sync_fixture_1x2_odds(
    fixture_id: int,
    bookmaker_id: int | None = None,
) -> dict[str, Any]:
    """
    Κατεβάζει pre-match 1-X-2 αποδόσεις ενός αγώνα
    και αποθηκεύει snapshots στη SQLite.
    """

    if fixture_id <= 0:
        raise ValueError(
            "Το fixture_id πρέπει να είναι θετικός αριθμός."
        )

    if bookmaker_id is not None and bookmaker_id <= 0:
        raise ValueError(
            "Το bookmaker_id πρέπει να είναι θετικός αριθμός."
        )

    initialize_database()

    fixture, fixture_api_requests = ensure_fixture_saved(
        fixture_id=fixture_id,
    )

    params: dict[str, Any] = {
        "fixture": fixture_id,
    }

    if bookmaker_id is not None:
        params["bookmaker"] = bookmaker_id

    odds_data = api_get_all_pages(
        endpoint="/odds",
        params=params,
        max_pages=20,
    )

    snapshots = extract_1x2_snapshots(
        api_odds_items=odds_data.get(
            "response",
            [],
        )
    )

    database_result = save_odds_snapshots(
        snapshots=snapshots,
    )

    return {
        "fixture": fixture,
        "bookmaker_filter": bookmaker_id,
        "api_pages_fetched": odds_data.get(
            "pages_fetched",
            1,
        ),
        "api_requests_used": (
            fixture_api_requests
            + int(
                odds_data.get(
                    "pages_fetched",
                    1,
                )
            )
        ),
        "api_odds_items_received": len(
            odds_data.get(
                "response",
                [],
            )
        ),
        "normalized_1x2_snapshots": len(
            snapshots
        ),
        "database": database_result,
        "message": (
            "Δεν βρέθηκαν διαθέσιμες pre-match αποδόσεις "
            "1-X-2 για αυτόν τον αγώνα."
            if not snapshots
            else "Οι διαθέσιμες αποδόσεις αποθηκεύτηκαν."
        ),
    }


def create_manual_1x2_snapshot(
    fixture: dict[str, Any],
    bookmaker_name: str,
    home_odds: float,
    draw_odds: float,
    away_odds: float,
    bookmaker_id: int | None = None,
) -> dict[str, Any]:
    """
    Δημιουργεί χειροκίνητο snapshot με τρέχον timestamp.

    Δεν επιτρέπει backdating. Έτσι ένα χειροκίνητο snapshot
    δεν μπορεί να παρουσιαστεί ως παλαιότερη ιστορική απόδοση.
    """

    cleaned_bookmaker_name = bookmaker_name.strip()

    if not cleaned_bookmaker_name:
        raise ValueError(
            "Το bookmaker_name δεν μπορεί να είναι κενό."
        )

    parsed_home = parse_decimal_odd(home_odds)
    parsed_draw = parse_decimal_odd(draw_odds)
    parsed_away = parse_decimal_odd(away_odds)

    if None in (
        parsed_home,
        parsed_draw,
        parsed_away,
    ):
        raise ValueError(
            "Όλες οι δεκαδικές αποδόσεις πρέπει "
            "να είναι μεγαλύτερες από 1."
        )

    captured_at = utc_now_iso()

    snapshot: dict[str, Any] = {
        "fixture_id": int(fixture["fixture_id"]),
        "league_id": fixture.get("league_id"),
        "season": fixture.get("season"),
        "fixture_date": fixture.get("fixture_date"),
        "bookmaker_id": bookmaker_id,
        "bookmaker_name": cleaned_bookmaker_name,
        "bet_id": None,
        "bet_name": "Match Winner",
        "market": "1X2",
        "home_odds": parsed_home,
        "draw_odds": parsed_draw,
        "away_odds": parsed_away,
        "market_updated_at": captured_at,
        "captured_at": captured_at,
        "source": "manual-current-observation",
    }

    snapshot["snapshot_key"] = (
        create_snapshot_key(snapshot)
    )

    return snapshot


def save_manual_1x2_snapshot(
    fixture_id: int,
    bookmaker_name: str,
    home_odds: float,
    draw_odds: float,
    away_odds: float,
    bookmaker_id: int | None = None,
) -> dict[str, Any]:
    """
    Αποθηκεύει χειροκίνητη τρέχουσα παρατήρηση 1-X-2.
    """

    initialize_database()

    fixture = get_fixture_by_id(
        fixture_id=fixture_id,
    )

    if fixture is None:
        raise ValueError(
            "Ο αγώνας δεν υπάρχει στη βάση. "
            "Συγχρόνισέ τον πρώτα από το API-Football."
        )

    snapshot = create_manual_1x2_snapshot(
        fixture=fixture,
        bookmaker_name=bookmaker_name,
        bookmaker_id=bookmaker_id,
        home_odds=home_odds,
        draw_odds=draw_odds,
        away_odds=away_odds,
    )

    database_result = save_odds_snapshots(
        snapshots=[snapshot],
    )

    return {
        "fixture": fixture,
        "snapshot": snapshot,
        "database": database_result,
    }
