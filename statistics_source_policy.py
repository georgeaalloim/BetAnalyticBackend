from __future__ import annotations

from typing import Any, Mapping


API_FOOTBALL_SOURCE_PREFIXES = (
    "API-Football",
    "API-Football Free fixture details",
)
FOOTBALL_DATA_SOURCE_PREFIXES = (
    "Football-Data.co.uk",
)

STAT_FIELDS = (
    "home_corners",
    "away_corners",
    "home_yellow_cards",
    "away_yellow_cards",
    "home_red_cards",
    "away_red_cards",
    "home_total_shots",
    "away_total_shots",
    "home_shots_on_target",
    "away_shots_on_target",
    "home_fouls",
    "away_fouls",
    "home_offsides",
    "away_offsides",
)

STAT_PAIRS = (
    ("home_total_shots", "away_total_shots"),
    ("home_shots_on_target", "away_shots_on_target"),
    ("home_fouls", "away_fouls"),
    ("home_yellow_cards", "away_yellow_cards"),
    ("home_red_cards", "away_red_cards"),
    ("home_offsides", "away_offsides"),
    ("home_corners", "away_corners"),
)


def clean_source(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def source_key(value: Any) -> str:
    source = clean_source(value).lower()
    if not source:
        return "missing"
    if "mixed" in source or " + " in source:
        return "mixed"
    if source.startswith("api-football"):
        return "api_football"
    if source.startswith("football-data.co.uk"):
        return "football_data"
    return "other"


def source_priority(value: Any) -> int:
    key = source_key(value)
    return {
        "api_football": 300,
        "football_data": 200,
        "other": 100,
        "missing": 0,
        "mixed": -100,
    }[key]


def is_mixed_source(value: Any) -> bool:
    return source_key(value) == "mixed"


def available_stat_pairs(record: Mapping[str, Any]) -> int:
    return sum(
        1
        for home_field, away_field in STAT_PAIRS
        if record.get(home_field) is not None and record.get(away_field) is not None
    )


def has_any_statistics(record: Mapping[str, Any]) -> bool:
    return available_stat_pairs(record) > 0


def same_provider(first: Any, second: Any) -> bool:
    first_key = source_key(first)
    second_key = source_key(second)
    return first_key == second_key and first_key not in {"missing", "mixed"}


def choose_whole_record(
    existing: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Επιλέγει ολόκληρο snapshot από έναν πάροχο. Δεν ενώνει πεδία διαφορετικών
    πηγών. Για τον ίδιο πάροχο, κρατά μόνο ελλείποντα πεδία από το παλιό
    snapshot, ώστε μια προσωρινά ελλιπής απόκριση να μη διαγράψει δεδομένα.
    """
    new = dict(candidate)
    if not existing:
        return new

    old = dict(existing)
    old_source = old.get("source")
    new_source = new.get("source")

    if same_provider(old_source, new_source):
        merged = dict(old)
        merged.update({key: value for key, value in new.items() if value is not None and value != ""})
        merged["source"] = clean_source(new_source) or clean_source(old_source)
        return merged

    old_priority = source_priority(old_source)
    new_priority = source_priority(new_source)
    if new_priority > old_priority:
        return new
    if new_priority < old_priority:
        return old

    # Ίση προτεραιότητα: προτιμάται το πληρέστερο snapshot, και σε ισοπαλία
    # το νεότερο candidate ώστε να περνούν διορθώσεις του ίδιου επιπέδου.
    old_pairs = available_stat_pairs(old)
    new_pairs = available_stat_pairs(new)
    if new_pairs >= old_pairs:
        return new
    return old
