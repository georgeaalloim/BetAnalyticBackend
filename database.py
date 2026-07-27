from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


DATABASE_PATH = Path(__file__).resolve().parent / "betanalytic.db"


ODDS_SNAPSHOT_COLUMNS = (
    "snapshot_key",
    "fixture_id",
    "league_id",
    "season",
    "fixture_date",
    "bookmaker_id",
    "bookmaker_name",
    "bet_id",
    "bet_name",
    "market",
    "home_odds",
    "draw_odds",
    "away_odds",
    "market_updated_at",
    "captured_at",
    "source",
)


def get_connection() -> sqlite3.Connection:
    """
    Ανοίγει σύνδεση με τη βάση δεδομένων SQLite.
    """

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def initialize_database() -> None:
    """
    Δημιουργεί τους πίνακες fixtures και odds_snapshots,
    χωρίς να διαγράφει ή να αλλάζει τα υπάρχοντα δεδομένα.
    """

    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fixtures (
                fixture_id INTEGER PRIMARY KEY,
                league_id INTEGER NOT NULL,
                season INTEGER NOT NULL,
                fixture_date TEXT,
                status TEXT,
                home_team_id INTEGER NOT NULL,
                home_team_name TEXT NOT NULL,
                away_team_id INTEGER NOT NULL,
                away_team_name TEXT NOT NULL,
                home_goals INTEGER,
                away_goals INTEGER
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                odds_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_key TEXT NOT NULL UNIQUE,
                fixture_id INTEGER NOT NULL,
                league_id INTEGER,
                season INTEGER,
                fixture_date TEXT,
                bookmaker_id INTEGER,
                bookmaker_name TEXT NOT NULL,
                bet_id INTEGER,
                bet_name TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT '1X2',
                home_odds REAL NOT NULL CHECK(home_odds > 1.0),
                draw_odds REAL NOT NULL CHECK(draw_odds > 1.0),
                away_odds REAL NOT NULL CHECK(away_odds > 1.0),
                market_updated_at TEXT,
                captured_at TEXT NOT NULL,
                source TEXT NOT NULL,
                FOREIGN KEY(fixture_id)
                    REFERENCES fixtures(fixture_id)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_fixtures_league_season_date
            ON fixtures (
                league_id,
                season,
                fixture_date
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_odds_snapshots_fixture_time
            ON odds_snapshots (
                fixture_id,
                market_updated_at,
                captured_at
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_odds_snapshots_bookmaker
            ON odds_snapshots (
                fixture_id,
                bookmaker_id,
                bookmaker_name
            )
            """
        )

        connection.commit()


def save_fixtures(
    api_fixtures: list[dict[str, Any]],
) -> int:
    """
    Αποθηκεύει ή ενημερώνει τους αγώνες
    που πήραμε από το API-Football.
    """

    rows_to_save: list[tuple[Any, ...]] = []

    for item in api_fixtures:
        fixture = item.get("fixture", {})
        league = item.get("league", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})

        home_team = teams.get("home", {})
        away_team = teams.get("away", {})
        status = fixture.get("status", {})

        fixture_id = fixture.get("id")
        league_id = league.get("id")
        season = league.get("season")

        home_team_id = home_team.get("id")
        home_team_name = home_team.get("name")

        away_team_id = away_team.get("id")
        away_team_name = away_team.get("name")

        if None in (
            fixture_id,
            league_id,
            season,
            home_team_id,
            home_team_name,
            away_team_id,
            away_team_name,
        ):
            continue

        rows_to_save.append(
            (
                fixture_id,
                league_id,
                season,
                fixture.get("date"),
                status.get("short"),
                home_team_id,
                home_team_name,
                away_team_id,
                away_team_name,
                goals.get("home"),
                goals.get("away"),
            )
        )

    if not rows_to_save:
        return 0

    with get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO fixtures (
                fixture_id,
                league_id,
                season,
                fixture_date,
                status,
                home_team_id,
                home_team_name,
                away_team_id,
                away_team_name,
                home_goals,
                away_goals
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(fixture_id) DO UPDATE SET
                league_id = excluded.league_id,
                season = excluded.season,
                fixture_date = excluded.fixture_date,
                status = excluded.status,
                home_team_id = excluded.home_team_id,
                home_team_name = excluded.home_team_name,
                away_team_id = excluded.away_team_id,
                away_team_name = excluded.away_team_name,
                home_goals = excluded.home_goals,
                away_goals = excluded.away_goals
            """,
            rows_to_save,
        )

        connection.commit()

    return len(rows_to_save)


def count_fixtures(
    league_id: int,
    season: int,
) -> int:
    """
    Μετρά πόσοι αγώνες υπάρχουν στη βάση
    για συγκεκριμένη διοργάνωση και σεζόν.
    """

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM fixtures
            WHERE league_id = ?
              AND season = ?
            """,
            (league_id, season),
        ).fetchone()

    if row is None:
        return 0

    return int(row["total"])


def get_fixture_by_id(
    fixture_id: int,
) -> dict[str, Any] | None:
    """
    Επιστρέφει έναν αγώνα από τη βάση με βάση το fixture_id.
    """

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM fixtures
            WHERE fixture_id = ?
            """,
            (fixture_id,),
        ).fetchone()

    if row is None:
        return None

    return dict(row)


def get_saved_fixtures(
    league_id: int,
    season: int,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Επιστρέφει αποθηκευμένους αγώνες
    από τη δική μας βάση.
    """

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM fixtures
            WHERE league_id = ?
              AND season = ?
            ORDER BY fixture_date DESC
            LIMIT ?
            """,
            (league_id, season, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def get_completed_fixtures(
    league_id: int,
    season: int,
) -> list[dict[str, Any]]:
    """
    Επιστρέφει όλους τους ολοκληρωμένους αγώνες
    μιας διοργάνωσης και σεζόν.
    """

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                fixture_id,
                fixture_date,
                home_team_id,
                home_team_name,
                away_team_id,
                away_team_name,
                home_goals,
                away_goals
            FROM fixtures
            WHERE league_id = ?
              AND season = ?
              AND status = 'FT'
              AND home_goals IS NOT NULL
              AND away_goals IS NOT NULL
            ORDER BY fixture_date ASC
            """,
            (league_id, season),
        ).fetchall()

    return [dict(row) for row in rows]


def get_completed_fixtures_before_date(
    league_id: int,
    season: int,
    cutoff_date: str,
) -> list[dict[str, Any]]:
    """
    Επιστρέφει μόνο τους ολοκληρωμένους αγώνες
    που έγιναν πριν από συγκεκριμένη ημερομηνία.
    """

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                fixture_id,
                fixture_date,
                home_team_id,
                home_team_name,
                away_team_id,
                away_team_name,
                home_goals,
                away_goals
            FROM fixtures
            WHERE league_id = ?
              AND season = ?
              AND status = 'FT'
              AND fixture_date < ?
              AND home_goals IS NOT NULL
              AND away_goals IS NOT NULL
            ORDER BY fixture_date ASC
            """,
            (
                league_id,
                season,
                cutoff_date,
            ),
        ).fetchall()

    return [dict(row) for row in rows]


def save_odds_snapshots(
    snapshots: list[dict[str, Any]],
) -> dict[str, int]:
    """
    Αποθηκεύει κανονικοποιημένα snapshots αποδόσεων 1-X-2.

    Το snapshot_key είναι μοναδικό. Αν το API επιστρέψει
    ξανά ακριβώς το ίδιο snapshot, δεν δημιουργείται διπλότυπο.
    """

    if not snapshots:
        return {
            "received": 0,
            "inserted": 0,
            "duplicates_skipped": 0,
        }

    rows_to_save: list[tuple[Any, ...]] = []

    for snapshot in snapshots:
        missing_columns = [
            column
            for column in ODDS_SNAPSHOT_COLUMNS
            if column not in snapshot
        ]

        if missing_columns:
            missing_text = ", ".join(missing_columns)
            raise ValueError(
                "Λείπουν πεδία από το odds snapshot: "
                f"{missing_text}."
            )

        home_odds = float(snapshot["home_odds"])
        draw_odds = float(snapshot["draw_odds"])
        away_odds = float(snapshot["away_odds"])

        if min(home_odds, draw_odds, away_odds) <= 1.0:
            raise ValueError(
                "Όλες οι δεκαδικές αποδόσεις πρέπει "
                "να είναι μεγαλύτερες από 1."
            )

        rows_to_save.append(
            (
                snapshot["snapshot_key"],
                int(snapshot["fixture_id"]),
                snapshot["league_id"],
                snapshot["season"],
                snapshot["fixture_date"],
                snapshot["bookmaker_id"],
                str(snapshot["bookmaker_name"]),
                snapshot["bet_id"],
                str(snapshot["bet_name"]),
                str(snapshot["market"]),
                home_odds,
                draw_odds,
                away_odds,
                snapshot["market_updated_at"],
                str(snapshot["captured_at"]),
                str(snapshot["source"]),
            )
        )

    with get_connection() as connection:
        changes_before = connection.total_changes

        connection.executemany(
            """
            INSERT OR IGNORE INTO odds_snapshots (
                snapshot_key,
                fixture_id,
                league_id,
                season,
                fixture_date,
                bookmaker_id,
                bookmaker_name,
                bet_id,
                bet_name,
                market,
                home_odds,
                draw_odds,
                away_odds,
                market_updated_at,
                captured_at,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_save,
        )

        inserted = connection.total_changes - changes_before
        connection.commit()

    return {
        "received": len(rows_to_save),
        "inserted": inserted,
        "duplicates_skipped": len(rows_to_save) - inserted,
    }


def count_odds_snapshots(
    fixture_id: int | None = None,
) -> int:
    """
    Μετρά όλα τα odds snapshots ή μόνο εκείνα ενός αγώνα.
    """

    with get_connection() as connection:
        if fixture_id is None:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM odds_snapshots
                """
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM odds_snapshots
                WHERE fixture_id = ?
                """,
                (fixture_id,),
            ).fetchone()

    if row is None:
        return 0

    return int(row["total"])


def get_odds_snapshots(
    fixture_id: int,
    bookmaker_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Επιστρέφει το ιστορικό αποδόσεων ενός αγώνα,
    από το νεότερο snapshot προς το παλαιότερο.
    """

    if limit < 1 or limit > 1000:
        raise ValueError(
            "Το limit πρέπει να είναι από 1 έως 1000."
        )

    query = """
        SELECT *
        FROM odds_snapshots
        WHERE fixture_id = ?
    """
    parameters: list[Any] = [fixture_id]

    if bookmaker_id is not None:
        query += " AND bookmaker_id = ?"
        parameters.append(bookmaker_id)

    query += """
        ORDER BY
            COALESCE(market_updated_at, captured_at) DESC,
            odds_snapshot_id DESC
        LIMIT ?
    """
    parameters.append(limit)

    with get_connection() as connection:
        rows = connection.execute(
            query,
            tuple(parameters),
        ).fetchall()

    return [dict(row) for row in rows]


def get_latest_odds_snapshots(
    fixture_id: int,
    bookmaker_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Επιστρέφει το νεότερο snapshot ανά bookmaker
    για έναν συγκεκριμένο αγώνα.
    """

    snapshots = get_odds_snapshots(
        fixture_id=fixture_id,
        bookmaker_id=bookmaker_id,
        limit=1000,
    )

    latest_by_bookmaker: dict[str, dict[str, Any]] = {}

    for snapshot in snapshots:
        bookmaker_key = (
            str(snapshot.get("bookmaker_id"))
            if snapshot.get("bookmaker_id") is not None
            else f"name:{snapshot.get('bookmaker_name')}"
        )

        if bookmaker_key not in latest_by_bookmaker:
            latest_by_bookmaker[bookmaker_key] = snapshot

    return list(latest_by_bookmaker.values())