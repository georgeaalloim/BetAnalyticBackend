from __future__ import annotations

import os
import sys
from pathlib import Path

from recent_result_sync import sync_recent_results
from time_utils import parse_iso_datetime, utc_now


def _as_of_from_argv():
    for index, value in enumerate(sys.argv[1:]):
        if value == "--as-of":
            absolute = index + 1
            if absolute + 1 < len(sys.argv):
                return parse_iso_datetime(sys.argv[absolute + 1])
        if value.startswith("--as-of="):
            return parse_iso_datetime(value.split("=", 1)[1])
    return utc_now()


def main() -> int:
    # First pull only the very recent final scores. This makes a just-finished
    # match eligible for History before slower CSV providers publish their file.
    api_football_key = os.getenv("API_FOOTBALL_KEY")
    result = sync_recent_results(
        as_of=_as_of_from_argv(),
        recent_hours=18.0,
        thesportsdb_key=os.getenv("THESPORTSDB_KEY"),
        api_football_key=api_football_key,
    )

    summary_path = Path("data/recent_result_sync_summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    from dataclasses import asdict
    summary_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # The free API-Football plan blocks full 2026 season queries. Keep its key
    # for the narrow date fallback above, but do not waste quota on blocked
    # season requests inside the normal full sync. The next workflow step gets
    # its own original environment again.
    previous_api_key = os.environ.pop("API_FOOTBALL_KEY", None)
    try:
        from automatic_update import main as automatic_update_main
        return int(automatic_update_main())
    finally:
        if previous_api_key is not None:
            os.environ["API_FOOTBALL_KEY"] = previous_api_key


if __name__ == "__main__":
    raise SystemExit(main())
