from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import load_settings  # noqa: E402
from kvizi.export_state import export_state  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Kvizi SQLite state to JSON.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path. Defaults to exports/kvizi-state-<timestamp>.json",
    )
    parser.add_argument(
        "--include-processed-updates",
        action="store_true",
        help="Include processed Telegram update ids. Usually not needed.",
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.database_path.exists():
        raise SystemExit(f"SQLite database not found: {settings.database_path}")

    output_path = args.output or _default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    state = export_state(settings.database_path, include_processed_updates=args.include_processed_updates)
    output_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"Exported Kvizi state: {output_path}")
    print(f"Topics: {len(state['topics'])}")
    print(f"Users: {len(state['users'])}")
    print(f"Scores: {len(state['scores'])}")
    print(f"Active polls: {len(state['active_polls'])}")


def _default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return PROJECT_ROOT / "exports" / f"kvizi-state-{timestamp}.json"


if __name__ == "__main__":
    main()
