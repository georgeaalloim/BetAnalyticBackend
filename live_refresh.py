from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from live_match_service import build_live_payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate BetAnalytic zero-cost near-live JSON.")
    parser.add_argument("--feed-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--previous-live-file", type=Path)
    return parser.parse_args()


def _read_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    args = _parse_args()
    feed = _read_json(args.feed_file)
    if not feed:
        raise SystemExit(f"Invalid or missing feed: {args.feed_file}")
    previous = _read_json(args.previous_live_file)
    payload = build_live_payload(
        feed,
        previous_live=previous,
        api_key=os.getenv("THESPORTSDB_KEY"),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "live.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({
        "status": "ok",
        "live_matches": payload["matches_count"],
        "requests_used": payload["requests_used"],
        "live_path": str(output),
        "warnings": payload["warnings"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
