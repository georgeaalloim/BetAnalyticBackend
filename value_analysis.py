from __future__ import annotations

import json
import math
from typing import Any


RESULT_LABELS = (
    "HOME",
    "DRAW",
    "AWAY",
)

DEFAULT_MIN_EDGE_PERCENT = 3.0
DEFAULT_MIN_EXPECTED_VALUE_PERCENT = 3.0
DEFAULT_KELLY_MULTIPLIER = 0.25
DEFAULT_MAX_BANKROLL_FRACTION = 0.02


def ensure_finite_number(
    value: Any,
    field_name: str,
) -> float:
    """
    Μετατρέπει μία τιμή σε float και ελέγχει
    ότι είναι πραγματικός, πεπερασμένος αριθμός.
    """

    try:
        numeric_value = float(value)

    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Το πεδίο '{field_name}' πρέπει "
            "να είναι αριθμός."
        ) from error

    if not math.isfinite(numeric_value):
        raise ValueError(
            f"Το πεδίο '{field_name}' πρέπει "
            "να είναι πεπερασμένος αριθμός."
        )

    return numeric_value


def validate_result_keys(
    values: dict[str, Any],
    field_name: str,
) -> None:
    """
    Ελέγχει ότι ένα λεξικό περιέχει ακριβώς
    τις εκβάσεις HOME, DRAW και AWAY.
    """

    if not isinstance(values, dict):
        raise ValueError(
            f"Το πεδίο '{field_name}' πρέπει "
            "να είναι λεξικό."
        )

    missing_labels = [
        label
        for label in RESULT_LABELS
        if label not in values
    ]

    if missing_labels:
        missing_text = ", ".join(
            missing_labels
        )

        raise ValueError(
            f"Λείπουν από το '{field_name}' "
            f"οι εκβάσεις: {missing_text}."
        )


def normalize_model_probabilities(
    probabilities: dict[str, Any],
) -> dict[str, float]:
    """
    Κανονικοποιεί πιθανότητες HOME/DRAW/AWAY.

    Δέχεται είτε δεκαδικές πιθανότητες:
        0.45, 0.28, 0.27

    είτε ποσοστά:
        45, 28, 27

    Η τελική έξοδος είναι πάντα σε δεκαδική μορφή
    και αθροίζει ακριβώς σε 1.
    """

    validate_result_keys(
        values=probabilities,
        field_name="model_probabilities",
    )

    numeric_probabilities = {
        label: ensure_finite_number(
            probabilities[label],
            f"model_probabilities.{label}",
        )
        for label in RESULT_LABELS
    }

    if any(
        value < 0
        for value in numeric_probabilities.values()
    ):
        raise ValueError(
            "Οι πιθανότητες του μοντέλου "
            "δεν μπορούν να είναι αρνητικές."
        )

    probability_sum = sum(
        numeric_probabilities.values()
    )

    if probability_sum <= 0:
        raise ValueError(
            "Το άθροισμα των πιθανοτήτων "
            "πρέπει να είναι μεγαλύτερο από μηδέν."
        )

    maximum_probability = max(
        numeric_probabilities.values()
    )

    if maximum_probability > 1.0:
        numeric_probabilities = {
            label: value / 100.0
            for label, value
            in numeric_probabilities.items()
        }

        probability_sum = sum(
            numeric_probabilities.values()
        )

    if any(
        value > 1.0
        for value in numeric_probabilities.values()
    ):
        raise ValueError(
            "Οι πιθανότητες πρέπει να δίνονται "
            "είτε ως δεκαδικοί αριθμοί 0-1 "
            "είτε ως ποσοστά 0-100."
        )

    return {
        label: (
            numeric_probabilities[label]
            / probability_sum
        )
        for label in RESULT_LABELS
    }


