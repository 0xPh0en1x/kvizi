from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from kvizi.scoring import parse_challenge_economy, parse_difficulty_points


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.split(","):
        value = item.strip()
        if value:
            result.add(int(value))
    return result


def _parse_bool(raw: str | None, default: bool = True) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {raw!r}")


def _parse_positive_int(raw: str | None, default: int, name: str) -> int:
    value = int(raw) if raw and raw.strip() else default
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _parse_positive_float(raw: str | None, default: float, name: str) -> float:
    value = float(raw) if raw and raw.strip() else default
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


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
    announce_first_answer: bool
    announce_no_answers: bool
    announce_risk_failures: bool
    announce_streaks: bool
    ai_enabled: bool
    ai_copy_enabled: bool
    groq_api_key: str
    ai_copy_model: str
    ai_timeout_seconds: float
    ai_retry_delay_seconds: int
    ai_max_attempts: int
    ai_job_ttl_seconds: int
    difficulty_points: dict[str, int]
    challenge_economy: dict[str, dict[str, int]]

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        webhook_secret=os.getenv("KVIZI_WEBHOOK_SECRET", "").strip(),
        cron_secret=os.getenv("KVIZI_CRON_SECRET", "").strip(),
        admin_ids=_parse_admin_ids(os.getenv("KVIZI_ADMIN_IDS", "")),
        timezone_name=os.getenv("KVIZI_TZ", "Europe/Moscow"),
        open_seconds=int(os.getenv("KVIZI_OPEN_SECONDS", "7200")),
        database_path=Path(os.getenv("KVIZI_DB_PATH", PROJECT_ROOT / "data" / "kvizi.sqlite3")),
        questions_path=Path(os.getenv("KVIZI_QUESTIONS_PATH", PROJECT_ROOT / "questions.csv")),
        season_name=os.getenv("KVIZI_SEASON", "main"),
        announce_thread_id=_parse_optional_int(os.getenv("KVIZI_ANNOUNCE_THREAD_ID", "")),
        chat_username=os.getenv("KVIZI_CHAT_USERNAME", "").strip().lstrip("@"),
        announce_first_answer=_parse_bool(os.getenv("KVIZI_ANNOUNCE_FIRST_ANSWER"), True),
        announce_no_answers=_parse_bool(os.getenv("KVIZI_ANNOUNCE_NO_ANSWERS"), True),
        announce_risk_failures=_parse_bool(os.getenv("KVIZI_ANNOUNCE_RISK_FAILURES"), True),
        announce_streaks=_parse_bool(os.getenv("KVIZI_ANNOUNCE_STREAKS"), True),
        ai_enabled=_parse_bool(os.getenv("KVIZI_AI_ENABLED"), False),
        ai_copy_enabled=_parse_bool(os.getenv("KVIZI_AI_COPY_ENABLED"), False),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        ai_copy_model=os.getenv("KVIZI_AI_COPY_MODEL", "qwen/qwen3.6-27b").strip(),
        ai_timeout_seconds=_parse_positive_float(
            os.getenv("KVIZI_AI_TIMEOUT_SECONDS"),
            7.0,
            "KVIZI_AI_TIMEOUT_SECONDS",
        ),
        ai_retry_delay_seconds=_parse_positive_int(
            os.getenv("KVIZI_AI_RETRY_DELAY_SECONDS"),
            300,
            "KVIZI_AI_RETRY_DELAY_SECONDS",
        ),
        ai_max_attempts=_parse_positive_int(
            os.getenv("KVIZI_AI_MAX_ATTEMPTS"),
            3,
            "KVIZI_AI_MAX_ATTEMPTS",
        ),
        ai_job_ttl_seconds=_parse_positive_int(
            os.getenv("KVIZI_AI_JOB_TTL_SECONDS"),
            1800,
            "KVIZI_AI_JOB_TTL_SECONDS",
        ),
        difficulty_points=parse_difficulty_points(os.getenv("KVIZI_DIFFICULTY_POINTS")),
        challenge_economy=parse_challenge_economy(os.getenv("KVIZI_CHALLENGE_REWARDS")),
    )


def _parse_optional_int(raw: str) -> int | None:
    value = raw.strip()
    return int(value) if value else None
