from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv


load_dotenv()

BASE_URL = os.getenv(
    "API_FOOTBALL_BASE_URL",
    "https://v3.football.api-sports.io",
).rstrip("/")

API_KEY = os.getenv("API_FOOTBALL_KEY")

API_TIMEOUT_SECONDS = float(
    os.getenv(
        "API_FOOTBALL_TIMEOUT_SECONDS",
        "20",
    )
)


def _normalize_endpoint(
    endpoint: str,
) -> str:
    """
    Επιστρέφει endpoint που ξεκινά πάντα με '/'.
    """

    cleaned_endpoint = endpoint.strip()

    if not cleaned_endpoint:
        raise ValueError(
            "Το endpoint του API δεν μπορεί να είναι κενό."
        )

    if not cleaned_endpoint.startswith("/"):
        cleaned_endpoint = f"/{cleaned_endpoint}"

    return cleaned_endpoint


def _request_json(
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Εκτελεί ένα GET request και επιστρέφει έγκυρο JSON.
    """

    if not API_KEY:
        raise RuntimeError(
            "Δεν βρέθηκε το API_FOOTBALL_KEY στο αρχείο .env."
        )

    normalized_endpoint = _normalize_endpoint(
        endpoint=endpoint,
    )

    headers = {
        "x-apisports-key": API_KEY,
    }

    response = requests.get(
        url=f"{BASE_URL}{normalized_endpoint}",
        headers=headers,
        params=params,
        timeout=API_TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    try:
        data = response.json()

    except requests.JSONDecodeError as error:
        raise RuntimeError(
            "Το API-Football επέστρεψε μη έγκυρο JSON."
        ) from error

    if not isinstance(data, dict):
        raise RuntimeError(
            "Το API-Football επέστρεψε μη αναμενόμενη μορφή δεδομένων."
        )

    if data.get("errors"):
        raise RuntimeError(
            f"Σφάλμα API-Football: {data['errors']}"
        )

    return data


def api_get(
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Στέλνει ένα GET αίτημα στο API-Football.
    """

    return _request_json(
        endpoint=endpoint,
        params=params,
    )


def api_get_all_pages(
    endpoint: str,
    params: dict[str, Any] | None = None,
    max_pages: int = 100,
) -> dict[str, Any]:
    """
    Διαβάζει όλες τις σελίδες ενός paginated endpoint.

    Επιστρέφει ενιαία λίστα response και το πλήθος
    των HTTP requests που πραγματοποιήθηκαν.
    """

    if max_pages < 1:
        raise ValueError(
            "Το max_pages πρέπει να είναι τουλάχιστον 1."
        )

    base_params = dict(params or {})
    requested_start_page = int(
        base_params.pop("page", 1)
    )

    if requested_start_page < 1:
        raise ValueError(
            "Η αρχική σελίδα πρέπει να είναι τουλάχιστον 1."
        )

    combined_response: list[Any] = []
    first_data: dict[str, Any] | None = None
    current_page = requested_start_page
    pages_fetched = 0
    total_pages = requested_start_page

    while True:
        request_params = {
            **base_params,
            "page": current_page,
        }

        data = _request_json(
            endpoint=endpoint,
            params=request_params,
        )

        if first_data is None:
            first_data = data

        page_response = data.get(
            "response",
            [],
        )

        if not isinstance(page_response, list):
            raise RuntimeError(
                "Το πεδίο response του API δεν είναι λίστα."
            )

        combined_response.extend(
            page_response
        )

        pages_fetched += 1

        paging = data.get(
            "paging",
            {},
        )

        api_current_page = int(
            paging.get(
                "current",
                current_page,
            )
            or current_page
        )

        total_pages = int(
            paging.get(
                "total",
                api_current_page,
            )
            or api_current_page
        )

        if api_current_page >= total_pages:
            break

        if pages_fetched >= max_pages:
            raise RuntimeError(
                "Η ανάκτηση σταμάτησε επειδή ξεπεράστηκε "
                f"το max_pages={max_pages}."
            )

        current_page = api_current_page + 1

    merged_data = dict(first_data or {})
    merged_data["response"] = combined_response
    merged_data["results"] = len(combined_response)
    merged_data["paging"] = {
        "current": total_pages,
        "total": total_pages,
    }
    merged_data["pages_fetched"] = pages_fetched

    return merged_data