def validate_decimal_odds(
    decimal_odds: dict[str, Any],
) -> dict[str, float]:
    """
    Ελέγχει τις δεκαδικές αποδόσεις 1-X-2.
    Κάθε απόδοση πρέπει να είναι μεγαλύτερη από 1.
    """

    validate_result_keys(
        values=decimal_odds,
        field_name="decimal_odds",
    )

    validated_odds = {
        label: ensure_finite_number(
            decimal_odds[label],
            f"decimal_odds.{label}",
        )
        for label in RESULT_LABELS
    }

    invalid_labels = [
        label
        for label, odds
        in validated_odds.items()
        if odds <= 1.0
    ]

    if invalid_labels:
        invalid_text = ", ".join(
            invalid_labels
        )

        raise ValueError(
            "Οι δεκαδικές αποδόσεις πρέπει "
            "να είναι μεγαλύτερες από 1. "
            f"Μη έγκυρες εκβάσεις: {invalid_text}."
        )

    return validated_odds


def calculate_market_probabilities(
    decimal_odds: dict[str, Any],
) -> dict[str, Any]:
    """
    Μετατρέπει τις αποδόσεις σε:

    1. Ακατέργαστες implied probabilities.
    2. Περιθώριο bookmaker (overround).
    3. Fair πιθανότητες αγοράς μετά την αφαίρεση
       του περιθωρίου με αναλογική κανονικοποίηση.
    """

    validated_odds = validate_decimal_odds(
        decimal_odds=decimal_odds,
    )

    raw_implied_probabilities = {
        label: 1.0 / validated_odds[label]
        for label in RESULT_LABELS
    }

    raw_probability_sum = sum(
        raw_implied_probabilities.values()
    )

    overround = raw_probability_sum - 1.0

    fair_market_probabilities = {
        label: (
            raw_implied_probabilities[label]
            / raw_probability_sum
        )
        for label in RESULT_LABELS
    }

    return {
        "decimal_odds": validated_odds,
        "raw_implied_probabilities": (
            raw_implied_probabilities
        ),
        "raw_probability_sum": (
            raw_probability_sum
        ),
        "overround": overround,
        "fair_market_probabilities": (
            fair_market_probabilities
        ),
    }


def calculate_expected_value(
    model_probability: float,
    decimal_odds: float,
) -> float:
    """
    Αναμενόμενη απόδοση ανά 1 μονάδα πονταρίσματος.

    EV = πιθανότητα μοντέλου × απόδοση - 1

    Παράδειγμα:
        EV = 0.08 σημαίνει θεωρητική αναμενόμενη
        απόδοση +8% ανά μονάδα πονταρίσματος.
    """

    return (
        model_probability
        * decimal_odds
        - 1.0
    )


def calculate_kelly_fraction(
    model_probability: float,
    decimal_odds: float,
) -> float:
    """
    Υπολογίζει το πλήρες κλάσμα Kelly:

        f = (odds × p - 1) / (odds - 1)

    Αρνητικό αποτέλεσμα μετατρέπεται σε 0,
    επειδή δεν υπάρχει θετικό θεωρητικό πλεονέκτημα.
    """

    numerator = (
        decimal_odds
        * model_probability
        - 1.0
    )

    denominator = decimal_odds - 1.0

    if numerator <= 0:
        return 0.0

    return numerator / denominator


def round_probability_percent(
    probability: float,
) -> float:
    """
    Μετατρέπει δεκαδική πιθανότητα σε ποσοστό.
    """

    return round(
        probability * 100.0,
        2,
    )


