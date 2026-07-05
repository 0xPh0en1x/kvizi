from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.split(","):
        value = item.strip()
        if value:
            result.add(int(value))
    return result


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    webhook_secret: str
    cron_secret: str
    admin_ids: set[int]
    timezone_name: str
    open_seconds: int
    database_path: Path
    questions_path: Path
    season_name: str
    announce_thread_id: int | None
    chat_username: str

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        webhook_secret=os.getenv("KVIZI_WEBHOOK_SECRET", "dev-webhook-secret"),
        cron_secret=os.getenv("KVIZI_CRON_SECRET", "dev-cron-secret"),
        admin_ids=_parse_admin_ids(os.getenv("KVIZI_ADMIN_IDS", "")),
        timezone_name=os.getenv("KVIZI_TZ", "Europe/Moscow"),
        open_seconds=int(os.getenv("KVIZI_OPEN_SECONDS", "7200")),
        database_path=Path(os.getenv("KVIZI_DB_PATH", PROJECT_ROOT / "data" / "kvizi.sqlite3")),
        questions_path=Path(os.getenv("KVIZI_QUESTIONS_PATH", PROJECT_ROOT / "questions.csv")),
        season_name=os.getenv("KVIZI_SEASON", "main"),
        announce_thread_id=_parse_optional_int(os.getenv("KVIZI_ANNOUNCE_THREAD_ID", "")),
        chat_username=os.getenv("KVIZI_CHAT_USERNAME", "").strip().lstrip("@"),
    )


def _parse_optional_int(raw: str) -> int | None:
    value = raw.strip()
    return int(value) if value else None
