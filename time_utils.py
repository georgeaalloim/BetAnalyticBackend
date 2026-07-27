from __future__ import annotations

from datetime import datetime, timezone


UTC = timezone.utc


def utc_now() -> datetime:
    """Επιστρέφει την τρέχουσα ώρα σε UTC."""

    return datetime.now(tz=UTC)


def parse_iso_datetime(value: str) -> datetime:
    """
    Μετατρέπει ISO ημερομηνία/ώρα σε timezone-aware UTC datetime.

    Δέχεται τόσο κατάληξη ``Z`` όσο και αριθμητικό UTC offset.
    Αν λείπει timezone, θεωρείται UTC ώστε να μην εξαρτάται η
    αυτοματοποίηση από τη ζώνη ώρας του GitHub runner.
    """

    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError("Η ημερομηνία δεν μπορεί να είναι κενή.")

    normalized = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    parsed = datetime.fromisoformat(normalized)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    return parsed.astimezone(UTC)


def to_iso_z(value: datetime) -> str:
    """Μορφοποιεί timezone-aware datetime σε σταθερό ISO UTC με ``Z``."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    utc_value = value.astimezone(UTC).replace(microsecond=0)
    return utc_value.isoformat().replace("+00:00", "Z")