def analyze_1x2_value(
    model_probabilities: dict[str, Any],
    decimal_odds: dict[str, Any],
    min_edge_percent: float = (
        DEFAULT_MIN_EDGE_PERCENT
    ),
    min_expected_value_percent: float = (
        DEFAULT_MIN_EXPECTED_VALUE_PERCENT
    ),
    kelly_multiplier: float = (
        DEFAULT_KELLY_MULTIPLIER
    ),
    max_bankroll_fraction: float = (
        DEFAULT_MAX_BANKROLL_FRACTION
    ),
) -> dict[str, Any]:
    """
    Αναλύει την πιθανή αξία στις αγορές 1-X-2.

    Η συνάρτηση:

    - αφαιρεί το περιθώριο του bookmaker,
    - συγκρίνει πιθανότητες μοντέλου και αγοράς,
    - υπολογίζει edge και Expected Value,
    - υπολογίζει πλήρες και κλασματικό Kelly,
    - επισημαίνει μόνο τις εκβάσεις που περνούν
      και τα δύο κατώφλια.

    Δεν εγγυάται κέρδος. Το θετικό EV είναι
    μαθηματική εκτίμηση που εξαρτάται από την
    ακρίβεια των πιθανοτήτων του μοντέλου.
    """

    normalized_model_probabilities = (
        normalize_model_probabilities(
            probabilities=model_probabilities,
        )
    )

    market_analysis = (
        calculate_market_probabilities(
            decimal_odds=decimal_odds,
        )
    )

    minimum_edge = (
        ensure_finite_number(
            min_edge_percent,
            "min_edge_percent",
        )
        / 100.0
    )

    minimum_expected_value = (
        ensure_finite_number(
            min_expected_value_percent,
            "min_expected_value_percent",
        )
        / 100.0
    )

    validated_kelly_multiplier = (
        ensure_finite_number(
            kelly_multiplier,
            "kelly_multiplier",
        )
    )

    validated_max_bankroll_fraction = (
        ensure_finite_number(
            max_bankroll_fraction,
            "max_bankroll_fraction",
        )
    )

    if minimum_edge < 0:
        raise ValueError(
            "Το min_edge_percent δεν μπορεί "
            "να είναι αρνητικό."
        )

    if minimum_expected_value < 0:
        raise ValueError(
            "Το min_expected_value_percent "
            "δεν μπορεί να είναι αρνητικό."
        )

    if not (
        0.0
        <= validated_kelly_multiplier
        <= 1.0
    ):
        raise ValueError(
            "Το kelly_multiplier πρέπει "
            "να βρίσκεται από 0 έως 1."
        )

    if not (
        0.0
        <= validated_max_bankroll_fraction
        <= 1.0
    ):
        raise ValueError(
            "Το max_bankroll_fraction πρέπει "
            "να βρίσκεται από 0 έως 1."
        )

    fair_market_probabilities = (
        market_analysis[
            "fair_market_probabilities"
        ]
    )

    validated_odds = market_analysis[
        "decimal_odds"
    ]

    outcome_analysis: dict[
        str,
        dict[str, Any],
    ] = {}

    for label in RESULT_LABELS:
        model_probability = (
            normalized_model_probabilities[label]
        )

        market_probability = (
            fair_market_probabilities[label]
        )

        odds = validated_odds[label]

        edge = (
            model_probability
            - market_probability
        )

        expected_value = (
            calculate_expected_value(
                model_probability=(
                    model_probability
                ),
                decimal_odds=odds,
            )
        )

        full_kelly_fraction = (
            calculate_kelly_fraction(
                model_probability=(
                    model_probability
                ),
                decimal_odds=odds,
            )
        )

        fractional_kelly = (
            full_kelly_fraction
            * validated_kelly_multiplier
        )

        capped_fractional_kelly = min(
            fractional_kelly,
            validated_max_bankroll_fraction,
        )

        qualifies_as_value = (
            edge >= minimum_edge
            and expected_value
            >= minimum_expected_value
        )

        model_fair_odds = (
            1.0 / model_probability
            if model_probability > 0
            else None
        )

        outcome_analysis[label] = {
            "decimal_odds": round(
                odds,
                4,
            ),
            "model_probability": round(
                model_probability,
                8,
            ),
            "model_probability_percent": (
                round_probability_percent(
                    model_probability
                )
            ),
            "market_raw_implied_probability_percent": (
                round_probability_percent(
                    market_analysis[
                        "raw_implied_probabilities"
                    ][label]
                )
            ),
            "market_fair_probability_percent": (
                round_probability_percent(
                    market_probability
                )
            ),
            "model_fair_odds": (
                round(
                    model_fair_odds,
                    4,
                )
                if model_fair_odds is not None
                else None
            ),
            "edge_probability_points": round(
                edge * 100.0,
                2,
            ),
            "expected_value_per_unit": round(
                expected_value,
                6,
            ),
            "expected_value_percent": round(
                expected_value * 100.0,
                2,
            ),
            "full_kelly_fraction": round(
                full_kelly_fraction,
                6,
            ),
            "full_kelly_percent": round(
                full_kelly_fraction * 100.0,
                2,
            ),
            "fractional_kelly_percent": round(
                fractional_kelly * 100.0,
                2,
            ),
            "capped_bankroll_fraction": round(
                capped_fractional_kelly,
                6,
            ),
            "capped_bankroll_percent": round(
                capped_fractional_kelly
                * 100.0,
                2,
            ),
            "qualifies_as_value": (
                qualifies_as_value
            ),
        }

    ranking_by_expected_value = sorted(
        RESULT_LABELS,
        key=lambda label: (
            outcome_analysis[label][
                "expected_value_per_unit"
            ]
        ),
        reverse=True,
    )

    qualifying_outcomes = [
        label
        for label in ranking_by_expected_value
        if outcome_analysis[label][
            "qualifies_as_value"
        ]
    ]

    best_value_outcome = (
        qualifying_outcomes[0]
        if qualifying_outcomes
        else None
    )

    return {
        "market": {
            "decimal_odds": validated_odds,
            "raw_probability_sum_percent": round(
                market_analysis[
                    "raw_probability_sum"
                ]
                * 100.0,
                2,
            ),
            "overround_percent": round(
                market_analysis[
                    "overround"
                ]
                * 100.0,
                2,
            ),
            "fair_probabilities_percent": {
                label: (
                    round_probability_percent(
                        fair_market_probabilities[
                            label
                        ]
                    )
                )
                for label in RESULT_LABELS
            },
        },
        "model_probabilities_percent": {
            label: (
                round_probability_percent(
                    normalized_model_probabilities[
                        label
                    ]
                )
            )
            for label in RESULT_LABELS
        },
        "thresholds": {
            "minimum_edge_probability_points": (
                round(
                    minimum_edge * 100.0,
                    2,
                )
            ),
            "minimum_expected_value_percent": (
                round(
                    minimum_expected_value
                    * 100.0,
                    2,
                )
            ),
            "kelly_multiplier": (
                validated_kelly_multiplier
            ),
            "maximum_bankroll_percent": round(
                validated_max_bankroll_fraction
                * 100.0,
                2,
            ),
        },
        "outcomes": outcome_analysis,
        "ranking_by_expected_value": (
            ranking_by_expected_value
        ),
        "qualifying_value_outcomes": (
            qualifying_outcomes
        ),
        "best_value_outcome": (
            best_value_outcome
        ),
        "has_qualifying_value": (
            best_value_outcome is not None
        ),
        "warning": (
            "Το θετικό Expected Value δεν αποτελεί "
            "εγγύηση κέρδους. Εξαρτάται από την "
            "ακρίβεια του μοντέλου και εμφανίζει "
            "μεγάλη βραχυχρόνια διακύμανση."
        ),
    }


def main() -> None:
    """
    Μικρό παράδειγμα εκτέλεσης του αρχείου.
    """

    example_model_probabilities = {
        "HOME": 48.0,
        "DRAW": 27.0,
        "AWAY": 25.0,
    }

    example_decimal_odds = {
        "HOME": 2.25,
        "DRAW": 3.30,
        "AWAY": 3.40,
    }

    result = analyze_1x2_value(
        model_probabilities=(
            example_model_probabilities
        ),
        decimal_odds=example_decimal_odds,
    )

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
