from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import load_settings  # noqa: E402
from kvizi.telegram import TelegramClient  # noqa: E402


ALLOWED_UPDATES = ["message", "callback_query", "poll", "poll_answer", "my_chat_member"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Telegram webhook for Kvizi.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("KVIZI_PUBLIC_BASE_URL", ""),
        help="Public HTTPS base URL, for example https://username.pythonanywhere.com",
    )
    parser.add_argument("--drop-pending", action="store_true", help="Drop pending Telegram updates")
    args = parser.parse_args()

    if not args.base_url:
        raise SystemExit("--base-url or KVIZI_PUBLIC_BASE_URL is required")

    settings = load_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not settings.webhook_secret:
        raise SystemExit("KVIZI_WEBHOOK_SECRET is required")

    url = args.base_url.rstrip("/") + f"/telegram/{settings.webhook_secret}"
    client = TelegramClient(settings.telegram_bot_token)
    result = client.set_webhook(
        url=url,
        secret_token=settings.webhook_secret,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=args.drop_pending,
    )
    print(result)


if __name__ == "__main__":
    main()
