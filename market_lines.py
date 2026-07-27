from __future__ import annotations

from typing import Any, Iterable


DEFAULT_TOTAL_GOALS_LINES = (
    0.5,
    1.5,
    2.5,
    3.5,
    4.5,
    5.5,
)

DEFAULT_RELEVANCE_WINDOW = 1.0


def _normalize_half_line(line: float) -> float:
    value = float(line)
    doubled = value * 2.0

    if value < 0.5 or abs(doubled - round(doubled)) > 1e-9:
        raise ValueError(
            "Οι γραμμές πρέπει να είναι θετικοί αριθμοί που τελειώνουν σε .5."
        )

    if int(round(doubled)) % 2 == 0:
        raise ValueError(
            "Οι γραμμές πρέπει να τελειώνουν σε .5, π.χ. 1.5 ή 2.5."
        )

    return value


def build_total_market_lines(
    score_probabilities: Iterable[dict[str, Any]],
    lines: Iterable[float] = DEFAULT_TOTAL_GOALS_LINES,
) -> list[dict[str, float]]:
    """
    Μετατρέπει την πλήρη κατανομή ακριβούς σκορ σε πιθανότητες
    Over/Under για κάθε μισή γραμμή συνολικών γκολ.
    """

    normalized_scores: list[tuple[int, float]] = []
    total_probability = 0.0

    for score in score_probabilities:
        home_goals = int(score["home_goals"])
        away_goals = int(score["away_goals"])
        probability = float(score["probability"])

        if probability < 0:
            raise ValueError("Η πιθανότητα σκορ δεν μπορεί να είναι αρνητική.")

        normalized_scores.append((home_goals + away_goals, probability))
        total_probability += probability

    if total_probability <= 0:
        raise ValueError("Δεν υπάρχει έγκυρη κατανομή σκορ.")

    market_lines: list[dict[str, float]] = []

    for raw_line in lines:
        line = _normalize_half_line(raw_line)
        over_probability = sum(
            probability
            for total_goals, probability in normalized_scores
            if total_goals > line
        ) / total_probability
        under_probability = 1.0 - over_probability

        market_lines.append(
            {
                "line": line,
                "over": round(over_probability, 8),
                "under": round(under_probability, 8),
                "over_percent": round(over_probability * 100.0, 2),
                "under_percent": round(under_probability * 100.0, 2),
            }
        )

    market_lines.sort(key=lambda item: float(item["line"]))
    return market_lines


def combine_total_market_lines(
    baseline_lines: Iterable[dict[str, Any]],
    mle_lines: Iterable[dict[str, Any]],
    baseline_weight: float,
    mle_weight: float,
) -> list[dict[str, float]]:
    """Συνδυάζει τις ίδιες γραμμές των δύο μοντέλων του ensemble."""

    baseline_by_line = {
        float(item["line"]): item
        for item in baseline_lines
    }
    mle_by_line = {
        float(item["line"]): item
        for item in mle_lines
    }

    common_lines = sorted(set(baseline_by_line) & set(mle_by_line))
    if not common_lines:
        raise ValueError("Τα δύο μοντέλα δεν έχουν κοινές γραμμές γκολ.")

    combined: list[dict[str, float]] = []

    for line in common_lines:
        baseline_item = baseline_by_line[line]
        mle_item = mle_by_line[line]

        over_probability = (
            float(baseline_weight) * float(baseline_item["over"])
            + float(mle_weight) * float(mle_item["over"])
        )
        under_probability = (
            float(baseline_weight) * float(baseline_item["under"])
            + float(mle_weight) * float(mle_item["under"])
        )

        pair_total = over_probability + under_probability
        if pair_total <= 0:
            raise ValueError("Μη έγκυρο ζεύγος πιθανοτήτων Over/Under.")

        over_probability /= pair_total
        under_probability /= pair_total

        combined.append(
            {
                "line": line,
                "over": round(over_probability, 8),
                "under": round(under_probability, 8),
                "over_percent": round(over_probability * 100.0, 2),
                "under_percent": round(under_probability * 100.0, 2),
            }
        )

    return combined


def select_strongest_relevant_market(
    market_lines: Iterable[dict[str, Any]],
    expected_total: float,
    relevance_window: float = DEFAULT_RELEVANCE_WINDOW,
) -> dict[str, Any]:
    """
    Επιλέγει την πιθανότερη πλευρά ανάμεσα στις γραμμές που βρίσκονται
    κοντά στην αναμενόμενη συνολική τιμή.

    Έτσι αποφεύγονται άχρηστες επιλογές σε υπερβολικά μακρινές γραμμές,
    όπως Under 5.5 γκολ με σχεδόν 100%, ενώ εξακολουθεί να επιλέγεται η
    μεγαλύτερη πιθανότητα μέσα στις πραγματικά σχετικές γραμμές.
    """

    normalized_lines = [
        {
            "line": float(item["line"]),
            "over": float(item["over"]),
            "under": float(item["under"]),
        }
        for item in market_lines
    ]

    if not normalized_lines:
        raise ValueError("Δεν δόθηκαν γραμμές αγοράς.")

    if relevance_window < 0:
        raise ValueError("Το relevance_window δεν μπορεί να είναι αρνητικό.")

    expected_value = float(expected_total)
    relevant = [
        item
        for item in normalized_lines
        if abs(item["line"] - expected_value) <= relevance_window
    ]

    if not relevant:
        nearest_distance = min(
            abs(item["line"] - expected_value)
            for item in normalized_lines
        )
        relevant = [
            item
            for item in normalized_lines
            if abs(abs(item["line"] - expected_value) - nearest_distance) < 1e-9
        ]

    candidates: list[dict[str, Any]] = []
    for item in relevant:
        if item["over"] >= item["under"]:
            side = "OVER"
            probability = item["over"]
        else:
            side = "UNDER"
            probability = item["under"]

        candidates.append(
            {
                "side": side,
                "line": item["line"],
                "probability": probability,
                "distance_from_expected": abs(item["line"] - expected_value),
            }
        )

    selected = max(
        candidates,
        key=lambda item: (
            float(item["probability"]),
            -float(item["distance_from_expected"]),
            -float(item["line"]),
        ),
    )

    side = str(selected["side"])
    line = float(selected["line"])
    probability = float(selected["probability"])

    return {
        "side": side,
        "line": line,
        "label": f"{side.title()} {line:.1f}",
        "probability": round(probability, 8),
        "probability_percent": round(probability * 100.0, 2),
        "expected_total": round(expected_value, 3),
        "candidate_lines": [
            float(item["line"])
            for item in sorted(relevant, key=lambda candidate: candidate["line"])
        ],
        "selection_rule": (
            "Η πιθανότερη πλευρά ανάμεσα στις γραμμές που απέχουν έως "
            f"{relevance_window:.1f} από τα αναμενόμενα συνολικά γκολ."
        ),
    }
