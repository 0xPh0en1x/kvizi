from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import load_settings  # noqa: E402
from kvizi.web import create_app  # noqa: E402


CRON_ENDPOINTS = {
    "tick": "/cron/tick",
    "maintenance": "/cron/maintenance",
    "daily": "/cron/daily",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a Kvizi cron endpoint locally through Flask test_client.",
    )
    parser.add_argument(
        "job",
        choices=sorted(CRON_ENDPOINTS),
        help="Cron endpoint to run.",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help="Override X-Kvizi-Cron-Secret. Defaults to KVIZI_CRON_SECRET.",
    )
    args = parser.parse_args()

    settings = load_settings()
    secret = args.secret if args.secret is not None else settings.cron_secret
    status_code, payload = run_local_cron(args.job, secret)

    print(f"POST {CRON_ENDPOINTS[args.job]} -> {status_code}")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    if status_code < 200 or status_code >= 300:
        raise SystemExit(1)


def run_local_cron(job: str, secret: str) -> tuple[int, dict[str, Any]]:
    endpoint = CRON_ENDPOINTS[job]
    app = create_app()
    response = app.test_client().post(
        endpoint,
        headers={"X-Kvizi-Cron-Secret": secret},
    )
    return response.status_code, _response_payload(response)


def _response_payload(response: Any) -> dict[str, Any]:
    payload = response.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return {"body": response.get_data(as_text=True)}


if __name__ == "__main__":
    main()
