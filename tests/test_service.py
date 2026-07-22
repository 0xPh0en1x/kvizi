from __future__ import annotations

import csv
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from kvizi import copy
from kvizi.ai import AIProviderError, AIResult
from kvizi.config import Settings
from kvizi.database import KviziRepository, utc_now_iso
from kvizi.questions import Question, QuestionBank
from kvizi.service import KviziService
from kvizi.telegram import TelegramApiError
from kvizi.web import create_app


class FakeTelegram:
    def __init__(self) -> None:
        self.sent_polls: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.sent_documents: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.stopped_polls: list[dict[str, Any]] = []
        self.downloaded_files: dict[str, bytes] = {}

    def send_poll(self, **payload: Any) -> dict[str, Any]:
        self.sent_polls.append(payload)
        poll_id = f"poll-{len(self.sent_polls)}"
        return {"ok": True, "result": {"message_id": len(self.sent_polls), "poll": {"id": poll_id}}}

    def send_message(self, **payload: Any) -> dict[str, Any]:
        self.sent_messages.append(payload)
        return {"ok": True, "result": {"message_id": 100 + len(self.sent_messages)}}

    def edit_message_text(self, **payload: Any) -> dict[str, Any]:
        self.edited_messages.append(payload)
        return {"ok": True, "result": {"message_id": payload["message_id"]}}

    def send_document(self, **payload: Any) -> dict[str, Any]:
        self.sent_documents.append(payload)
        return {"ok": True, "result": {"message_id": 200 + len(self.sent_documents)}}

    def answer_callback_query(self, **payload: Any) -> None:
        self.callback_answers.append(payload)

    def stop_poll(self, **payload: Any) -> dict[str, Any]:
        self.stopped_polls.append(payload)
        return {"ok": True, "result": {"id": "stopped"}}

    def download_file(self, file_id: str) -> bytes:
        return self.downloaded_files[file_id]


class FakeAIProvider:
    name = "fake"
    model = "fake-copy-model"

    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        timeout_seconds: float,
    ) -> AIResult:
        self.calls.append(
            {
                "messages": messages,
                "purpose": purpose,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.outcomes:
            raise AssertionError("FakeAIProvider has no configured outcome")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return AIResult(
            text=outcome,
            provider=self.name,
            model=self.model,
            latency_ms=1,
        )


def ai_teaser(teaser: str, anchor: str) -> str:
    return json.dumps({"teaser": teaser, "anchor": anchor}, ensure_ascii=False)


class BlockingAIProvider(FakeAIProvider):
    def __init__(self, outcome: str) -> None:
        super().__init__([outcome])
        self.entered = Event()
        self.release = Event()

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        timeout_seconds: float,
    ) -> AIResult:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release AI provider")
        return super().complete(
            messages,
            purpose=purpose,
            timeout_seconds=timeout_seconds,
        )


class FailOnceSendMessageTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def send_message(self, **payload: Any) -> dict[str, Any]:
        if not self.failed_once:
            self.failed_once = True
            raise TelegramApiError(
                "Telegram sendMessage request failed after 3 attempts: proxy 503",
                ambiguous=True,
            )
        return super().send_message(**payload)


class AlwaysFailSendMessageTelegram(FakeTelegram):
    def send_message(self, **payload: Any) -> dict[str, Any]:
        raise TelegramApiError("temporary proxy 503", ambiguous=True)


class FailOnceEditMessageTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.edit_attempts = 0

    def edit_message_text(self, **payload: Any) -> dict[str, Any]:
        self.edit_attempts += 1
        if self.edit_attempts == 1:
            raise TelegramApiError("temporary proxy 503", ambiguous=True)
        return super().edit_message_text(**payload)


class FailDocumentForChatTelegram(FakeTelegram):
    def __init__(self, failed_chat_id: str) -> None:
        super().__init__()
        self.failed_chat_id = failed_chat_id

    def send_document(self, **payload: Any) -> dict[str, Any]:
        if str(payload["chat_id"]) == self.failed_chat_id:
            raise TelegramApiError("bot can't initiate conversation")
        return super().send_document(**payload)


class BlockingSendPollTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def send_poll(self, **payload: Any) -> dict[str, Any]:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release send_poll")
        return super().send_poll(**payload)


class BlockingSendMessageTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def send_message(self, **payload: Any) -> dict[str, Any]:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release send_message")
        return super().send_message(**payload)


class AmbiguousStopPollTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.stop_attempts = 0

    def stop_poll(self, **payload: Any) -> dict[str, Any]:
        self.stop_attempts += 1
        raise TelegramApiError("temporary proxy 503", ambiguous=True)


class AlreadyClosedStopPollTelegram(FakeTelegram):
    def stop_poll(self, **payload: Any) -> dict[str, Any]:
        raise TelegramApiError("Bad Request: poll has already been closed")


class CannotStopPollTelegram(FakeTelegram):
    def stop_poll(self, **payload: Any) -> dict[str, Any]:
        raise TelegramApiError("Bad Request: poll can't be stopped")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="token",
        telegram_chat_id="-1001",
        webhook_secret="webhook-secret",
        cron_secret="cron-secret",
        admin_ids={7},
        timezone_name="Europe/Moscow",
        open_seconds=7200,
        database_path=tmp_path / "kvizi.sqlite3",
        questions_path=tmp_path / "questions.csv",
        season_name="main",
        announce_thread_id=None,
        chat_username="",
        announce_first_answer=True,
        announce_no_answers=True,
        announce_risk_failures=True,
        announce_streaks=True,
        ai_enabled=False,
        ai_copy_enabled=False,
        groq_api_key="",
        ai_copy_model="qwen/qwen3.6-27b",
        ai_timeout_seconds=7.0,
        ai_retry_delay_seconds=300,
        ai_max_attempts=3,
        ai_job_ttl_seconds=1800,
        difficulty_points={"easy": 5, "normal": 10, "hard": 15},
        challenge_economy={
            "easy": {"cost": 5, "reward": 10},
            "normal": {"cost": 10, "reward": 25},
            "hard": {"cost": 15, "reward": 40},
        },
    )


def make_question_bank() -> QuestionBank:
    return QuestionBank(
        [
            Question(
                id="q1",
                topic_key="network",
                difficulty="normal",
                text="What resolves names?",
                options=("DNS", "SMTP", "DHCP", "ARP"),
                correct_option_id=0,
                explanation="DNS resolves names.",
                source="",
            ),
            Question(
                id="q2",
                topic_key="network",
                difficulty="hard",
                text="Which record maps a name to IPv4?",
                options=("A", "AAAA", "MX", "TXT"),
                correct_option_id=0,
                explanation="A records point to IPv4 addresses.",
                source="",
            ),
        ]
    )


def make_service(tmp_path: Path) -> tuple[KviziService, KviziRepository, FakeTelegram]:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )
    return service, repository, telegram


def make_ai_service(
    tmp_path: Path,
    provider: FakeAIProvider,
    *,
    telegram: FakeTelegram | None = None,
) -> tuple[KviziService, KviziRepository, FakeTelegram]:
    settings = replace(
        make_settings(tmp_path),
        ai_enabled=True,
        ai_copy_enabled=True,
        ai_retry_delay_seconds=1,
        ai_job_ttl_seconds=60,
    )
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.set_bot_setting("announce_thread_id", "999")
    telegram = telegram or FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
        ai_provider=provider,
    )
    return service, repository, telegram


def _daily_title_matches(text: str) -> bool:
    first_line = text.splitlines()[0]
    return any(
        re.fullmatch(
            re.escape(template).replace(r"\{date\}", r"\d{2}\.\d{2}\.\d{4} MSK"),
            first_line,
        )
        for template in copy.DAILY_TITLE_TEMPLATES
    )


def test_database_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()

    with repository.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000


def test_database_migrates_existing_announcement_queue_for_ai(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE pending_announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                message_thread_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                event TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    repository = KviziRepository(database_path)
    repository.init_db()

    with repository.connect() as connection:
        pending_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(pending_announcements)")
        }
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"ai_purpose", "ai_context_json"} <= pending_columns
    assert "ai_enhancement_jobs" in tables


def test_post_bet_answer_updates_score_and_is_idempotent(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)

    posted = service.post_question(difficulty="normal")
    assert posted.posted is True
    assert telegram.sent_polls[0]["message_thread_id"] == 101

    bet_result = service.handle_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 42, "first_name": "Ada"},
                "data": "bet:3",
                "message": {
                    "chat": {"id": "-1001"},
                    "poll": {"id": "poll-1"},
                },
            },
        }
    )
    assert bet_result["bet"] is True

    answer_result = service.handle_update(
        {
            "update_id": 2,
            "poll_answer": {
                "poll_id": "poll-1",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )
    assert answer_result["recorded"] is True
    assert answer_result["delta"] == 30

    duplicate_result = service.handle_update(
        {
            "update_id": 3,
            "poll_answer": {
                "poll_id": "poll-1",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )
    assert duplicate_result["recorded"] is False

    score = repository.get_score("main", 42)
    assert score["points"] == 30
    assert score["correct_count"] == 1
    with repository.connect() as connection:
        answer = connection.execute("SELECT season FROM answers WHERE poll_id = ?", ("poll-1",)).fetchone()
    assert answer["season"] == "main"


def test_callback_query_from_foreign_chat_is_ignored(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)

    posted = service.post_question(difficulty="normal")
    assert posted.posted is True

    result = service.handle_update(
        {
            "update_id": 4,
            "callback_query": {
                "id": "cb-foreign",
                "from": {"id": 42, "first_name": "Ada"},
                "data": "bet:3",
                "message": {
                    "chat": {"id": "-9999"},
                    "poll": {"id": "poll-1"},
                },
            },
        }
    )

    assert result["ignored"] == "foreign_chat"
    assert telegram.callback_answers == []
    with repository.connect() as connection:
        bet_count = connection.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    assert bet_count == 0


def test_poll_answer_announces_new_season_leader_to_announce_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    _seed_today_answer(repository, user_id=900, username="seed", points_difficulty="easy")
    _seed_topic_answer(
        repository,
        poll_id="old-leader",
        topic_key="network",
        user_id=42,
        username="ada",
        difficulty="normal",
    )
    assert repository.leaderboard("main", limit=1)[0]["user_id"] == 42

    posted = service.post_question(difficulty="hard")
    assert posted.posted is True
    before_messages = len(telegram.sent_messages)

    answer_result = service.handle_update(
        {
            "update_id": 4,
            "poll_answer": {
                "poll_id": str(posted.poll_id),
                "user": {"id": 7, "first_name": "Neo"},
                "option_ids": [0],
            },
        }
    )

    assert answer_result["recorded"] is True
    assert answer_result["points"] == 15
    assert repository.leaderboard("main", limit=1)[0]["user_id"] == 7
    assert len(telegram.sent_messages) == before_messages + 1
    announcement = telegram.sent_messages[-1]
    assert announcement["message_thread_id"] == 999
    assert announcement["disable_notification"] is True
    assert "Neo" in announcement["text"]
    assert "@ada" in announcement["text"]
    assert "15" in announcement["text"]


def test_poll_answer_announces_streak_milestone_to_announce_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    _seed_today_answer(repository, user_id=900, username="seed", points_difficulty="easy")
    _seed_topic_answer(
        repository,
        poll_id="neo-first",
        topic_key="network",
        user_id=7,
        username="neo",
        difficulty="normal",
    )
    _seed_topic_answer(
        repository,
        poll_id="neo-second",
        topic_key="network",
        user_id=7,
        username="neo",
        difficulty="normal",
    )

    posted = service.post_question(difficulty="normal")
    assert posted.posted is True
    before_messages = len(telegram.sent_messages)

    answer_result = service.handle_update(
        {
            "update_id": 5,
            "poll_answer": {
                "poll_id": str(posted.poll_id),
                "user": {"id": 7, "first_name": "Neo"},
                "option_ids": [0],
            },
        }
    )

    assert answer_result["recorded"] is True
    assert answer_result["delta"] == 13
    assert len(telegram.sent_messages) == before_messages + 2
    score_message = telegram.sent_messages[-2]
    announcement = telegram.sent_messages[-1]
    assert score_message["message_thread_id"] == 101
    assert announcement["message_thread_id"] == 999
    assert announcement["disable_notification"] is True
    assert "Neo" in announcement["text"]
    assert "3" in announcement["text"]
    assert "+3" in announcement["text"]
    assert "33" in announcement["text"]


def test_poll_answer_announces_x2_and_x3_risk_failures_to_announce_topic(tmp_path: Path) -> None:
    for stake, expected_delta in ((2, -10), (3, -20)):
        case_dir = tmp_path / f"risk-{stake}"
        case_dir.mkdir()
        service, repository, telegram = make_service(case_dir)
        repository.set_bot_setting("announce_thread_id", "999")
        _seed_today_answer(repository, user_id=900 + stake, username=f"seed{stake}", points_difficulty="easy")

        posted = service.post_question(difficulty="normal")
        assert posted.posted is True

        bet_result = service.handle_update(
            {
                "update_id": 10 + stake,
                "callback_query": {
                    "id": f"cb-risk-{stake}",
                    "from": {"id": 7, "first_name": "Neo"},
                    "data": f"bet:{stake}",
                    "message": {
                        "chat": {"id": "-1001"},
                        "poll": {"id": str(posted.poll_id)},
                    },
                },
            }
        )
        assert bet_result["bet"] is True
        before_messages = len(telegram.sent_messages)

        answer_result = service.handle_update(
            {
                "update_id": 20 + stake,
                "poll_answer": {
                    "poll_id": str(posted.poll_id),
                    "user": {"id": 7, "first_name": "Neo"},
                    "option_ids": [1],
                },
            }
        )

        assert answer_result["recorded"] is True
        assert answer_result["delta"] == expected_delta
        assert len(telegram.sent_messages) == before_messages + 2
        score_message = telegram.sent_messages[-2]
        announcement = telegram.sent_messages[-1]
        assert score_message["message_thread_id"] == 101
        assert announcement["message_thread_id"] == 999
        assert announcement["disable_notification"] is True
        assert "Neo" in announcement["text"]
        assert f"x{stake}" in announcement["text"]
        assert str(expected_delta) in announcement["text"]
        assert str(answer_result["points"]) in announcement["text"]


def test_poll_answer_announces_first_answer_of_day_to_announce_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")

    posted = service.post_question(difficulty="normal")
    assert posted.posted is True
    before_messages = len(telegram.sent_messages)

    answer_result = service.handle_update(
        {
            "update_id": 31,
            "poll_answer": {
                "poll_id": str(posted.poll_id),
                "user": {"id": 7, "first_name": "Neo"},
                "option_ids": [0],
            },
        }
    )

    assert answer_result["recorded"] is True
    assert len(telegram.sent_messages) == before_messages + 1
    announcement = telegram.sent_messages[-1]
    assert announcement["message_thread_id"] == 999
    assert announcement["disable_notification"] is True
    assert "Neo" in announcement["text"]
    assert "network" in announcement["text"]
    assert "normal" in announcement["text"]
    assert "+10" in announcement["text"]
    assert "10" in announcement["text"]


def test_first_answer_announcement_can_be_disabled(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), announce_first_answer=False)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.set_bot_setting("announce_thread_id", "999")
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )

    posted = service.post_question(difficulty="normal")
    assert posted.posted is True
    before_messages = len(telegram.sent_messages)

    answer_result = service.handle_update(
        {
            "update_id": 32,
            "poll_answer": {
                "poll_id": str(posted.poll_id),
                "user": {"id": 7, "first_name": "Neo"},
                "option_ids": [0],
            },
        }
    )

    assert answer_result["recorded"] is True
    assert len(telegram.sent_messages) == before_messages


def test_close_expired_poll_announces_no_answers_to_announce_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    repository.create_poll(
        poll_id="expired-empty",
        telegram_message_id=321,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )

    closed = service.close_expired_polls()

    assert closed == 1
    assert telegram.stopped_polls == [{"chat_id": "-1001", "message_id": 321}]
    assert repository.get_poll("expired-empty")["status"] == "closing"
    assert telegram.sent_messages == []

    with repository.connect() as connection:
        connection.execute(
            "UPDATE polls SET closed_at = ? WHERE poll_id = ?",
            ("2000-01-01T00:00:00+00:00", "expired-empty"),
        )
    finalized = service.close_expired_polls()

    assert finalized == 1
    assert repository.get_poll("expired-empty")["status"] == "closed"
    assert len(telegram.sent_messages) == 1
    announcement = telegram.sent_messages[-1]
    assert announcement["message_thread_id"] == 999
    assert announcement["disable_notification"] is True
    assert "network" in announcement["text"]
    assert "normal" in announcement["text"]
    assert "https://t.me/c/1/321" in announcement["text"]


def test_answer_after_planned_close_is_recorded_while_telegram_poll_is_active(
    tmp_path: Path,
) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="late-active",
        telegram_message_id=321,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )

    result = service.handle_update(
        {
            "update_id": 500,
            "poll_answer": {
                "poll_id": "late-active",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )

    assert result["recorded"] is True
    assert result["delta"] == 10
    assert repository.get_score("main", 42)["answered_count"] == 1


def test_challenge_answer_is_recorded_during_closing_grace(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="late-challenge",
        telegram_message_id=322,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
        requested_by=42,
        request_cost=10,
        request_reward=25,
    )
    assert repository.mark_poll_closing(
        "late-challenge",
        closed_at=utc_now_iso(),
    ) is True

    result = service.handle_update(
        {
            "update_id": 501,
            "poll_answer": {
                "poll_id": "late-challenge",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )

    assert result["recorded"] is True
    assert result["delta"] == 25

    with repository.connect() as connection:
        connection.execute(
            "UPDATE polls SET closed_at = ? WHERE poll_id = ?",
            ("2000-01-01T00:00:00+00:00", "late-challenge"),
        )
    assert service.close_expired_polls() == 1
    assert repository.get_score("main", 42)["points"] == 25
    assert repository.get_score("main", 42)["answered_count"] == 1


def test_ambiguous_stop_failure_keeps_poll_active_and_blocks_new_question(
    tmp_path: Path,
) -> None:
    service, repository, _telegram = make_service(tmp_path)
    telegram = AmbiguousStopPollTelegram()
    service.telegram = telegram  # type: ignore[assignment]
    repository.create_poll(
        poll_id="stop-failed",
        telegram_message_id=323,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )

    assert service.close_expired_polls() == 0
    assert repository.get_poll("stop-failed")["status"] == "active"

    post = service.post_question(skip_busy_topics=True)

    assert post.posted is False
    assert telegram.sent_polls == []
    assert telegram.stop_attempts == 2
    assert repository.recent_error_events()[0]["event"] == "stop_poll_failed"


def test_already_closed_telegram_poll_enters_delivery_grace(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    service.telegram = AlreadyClosedStopPollTelegram()  # type: ignore[assignment]
    repository.create_poll(
        poll_id="already-closed",
        telegram_message_id=328,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )

    assert service.close_expired_polls() == 1
    assert repository.get_poll("already-closed")["status"] == "closing"
    assert repository.recent_error_events() == []


def test_poll_that_cannot_be_stopped_enters_delivery_grace(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    service.telegram = CannotStopPollTelegram()  # type: ignore[assignment]
    repository.create_poll(
        poll_id="cannot-stop",
        telegram_message_id=329,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )

    assert service.close_expired_polls() == 1
    assert repository.get_poll("cannot-stop")["status"] == "closing"
    assert repository.recent_error_events() == []


def test_closed_poll_update_starts_answer_delivery_grace(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="telegram-closed",
        telegram_message_id=324,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-22T00:00:00+00:00",
        closes_at="2026-07-22T02:00:00+00:00",
        explanation="",
    )

    result = service.handle_update(
        {
            "update_id": 502,
            "poll": {
                "id": "telegram-closed",
                "is_closed": True,
                "total_voter_count": 3,
            },
        }
    )

    assert result["closing"] is True
    assert repository.get_poll("telegram-closed")["status"] == "closing"
    assert repository.get_poll("telegram-closed")["telegram_voter_count"] == 3


def test_poll_answer_count_mismatch_is_reported_after_grace(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="missing-answer",
        telegram_message_id=326,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-22T00:00:00+00:00",
        closes_at="2026-07-22T02:00:00+00:00",
        explanation="",
    )
    service.handle_update(
        {
            "update_id": 504,
            "poll": {
                "id": "missing-answer",
                "is_closed": True,
                "total_voter_count": 1,
            },
        }
    )
    with repository.connect() as connection:
        connection.execute(
            "UPDATE polls SET closed_at = ? WHERE poll_id = ?",
            ("2000-01-01T00:00:00+00:00", "missing-answer"),
        )

    assert service.close_expired_polls() == 1

    error = repository.recent_error_events()[0]
    assert error["event"] == "poll_answer_count_mismatch"
    assert "telegram=1, sqlite=0" in error["message"]


def test_voter_count_mismatch_extends_grace_and_accepts_delayed_answer(
    tmp_path: Path,
) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="extended-grace",
        telegram_message_id=327,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-22T00:00:00+00:00",
        closes_at="2026-07-22T02:00:00+00:00",
        explanation="",
    )
    closed_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert repository.mark_poll_closing(
        "extended-grace",
        closed_at=closed_at,
        telegram_voter_count=1,
    ) is True

    assert service.close_expired_polls() == 0
    assert repository.get_poll("extended-grace")["status"] == "closing"

    answer = service.handle_update(
        {
            "update_id": 505,
            "poll_answer": {
                "poll_id": "extended-grace",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )

    assert answer["recorded"] is True
    assert service.close_expired_polls() == 1
    assert repository.get_poll("extended-grace")["status"] == "closed"


def test_answer_after_finalization_is_rejected_and_recorded_as_error(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="fully-closed",
        telegram_message_id=325,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )
    repository.mark_poll_closed("fully-closed")

    result = service.handle_update(
        {
            "update_id": 503,
            "poll_answer": {
                "poll_id": "fully-closed",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )

    assert result["recorded"] is False
    assert repository.recent_error_events()[0]["event"] == "poll_answer_rejected"
    assert "reason=poll_closed" in repository.recent_error_events()[0]["message"]


def test_no_answers_announcement_can_be_disabled(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), announce_no_answers=False)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.set_bot_setting("announce_thread_id", "999")
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )
    repository.create_poll(
        poll_id="expired-empty",
        telegram_message_id=321,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )

    closed = service.close_expired_polls()

    assert closed == 1
    assert telegram.stopped_polls == [{"chat_id": "-1001", "message_id": 321}]
    assert telegram.sent_messages == []


def test_risk_failure_announcement_can_be_disabled(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), announce_risk_failures=False)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.set_bot_setting("announce_thread_id", "999")
    _seed_today_answer(repository, user_id=900, username="seed", points_difficulty="easy")
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )

    posted = service.post_question(difficulty="normal")
    assert posted.posted is True
    service.handle_update(
        {
            "update_id": 33,
            "callback_query": {
                "id": "cb-risk-disabled",
                "from": {"id": 7, "first_name": "Neo"},
                "data": "bet:2",
                "message": {
                    "chat": {"id": "-1001"},
                    "poll": {"id": str(posted.poll_id)},
                },
            },
        }
    )
    before_messages = len(telegram.sent_messages)

    answer_result = service.handle_update(
        {
            "update_id": 34,
            "poll_answer": {
                "poll_id": str(posted.poll_id),
                "user": {"id": 7, "first_name": "Neo"},
                "option_ids": [1],
            },
        }
    )

    assert answer_result["recorded"] is True
    assert answer_result["delta"] == -10
    assert len(telegram.sent_messages) == before_messages + 1
    assert telegram.sent_messages[-1]["message_thread_id"] == 101


def test_streak_announcement_can_be_disabled(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), announce_streaks=False)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.set_bot_setting("announce_thread_id", "999")
    _seed_today_answer(repository, user_id=900, username="seed", points_difficulty="easy")
    _seed_topic_answer(
        repository,
        poll_id="neo-first",
        topic_key="network",
        user_id=7,
        username="neo",
        difficulty="normal",
    )
    _seed_topic_answer(
        repository,
        poll_id="neo-second",
        topic_key="network",
        user_id=7,
        username="neo",
        difficulty="normal",
    )
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )

    posted = service.post_question(difficulty="normal")
    assert posted.posted is True
    before_messages = len(telegram.sent_messages)

    answer_result = service.handle_update(
        {
            "update_id": 35,
            "poll_answer": {
                "poll_id": str(posted.poll_id),
                "user": {"id": 7, "first_name": "Neo"},
                "option_ids": [0],
            },
        }
    )

    assert answer_result["recorded"] is True
    assert answer_result["delta"] == 13
    assert len(telegram.sent_messages) == before_messages + 1
    assert telegram.sent_messages[-1]["message_thread_id"] == 101


def test_top_command_can_filter_by_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    _seed_topic_answer(
        repository,
        poll_id="network-ada",
        topic_key="network",
        user_id=42,
        username="ada",
        difficulty="normal",
    )
    _seed_topic_answer(
        repository,
        poll_id="security-linus",
        topic_key="security",
        user_id=99,
        username="linus",
        difficulty="hard",
    )
    _seed_topic_answer(
        repository,
        poll_id="network-old",
        topic_key="network",
        user_id=100,
        username="oldtimer",
        difficulty="hard",
        season="old",
    )

    result = service.handle_update(
        {
            "update_id": 90,
            "message": {
                "message_id": 80,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 42, "first_name": "Ada"},
                "text": "/top network",
            },
        }
    )

    assert result["command"] == "/top"
    assert result["topic_key"] == "network"
    text = telegram.sent_messages[-1]["text"]
    assert "Табло сектора network:" in text
    assert "@ada - 10" in text
    assert "@linus" not in text
    assert "@oldtimer" not in text


def test_top_command_reports_unknown_topic(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 91,
            "message": {
                "message_id": 80,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 42, "first_name": "Ada"},
                "text": "/top unknown",
            },
        }
    )

    assert result["command"] == "/top"
    text = telegram.sent_messages[-1]["text"]
    assert "Сектор unknown не найден." in text
    assert "network" in text


def test_rules_command_uses_configured_scoring_rules(tmp_path: Path) -> None:
    settings = replace(
        make_settings(tmp_path),
        difficulty_points={"easy": 5, "normal": 10, "hard": 15, "ccna": 20},
        challenge_economy={
            "easy": {"cost": 5, "reward": 10},
            "normal": {"cost": 10, "reward": 25},
            "hard": {"cost": 15, "reward": 40},
            "ccna": {"cost": 20, "reward": 55},
        },
    )
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )

    result = service.handle_update(
        {
            "update_id": 92,
            "message": {
                "message_id": 80,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 42, "first_name": "Ada"},
                "text": "/rules",
            },
        }
    )

    assert result["command"] == "/rules"
    text = telegram.sent_messages[-1]["text"]
    assert text.index("easy - 5") < text.index("normal - 10")
    assert text.index("normal - 10") < text.index("hard - 15")
    assert text.index("hard - 15") < text.index("ccna - 20")
    assert "Вызовы: easy 5->10, normal 10->25, hard 15->40, ccna 20->55." in text


def test_public_kvizi_help_is_available_to_non_admins(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 95,
            "message": {
                "message_id": 83,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 42, "first_name": "Ada"},
                "text": "/kvizi_help",
            },
        }
    )

    assert result["command"] == "/kvizi_help"
    text = telegram.sent_messages[-1]["text"]
    assert "Квизи-справка для участников:" in text
    assert "/me" in text
    assert "/top <topic_key>" in text
    assert "/kvizi_challenge <difficulty>" in text
    assert "только для администраторов" not in text


def test_admin_config_command_shows_configured_scoring_rules(tmp_path: Path) -> None:
    settings = replace(
        make_settings(tmp_path),
        difficulty_points={"easy": 5, "normal": 10, "hard": 15, "ccna": 20},
        challenge_economy={
            "easy": {"cost": 5, "reward": 10},
            "normal": {"cost": 10, "reward": 25},
            "hard": {"cost": 15, "reward": 40},
            "ccna": {"cost": 20, "reward": 55},
        },
    )
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )

    result = service.handle_update(
        {
            "update_id": 93,
            "message": {
                "message_id": 81,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_config",
            },
        }
    )

    assert result["command"] == "/kvizi_config"
    text = telegram.sent_messages[-1]["text"]
    assert "Конфиг Квизи:" in text
    assert "- easy: 5" in text
    assert "- normal: 10" in text
    assert "- hard: 15" in text
    assert "- ccna: 20" in text
    assert "- ccna: стоимость 20, награда +55" in text
    assert "Анонсы:" in text
    assert "- first_answer: on" in text
    assert "- no_answers: on" in text
    assert "- risk_failures: on" in text
    assert "- streaks: on" in text


def test_admin_voice_preview_shows_current_copy_without_side_effects(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 94,
            "message": {
                "message_id": 82,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_voice_preview",
            },
        }
    )

    assert result["command"] == "/kvizi_voice_preview"
    assert telegram.sent_polls == []
    message = telegram.sent_messages[-1]
    assert message["message_thread_id"] == 101
    text = message["text"]
    assert "Голосовой пример Квизи:" in text
    assert "Опрос:" in text
    assert "Анонс:" in text
    assert "Ставки:" in text
    assert "Счёт:" in text
    assert "Итоги дня:" in text
    assert "network" in text
    assert "https://t.me/c/123456789/42" in text
    assert "@guest" in text


def test_admin_prod_check_reports_ready_state(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    now = datetime.now(timezone.utc)
    for index, (status, message) in enumerate(
        (
            ("posted", "О, hardware! Какое совпадение: вопрос как раз искал сектор с хорошей акустикой."),
            ("maintenance_ok", "Closed expired polls: 0"),
            ("daily_posted", "Итоги дня 2026-07-06: Вопросы: 4 Ответы: 3 от 1 участников."),
            ("backup_sent", "Backup kvizi-backup-20260706T200054Z.json: sent=1/1, failed=0"),
        ),
        start=1,
    ):
        started_at = (now - timedelta(minutes=index + 1)).isoformat()
        finished_at = (now - timedelta(minutes=index)).isoformat()
        repository.record_cron_run(started_at, finished_at, status, message)

    result = service.handle_update(
        {
            "update_id": 96,
            "message": {
                "message_id": 84,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_prod_check",
            },
        }
    )

    assert result["command"] == "/kvizi_prod_check"
    text = telegram.sent_messages[-1]["text"]
    assert "Prod-check Квизи: OK" in text
    assert "[OK] questions.csv: 2 вопросов, duplicate ids: none" in text
    assert "[OK] CSV-топики привязаны" in text
    assert "[OK] анонс-топик: 999" in text
    assert "[OK] просроченных poll нет" in text
    assert "[OK] cron/tick: posted" in text
    assert "[OK] cron/maintenance: maintenance_ok" in text
    assert "[OK] cron/daily: daily_posted" in text
    assert "[OK] cron/backup: backup_sent" in text
    assert "Какое совпадение" not in text
    assert "Closed expired polls" not in text
    assert "Backup kvizi-backup" not in text
    assert "WARN" not in text
    assert "FAIL" not in text


def test_admin_prod_check_treats_transient_telegram_errors_as_info(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    now = datetime.now(timezone.utc)
    for index, status in enumerate(
        ("posted", "maintenance_ok", "daily_posted", "backup_sent"),
        start=1,
    ):
        started_at = (now - timedelta(minutes=index + 1)).isoformat()
        finished_at = (now - timedelta(minutes=index)).isoformat()
        repository.record_cron_run(started_at, finished_at, status, "ok")
    repository.record_error_event(
        source="telegram",
        event="webhook_update_failed",
        message=(
            "Telegram sendMessage request failed after 3 attempts: "
            "HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded with url: /bot<bot_token>/sendMessage "
            "(Caused by ProxyError('Unable to connect to proxy', "
            "OSError('Tunnel connection failed: 503 Service Unavailable')))"
        ),
        created_at=(now - timedelta(minutes=2)).isoformat(),
    )

    result = service.handle_update(
        {
            "update_id": 97,
            "message": {
                "message_id": 85,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_prod_check",
            },
        }
    )

    assert result["command"] == "/kvizi_prod_check"
    text = telegram.sent_messages[-1]["text"]
    assert "Prod-check Квизи: OK" in text
    assert "[INFO] transient Telegram/proxy events: 1; смотри /kvizi_errors" in text
    assert "свежие error events" not in text
    assert "[WARN]" not in text
    assert "[FAIL]" not in text


def test_admin_prod_check_does_not_double_report_exact_cron_failure(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    now = datetime.now(timezone.utc)
    for index, status in enumerate(
        ("maintenance_ok", "daily_posted", "backup_sent"),
        start=1,
    ):
        repository.record_cron_run(
            (now - timedelta(minutes=index + 1)).isoformat(),
            (now - timedelta(minutes=index)).isoformat(),
            status,
            "ok",
        )
    failed_at = (now - timedelta(seconds=30)).isoformat()
    repository.record_cron_run(
        (now - timedelta(minutes=1)).isoformat(),
        failed_at,
        "failed",
        "tick exploded",
    )
    repository.record_error_event(
        source="cron",
        event="tick_failed",
        message="tick exploded",
        created_at=failed_at,
    )

    result = service.handle_update(
        {
            "update_id": 971,
            "message": {
                "message_id": 851,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_prod_check",
            },
        }
    )

    assert result["command"] == "/kvizi_prod_check"
    text = telegram.sent_messages[-1]["text"]
    assert "Prod-check Квизи: FAIL" in text
    assert "свежие failed cron: 1" in text
    assert "свежие error events" not in text


def test_admin_prod_check_warns_for_final_answer_delivery_mismatch(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    now = datetime.now(timezone.utc)
    for index, status in enumerate(
        ("posted", "maintenance_ok", "daily_posted", "backup_sent"),
        start=1,
    ):
        repository.record_cron_run(
            (now - timedelta(minutes=index + 1)).isoformat(),
            (now - timedelta(minutes=index)).isoformat(),
            status,
            "ok",
        )
    repository.create_poll(
        poll_id="prod-check-mismatch",
        telegram_message_id=557,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at=(now - timedelta(hours=2)).isoformat(),
        closes_at=(now + timedelta(hours=1)).isoformat(),
        explanation="",
    )
    repository.record_answer(
        season="main",
        poll_id="prod-check-mismatch",
        user={"id": 42, "first_name": "Ada"},
        option_ids=[0],
        now_iso=(now - timedelta(hours=1)).isoformat(),
    )
    assert repository.mark_poll_closing(
        "prod-check-mismatch",
        closed_at=now.isoformat(),
        telegram_voter_count=2,
    ) is True
    repository.mark_poll_closed("prod-check-mismatch")

    result = service.handle_update(
        {
            "update_id": 98,
            "message": {
                "message_id": 86,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_prod_check",
            },
        }
    )

    assert result["command"] == "/kvizi_prod_check"
    text = telegram.sent_messages[-1]["text"]
    assert "Prod-check Квизи: WARN" in text
    assert "[WARN] аудит ответов: подтверждённых расхождений 1" in text
    assert "/kvizi_recent и /kvizi_errors" in text


def test_post_question_sends_announcement_with_private_group_link(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert posted.question_link == "https://t.me/c/1/1"
    announcement = telegram.sent_messages[-1]
    assert announcement["chat_id"] == "-1001"
    assert announcement["message_thread_id"] == 999
    assert announcement["disable_notification"] is True
    assert "network" in announcement["text"]
    assert "normal" in announcement["text"]
    assert "10" in announcement["text"]
    assert announcement["text"].endswith("\nhttps://t.me/c/1/1")


def test_postnow_command_does_not_echo_announcement_in_source_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")

    result = service.handle_update(
        {
            "update_id": 10,
            "message": {
                "message_id": 77,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_postnow",
            },
        }
    )

    assert result["posted"] is True
    assert len(telegram.sent_polls) == 1
    assert len(telegram.sent_messages) == 1
    assert telegram.sent_messages[0]["message_thread_id"] == 999


def test_challenge_posts_selected_difficulty_and_rewards_requester(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.record_answer(
        season="main",
        poll_id=_seed_poll(repository, difficulty="hard"),
        user={"id": 7, "first_name": "Admin"},
        option_ids=[0],
        now_iso="2026-07-05T20:00:00+00:00",
    )

    result = service.handle_update(
        {
            "update_id": 20,
            "message": {
                "message_id": 77,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_challenge hard",
            },
        }
    )

    assert result["posted"] is True
    assert result["difficulty"] == "hard"
    assert "Which record maps a name to IPv4?" in telegram.sent_polls[-1]["question"]

    answer_result = service.handle_update(
        {
            "update_id": 21,
            "poll_answer": {
                "poll_id": "poll-1",
                "user": {"id": 7, "first_name": "Admin"},
                "option_ids": [0],
            },
        }
    )

    assert answer_result["recorded"] is True
    assert answer_result["delta"] == 40
    assert repository.get_score("main", 7)["points"] == 55


def test_custom_difficulty_scoring_rules_and_challenge_economy(tmp_path: Path) -> None:
    settings = replace(
        make_settings(tmp_path),
        difficulty_points={"easy": 5, "normal": 10, "hard": 15, "ccna": 20},
        challenge_economy={
            "easy": {"cost": 5, "reward": 10},
            "normal": {"cost": 10, "reward": 25},
            "hard": {"cost": 15, "reward": 40},
            "ccna": {"cost": 20, "reward": 55},
        },
    )
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=QuestionBank(
            [
                Question(
                    id="ccna-1",
                    topic_key="network",
                    difficulty="ccna",
                    text="Which OSI layer does IP operate at?",
                    options=("Network", "Transport", "Session", "Application"),
                    correct_option_id=0,
                    explanation="IP is a network layer protocol.",
                    source="",
                )
            ]
        ),
    )

    posted = service.post_question(difficulty="ccna")
    assert posted.posted is True
    assert "network" in posted.message
    assert "ccna" in posted.message
    assert "20" in posted.message

    answer_result = service.handle_update(
        {
            "update_id": 35,
            "poll_answer": {
                "poll_id": "poll-1",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )
    assert answer_result["delta"] == 20
    assert repository.get_score("main", 42)["points"] == 20

    repository.mark_poll_closed("poll-1")
    challenge_result = service.handle_update(
        {
            "update_id": 36,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 42, "first_name": "Ada"},
                "text": "/kvizi_challenge ccna",
            },
        }
    )
    assert challenge_result["posted"] is True
    assert challenge_result["cost"] == 20
    assert challenge_result["reward"] == 55

    challenge_answer = service.handle_update(
        {
            "update_id": 37,
            "poll_answer": {
                "poll_id": "poll-2",
                "user": {"id": 42, "first_name": "Ada"},
                "option_ids": [0],
            },
        }
    )
    assert challenge_answer["delta"] == 55
    assert repository.get_score("main", 42)["points"] == 75


def test_challenge_rejects_requester_bet_and_penalizes_wrong_answer(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.record_answer(
        season="main",
        poll_id=_seed_poll(repository),
        user={"id": 7, "first_name": "Admin"},
        option_ids=[0],
        now_iso="2026-07-05T20:00:00+00:00",
    )
    service.handle_update(
        {
            "update_id": 30,
            "message": {
                "message_id": 77,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_challenge normal",
            },
        }
    )

    bet_result = service.handle_update(
        {
            "update_id": 31,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 7, "first_name": "Admin"},
                "data": "bet:3",
                "message": {
                    "chat": {"id": "-1001"},
                    "poll": {"id": "poll-1"},
                },
            },
        }
    )
    assert bet_result["bet"] is False
    assert telegram.callback_answers[-1]["show_alert"] is True

    answer_result = service.handle_update(
        {
            "update_id": 32,
            "poll_answer": {
                "poll_id": "poll-1",
                "user": {"id": 7, "first_name": "Admin"},
                "option_ids": [1],
            },
        }
    )

    assert answer_result["delta"] == -10
    assert repository.get_score("main", 7)["points"] == 0


def test_completed_challenge_allows_next_challenge_after_poll_is_closed(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.record_answer(
        season="main",
        poll_id=_seed_poll(repository, difficulty="hard"),
        user={"id": 7, "first_name": "Admin"},
        option_ids=[0],
        now_iso="2026-07-05T20:00:00+00:00",
    )

    first = service.handle_update(
        {
            "update_id": 40,
            "message": {
                "message_id": 77,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_challenge normal",
            },
        }
    )
    assert first["posted"] is True

    service.handle_update(
        {
            "update_id": 41,
            "poll_answer": {
                "poll_id": "poll-1",
                "user": {"id": 7, "first_name": "Admin"},
                "option_ids": [0],
            },
        }
    )
    close_result = service.handle_update(
        {
            "update_id": 42,
            "message": {
                "message_id": 78,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_close_here",
            },
        }
    )
    assert close_result["closed"] == 1

    second = service.handle_update(
        {
            "update_id": 43,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_challenge hard",
            },
        }
    )

    assert second["posted"] is True
    assert second["difficulty"] == "hard"
    assert len(telegram.sent_polls) == 2


def test_challenge_rejects_when_topic_has_active_poll(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.record_answer(
        season="main",
        poll_id=_seed_poll(repository, difficulty="hard"),
        user={"id": 7, "first_name": "Admin"},
        option_ids=[0],
        now_iso="2026-07-05T20:00:00+00:00",
    )
    assert service.post_question(difficulty="normal").posted is True

    result = service.handle_update(
        {
            "update_id": 60,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_challenge hard",
            },
        }
    )

    assert result["posted"] is False
    assert len(telegram.sent_polls) == 1
    assert "уже есть активный вопрос" in telegram.sent_messages[-1]["text"]


def test_postnow_rejects_when_current_topic_has_active_poll(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    assert service.post_question(difficulty="normal").posted is True

    result = service.handle_update(
        {
            "update_id": 61,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_postnow",
            },
        }
    )

    assert result["posted"] is False
    assert len(telegram.sent_polls) == 1
    assert "уже есть активный вопрос" in telegram.sent_messages[-1]["text"]


def test_close_here_stops_and_marks_active_polls_closed(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    assert service.post_question(difficulty="normal").posted is True

    result = service.handle_update(
        {
            "update_id": 62,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_close_here",
            },
        }
    )

    assert result["closed"] == 1
    assert len(telegram.stopped_polls) == 1
    assert repository.active_polls_for_thread(101, "2999-01-01T00:00:00+00:00") == []


def test_close_here_announces_no_answers_to_announce_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    assert service.post_question(difficulty="normal").posted is True
    before_messages = len(telegram.sent_messages)

    result = service.handle_update(
        {
            "update_id": 63,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_close_here",
            },
        }
    )

    assert result["closed"] == 1
    assert len(telegram.sent_messages) == before_messages + 1
    reply = telegram.sent_messages[-1]
    assert reply["message_thread_id"] == 101

    with repository.connect() as connection:
        connection.execute(
            "UPDATE polls SET closed_at = ? WHERE poll_id = ?",
            ("2000-01-01T00:00:00+00:00", "poll-1"),
        )
    assert service.close_expired_polls() == 1

    announcement = telegram.sent_messages[-1]
    assert announcement["message_thread_id"] == 999
    assert announcement["disable_notification"] is True
    assert "network" in announcement["text"]
    assert "normal" in announcement["text"]
    assert "https://t.me/c/1/1" in announcement["text"]
    assert "Закрыто активных вопросов" in reply["text"]


def test_close_here_does_not_announce_no_answers_when_poll_had_answer(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")
    repository.create_poll(
        poll_id="answered-active",
        telegram_message_id=321,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-05T20:00:00+00:00",
        closes_at="2999-01-01T00:00:00+00:00",
        explanation="",
    )
    repository.record_answer(
        season="main",
        poll_id="answered-active",
        user={"id": 7, "first_name": "Admin"},
        option_ids=[0],
        now_iso="2026-07-05T20:01:00+00:00",
    )

    result = service.handle_update(
        {
            "update_id": 64,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_close_here",
            },
        }
    )

    assert result["closed"] == 1
    assert len(telegram.sent_messages) == 1
    assert telegram.sent_messages[-1]["message_thread_id"] == 101
    assert "Закрыто активных вопросов" in telegram.sent_messages[-1]["text"]


def test_question_announcement_failure_is_non_blocking(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.set_bot_setting("announce_thread_id", "999")
    telegram = FailOnceSendMessageTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=make_question_bank(),
    )

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert len(telegram.sent_polls) == 1
    assert telegram.sent_messages == []
    errors = repository.recent_error_events()
    assert errors[-1]["event"] == "question_announcement_failed"
    assert "proxy 503" in errors[-1]["message"]
    pending = repository.pending_announcements_due("9999-01-01T00:00:00+00:00")
    assert len(pending) == 1
    assert pending[0]["attempt_count"] == 1

    retried = service.retry_pending_announcements(
        now=datetime.now(timezone.utc) + timedelta(hours=1)
    )

    assert retried == 1
    assert len(telegram.sent_messages) == 1
    assert telegram.sent_messages[0]["message_thread_id"] == 999
    assert repository.pending_announcements_due("9999-01-01T00:00:00+00:00") == []


def test_overlapping_announcement_retries_send_once(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    telegram = BlockingSendMessageTelegram()
    service.telegram = telegram  # type: ignore[assignment]
    now = datetime.now(timezone.utc)
    repository.enqueue_pending_announcement(
        dedupe_key="overlapping-retry",
        message_thread_id=999,
        text="Question announcement",
        event="question_announcement_failed",
        next_attempt_at=(now - timedelta(minutes=1)).isoformat(),
        created_at=(now - timedelta(minutes=5)).isoformat(),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.retry_pending_announcements, now=now)
        assert telegram.entered.wait(timeout=2)
        second = executor.submit(service.retry_pending_announcements, now=now)
        assert second.result(timeout=2) == 0
        telegram.release.set()
        assert first.result(timeout=2) == 1

    assert len(telegram.sent_messages) == 1
    assert repository.pending_announcements_due("9999-01-01T00:00:00+00:00") == []


def test_announcement_retry_stops_after_max_attempts(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    service.telegram = AlwaysFailSendMessageTelegram()  # type: ignore[assignment]
    repository.set_bot_setting("announce_thread_id", "999")

    assert service._send_announcement(  # noqa: SLF001
        text="Question announcement",
        event="question_announcement_failed",
    ) is False
    first_retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    assert service.retry_pending_announcements(now=first_retry_at) == 0
    assert service.retry_pending_announcements(
        now=first_retry_at + timedelta(hours=1)
    ) == 0

    assert repository.pending_announcements_due("9999-01-01T00:00:00+00:00") == []
    events = repository.recent_error_events()
    assert events[0]["event"] == "announcement_retry_exhausted"
    assert "attempts=3" in events[0]["message"]


def test_ai_question_copy_sends_static_text_then_edits_safe_facts(tmp_path: Path) -> None:
    provider = FakeAIProvider(
        [ai_teaser("Names are looking for addresses — time to identify their guide.", "Names")]
    )
    service, repository, telegram = make_ai_service(tmp_path, provider)

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert len(telegram.sent_messages) == 1
    assert len(telegram.edited_messages) == 1
    edited = telegram.edited_messages[0]
    assert edited["chat_id"] == "-1001"
    assert edited["message_id"] == 101
    assert edited["text"].startswith("Names are looking for addresses")
    assert "Сектор: network · сложность: normal · база: 10" in edited["text"]
    assert edited["text"].endswith("https://t.me/c/1/1")
    assert repository.pending_ai_enhancement_count() == 0
    assert provider.calls[0]["purpose"] == "question_announcement"
    assert provider.calls[0]["timeout_seconds"] == 7.0
    system_prompt = provider.calls[0]["messages"][0]["content"]
    user_prompt = provider.calls[0]["messages"][-1]["content"]
    assert "anchor — дословная непрерывная цитата" in system_prompt
    assert "Не отвечай на вопрос" in system_prompt
    assert '"question": "What resolves names?"' in user_prompt
    assert '"topic": "network"' in user_prompt
    assert '"forbidden_answers": ["DNS", "SMTP", "DHCP", "ARP"]' in user_prompt
    assert "base_points" not in user_prompt
    assert "question_link" not in user_prompt


@pytest.mark.parametrize(
    "generated",
    (
        ai_teaser("Names are assigned by DNS, so the mystery is over.", "Names"),
        ai_teaser(
            "Names — сложное сочетание слов, которое может означать одно, а значит и другое.",
            "Names",
        ),
    ),
)
def test_ai_question_copy_rejects_spoilers_and_low_quality_text(
    tmp_path: Path,
    generated: str,
) -> None:
    provider = FakeAIProvider([generated])
    service, repository, telegram = make_ai_service(tmp_path, provider)

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert len(telegram.sent_messages) == 1
    assert telegram.edited_messages == []
    assert repository.pending_ai_enhancement_count() == 0
    errors = repository.recent_error_events(limit=1)
    assert errors[0]["source"] == "ai"
    assert errors[0]["event"] == "ai_output_rejected"
    assert "kind=invalid_output" in errors[0]["message"]
    error_report = service._format_errors()
    assert "требуют внимания=0" in error_report
    assert "AI fallback=1" in error_report
    assert "AI-подводки отклонены, оставлен copy.py:" in error_report


def test_retryable_ai_failure_keeps_static_copy_and_edits_later(tmp_path: Path) -> None:
    provider = FakeAIProvider(
        [
            AIProviderError(
                "Groq HTTP 429",
                kind="rate_limit",
                retryable=True,
                retry_after_seconds=0,
            ),
            ai_teaser(
                "Names have lost their addresses again — time to question the network.",
                "Names",
            ),
        ]
    )
    service, repository, telegram = make_ai_service(tmp_path, provider)

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert len(telegram.sent_messages) == 1
    assert telegram.edited_messages == []
    assert repository.pending_ai_enhancement_count() == 1

    delivered = service.retry_ai_enhancements(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    assert delivered == 1
    assert len(telegram.edited_messages) == 1
    assert len(provider.calls) == 2
    assert repository.pending_ai_enhancement_count() == 0


def test_delayed_static_announcement_still_schedules_ai_after_retry(tmp_path: Path) -> None:
    provider = FakeAIProvider(
        [ai_teaser("Names opened the network address book — someone must keep order.", "Names")]
    )
    service, repository, telegram = make_ai_service(
        tmp_path,
        provider,
        telegram=FailOnceSendMessageTelegram(),
    )

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert telegram.sent_messages == []
    assert provider.calls == []

    delivered = service.retry_pending_announcements(
        now=datetime.now(timezone.utc) + timedelta(minutes=10)
    )

    assert delivered == 1
    assert len(telegram.sent_messages) == 1
    assert telegram.edited_messages == []
    assert service.retry_ai_enhancements(
        now=datetime.now(timezone.utc) + timedelta(minutes=10)
    ) == 1
    assert len(telegram.edited_messages) == 1
    assert len(provider.calls) == 1
    assert repository.pending_ai_enhancement_count() == 0


def test_ai_candidate_is_reused_when_telegram_edit_needs_retry(tmp_path: Path) -> None:
    provider = FakeAIProvider(
        [ai_teaser("Names seek their addresses, and the network owes them an introduction.", "Names")]
    )
    service, repository, telegram = make_ai_service(
        tmp_path,
        provider,
        telegram=FailOnceEditMessageTelegram(),
    )

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert repository.pending_ai_enhancement_count() == 1
    assert len(provider.calls) == 1

    delivered = service.retry_ai_enhancements(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    assert delivered == 1
    assert len(provider.calls) == 1
    assert telegram.edit_attempts == 2
    assert len(telegram.edited_messages) == 1
    assert repository.pending_ai_enhancement_count() == 0


def test_overlapping_ai_retries_claim_one_job_once(tmp_path: Path) -> None:
    provider = BlockingAIProvider(
        ai_teaser("Names seek their addresses, and the network owes them an introduction.", "Names")
    )
    service, repository, telegram = make_ai_service(tmp_path, provider)
    now = datetime.now(timezone.utc)
    repository.enqueue_ai_enhancement(
        dedupe_key="overlapping-ai-retry",
        purpose="question_announcement",
        chat_id="-1001",
        message_thread_id=999,
        telegram_message_id=555,
        base_text="Static copy",
        context_json=json.dumps(
            {
                "topic_key": "network",
                "difficulty": "normal",
                "base_points": 10,
                "question_link": "https://t.me/c/1/1",
                "question_text": "What resolves names?",
                "blocked_answers": ["DNS", "SMTP", "DHCP", "ARP"],
            }
        ),
        next_attempt_at=(now - timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(minutes=1)).isoformat(),
        created_at=now.isoformat(),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.retry_ai_enhancements, now=now)
        assert provider.entered.wait(timeout=2)
        second = executor.submit(service.retry_ai_enhancements, now=now)
        assert second.result(timeout=2) == 0
        provider.release.set()
        assert first.result(timeout=2) == 1

    assert len(provider.calls) == 1
    assert len(telegram.edited_messages) == 1
    assert repository.pending_ai_enhancement_count() == 0


def test_ai_copy_flags_off_do_not_call_provider(tmp_path: Path) -> None:
    provider = FakeAIProvider(["Не должно использоваться."])
    service, repository, telegram = make_service(tmp_path)
    service.ai_provider = provider
    repository.set_bot_setting("announce_thread_id", "999")

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert len(telegram.sent_messages) == 1
    assert telegram.edited_messages == []
    assert provider.calls == []
    assert repository.pending_ai_enhancement_count() == 0


def test_admin_ai_status_does_not_call_provider(tmp_path: Path) -> None:
    provider = FakeAIProvider([])
    service, _repository, telegram = make_ai_service(tmp_path, provider)

    result = service.handle_update(
        {
            "update_id": 902,
            "message": {
                "message_id": 88,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_ai_status",
            },
        }
    )

    assert result["command"] == "/kvizi_ai_status"
    text = telegram.sent_messages[-1]["text"]
    assert "AI Квизи:" in text
    assert "генерация подводок: ON" in text
    assert "fake/fake-copy-model" in text
    assert "prompt-skill: question-teaser-v2" in text
    assert "ожидают улучшения: 0" in text
    assert provider.calls == []


def test_admin_ai_preview_generates_three_variants_without_publishing(tmp_path: Path) -> None:
    provider = FakeAIProvider(
        [
            ai_teaser(
                "Единица данных ждёт короткого имени, а модель OSI снова разложила всё по полкам.",
                "единица данных",
            ),
            ai_teaser(
                "У канального уровня сегодня собеседование — без подсказок и лишнего пафоса.",
                "канального уровня",
            ),
            ai_teaser(
                "Модель OSI распределила роли и оставила одно короткое имя на десерт.",
                "модели OSI",
            ),
        ]
    )
    service, repository, telegram = make_ai_service(tmp_path, provider)

    result = service.handle_update(
        {
            "update_id": 903,
            "message": {
                "message_id": 89,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_ai_preview network",
            },
        }
    )

    assert result == {
        "ok": True,
        "command": "/kvizi_ai_preview",
        "scenario": "network",
        "generated": 3,
        "failed": 0,
    }
    assert len(provider.calls) == 3
    assert all(call["purpose"] == "question_announcement_preview" for call in provider.calls)
    assert telegram.sent_polls == []
    assert telegram.edited_messages == []
    assert repository.asked_question_ids("network") == set()
    assert repository.pending_ai_enhancement_count() == 0
    assert len(telegram.sent_messages) == 1
    text = telegram.sent_messages[0]["text"]
    assert "AI-preview Квизи:" in text
    assert "1. Единица данных" in text
    assert "2. У канального уровня" in text
    assert "3. Модель OSI" in text
    assert "poll, анонсы и история вопросов не изменены" in text


def test_admin_ai_preview_rejects_unknown_scenario_without_provider_call(tmp_path: Path) -> None:
    provider = FakeAIProvider([])
    service, _repository, telegram = make_ai_service(tmp_path, provider)

    result = service.handle_update(
        {
            "update_id": 904,
            "message": {
                "message_id": 90,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_ai_preview unknown",
            },
        }
    )

    assert result["generated"] == 0
    assert provider.calls == []
    assert "Формат: /kvizi_ai_preview" in telegram.sent_messages[-1]["text"]


def test_admin_status_reports_loaded_questions_topics_active_polls_and_cron(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.upsert_user({"id": 7, "username": "adminuser", "first_name": "Admin"})
    repository.create_poll(
        poll_id="challenge-poll",
        telegram_message_id=555,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-05T20:00:00+00:00",
        closes_at="2999-01-01T00:00:00+00:00",
        explanation="",
        requested_by=7,
        request_cost=10,
        request_reward=25,
    )
    repository.set_bot_setting("announce_thread_id", "999")
    repository.record_cron_run(
        "2026-07-05T20:00:00+00:00",
        "2026-07-05T20:00:01+00:00",
        "posted",
        "ok",
    )
    now = datetime.now(timezone.utc)
    repository.enqueue_pending_announcement(
        dedupe_key="status-pending-announcement",
        message_thread_id=999,
        text="Queued announcement",
        event="question_announcement_failed",
        next_attempt_at=(now + timedelta(minutes=5)).isoformat(),
    )
    assert repository.try_claim_operation(
        "post_question",
        claimed_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
    )

    result = service.handle_update(
        {
            "update_id": 50,
            "message": {
                "message_id": 79,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_status",
            },
        }
    )

    assert result["command"] == "/kvizi_status"
    text = telegram.sent_messages[-1]["text"]
    assert "Статус Квизи:" in text
    assert "Вопросы: 2" in text
    assert "network: thread=101" in text
    assert "Активные вопросы: 1" in text
    assert "challenge user=@adminuser (7)" in text
    assert "05.07.2026 23:00:01 MSK" in text
    assert "2026-07-05 20:00:01" not in text
    assert "Анонс-топик: 999" in text
    assert "Анонсы в очереди: 1" in text
    assert "Защита публикации: активна до" in text
    assert "Последний cron: posted" in text


def test_admin_recent_reports_recent_questions_and_answers(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.upsert_user({"id": 42, "username": "ada", "first_name": "Ada"})
    repository.upsert_user({"id": 43, "first_name": "Bob"})
    repository.create_poll(
        poll_id="poll-recent",
        telegram_message_id=555,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-05T20:00:00+00:00",
        closes_at="2999-01-01T00:00:00+00:00",
        explanation="",
    )
    repository.record_answer(
        season="main",
        poll_id="poll-recent",
        user={"id": 42, "username": "ada", "first_name": "Ada"},
        option_ids=[0],
        now_iso="2026-07-05T20:00:10+00:00",
    )
    repository.record_answer(
        season="main",
        poll_id="poll-recent",
        user={"id": 43, "first_name": "Bob"},
        option_ids=[1],
        now_iso="2026-07-05T20:00:20+00:00",
    )

    result = service.handle_update(
        {
            "update_id": 51,
            "message": {
                "message_id": 80,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_recent",
            },
        }
    )

    assert result["command"] == "/kvizi_recent"
    text = telegram.sent_messages[-1]["text"]
    assert "Последние вопросы Квизи:" in text
    assert (
        "05.07.2026 23:00:00 MSK | network normal | active | "
        "Telegram/БД: —/2 (опрос открыт)"
    ) in text
    assert "ответов 2, верно/ошибки 1/1" in text
    assert "@ada" in text
    assert "Bob" in text


def test_admin_recent_reports_answer_delivery_mismatch_states(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="poll-delivery-audit",
        telegram_message_id=556,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-05T21:00:00+00:00",
        closes_at="2999-01-01T00:00:00+00:00",
        explanation="",
    )
    repository.record_answer(
        season="main",
        poll_id="poll-delivery-audit",
        user={"id": 42, "first_name": "Ada"},
        option_ids=[0],
        now_iso="2026-07-05T21:00:10+00:00",
    )
    assert repository.mark_poll_closing(
        "poll-delivery-audit",
        closed_at=utc_now_iso(),
        telegram_voter_count=2,
    ) is True

    closing_text = service._format_recent()
    assert "Telegram/БД: 2/1 (ожидаем доставку)" in closing_text

    repository.mark_poll_closed("poll-delivery-audit")
    closed_text = service._format_recent()
    assert "Telegram/БД: 2/1 (РАСХОЖДЕНИЕ)" in closed_text
    assert service._poll_answer_audit_text(
        {"status": "closed", "telegram_voter_count": 1, "answers": [{}]}
    ) == "Telegram/БД: 1/1 (OK)"
    assert service._poll_answer_audit_text(
        {"status": "closed", "telegram_voter_count": None, "answers": []}
    ) == "Telegram/БД: ?/0 (данных Telegram нет)"


def test_recent_poll_summaries_exclude_unanswered_challenge_settlement(tmp_path: Path) -> None:
    _service, repository, _telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="unanswered-challenge-audit",
        telegram_message_id=558,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-05T22:00:00+00:00",
        closes_at="2026-07-05T23:00:00+00:00",
        explanation="",
        requested_by=42,
        request_cost=10,
        request_reward=25,
    )
    assert repository.mark_poll_closing(
        "unanswered-challenge-audit",
        closed_at=utc_now_iso(),
        telegram_voter_count=0,
    ) is True
    poll = repository.get_poll("unanswered-challenge-audit")
    finalized, settlement = repository.finalize_closing_poll(
        season="main",
        poll=poll,
        now_iso=utc_now_iso(),
    )

    assert finalized is True
    assert settlement is not None
    summaries = repository.recent_poll_summaries(limit=1)
    assert summaries[0]["telegram_voter_count"] == 0
    assert summaries[0]["answers"] == []


def test_admin_errors_reports_error_events_and_failed_cron(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    now = datetime.now(timezone.utc)
    transient_at = (now - timedelta(minutes=5)).isoformat()
    repository.record_error_event(
        source="telegram",
        event="send_message_failed",
        message=(
            "Telegram sendMessage request failed after 3 attempts: "
            "HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded with url: /bot<bot_token>/sendMessage "
            "(Caused by ProxyError('Unable to connect to proxy', "
            "OSError('Tunnel connection failed: 503 Service Unavailable')))"
        ),
        created_at=transient_at,
    )
    cron_finished_at = (now - timedelta(minutes=4)).isoformat()
    repository.record_cron_run(
        (now - timedelta(minutes=5)).isoformat(),
        cron_finished_at,
        "failed",
        "tick exploded",
    )
    repository.record_error_event(
        source="cron",
        event="tick_failed",
        message="tick exploded",
        created_at=cron_finished_at,
    )
    repository.record_error_event(
        source="telegram",
        event="old_send_failed",
        message="old event kept for history",
        created_at=(now - timedelta(hours=48)).isoformat(),
    )
    repository.record_cron_run(
        (now - timedelta(hours=49, minutes=1)).isoformat(),
        (now - timedelta(hours=49)).isoformat(),
        "backup_failed",
        "old cron kept for history",
    )

    result = service.handle_update(
        {
            "update_id": 52,
            "message": {
                "message_id": 81,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_errors",
            },
        }
    )

    assert result["command"] == "/kvizi_errors"
    text = telegram.sent_messages[-1]["text"]
    assert "Ошибки Квизи:" in text
    assert (
        "Свежие за 36ч: требуют внимания=0, "
        "transient Telegram/proxy=1, failed cron=1"
    ) in text
    assert "Временные Telegram/proxy:" in text
    assert (
        "telegram/send_message_failed: "
        "временный Telegram/proxy сбой (503, proxy, after retries)"
    ) in text
    assert "HTTPSConnectionPool" not in text
    assert "/bot" not in text
    assert "Cron:" in text
    assert "failed: tick exploded" in text
    assert "Скрыто точных дублей cron/event: 1" in text
    assert "cron/tick_failed" not in text
    assert "История старше 36ч (не влияет на prod-check):" in text
    assert "telegram/old_send_failed: old event kept for history" in text
    assert "backup_failed: old cron kept for history" in text


def test_admin_errors_marks_old_entries_as_non_actionable_history(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.record_error_event(
        source="telegram",
        event="old_proxy_failure",
        message="historical only",
        created_at=(datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
    )

    text = service._format_errors()

    assert "Свежие за 36ч: актуальных ошибок нет." in text
    assert "История старше 36ч (не влияет на prod-check):" in text
    assert "telegram/old_proxy_failure: historical only" in text


def test_admin_review_reports_suspicious_questions(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=QuestionBank(
            [
                Question(
                    id="too-hard",
                    topic_key="network",
                    difficulty="hard",
                    text="Hard?",
                    options=("A", "B", "C", "D"),
                    correct_option_id=0,
                    explanation="Because.",
                    source="lab",
                ),
                Question(
                    id="too-easy",
                    topic_key="system",
                    difficulty="easy",
                    text="Easy?",
                    options=("A", "B", "C", "D"),
                    correct_option_id=0,
                    explanation="Because.",
                    source="lab",
                ),
                Question(
                    id="missing-meta",
                    topic_key="security",
                    difficulty="normal",
                    text="Missing?",
                    options=("A", "B", "C", "D"),
                    correct_option_id=0,
                    explanation="",
                    source="",
                ),
            ]
        ),
    )
    repository.create_poll(
        poll_id="hard-poll",
        telegram_message_id=101,
        question_id="too-hard",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="hard",
        opened_at="2026-07-05T20:00:00+00:00",
        closes_at="2999-01-01T00:00:00+00:00",
        explanation="",
    )
    for index, user_id in enumerate((41, 42, 43), start=1):
        repository.record_answer(
            season="main",
            poll_id="hard-poll",
            user={"id": user_id, "first_name": f"Wrong{index}"},
            option_ids=[1],
            now_iso=f"2026-07-05T20:00:{index:02d}+00:00",
        )
    repository.create_poll(
        poll_id="easy-poll",
        telegram_message_id=102,
        question_id="too-easy",
        topic_key="system",
        message_thread_id=102,
        correct_option_id=0,
        difficulty="easy",
        opened_at="2026-07-05T21:00:00+00:00",
        closes_at="2999-01-01T00:00:00+00:00",
        explanation="",
    )
    for index, user_id in enumerate((51, 52, 53, 54, 55), start=1):
        repository.record_answer(
            season="main",
            poll_id="easy-poll",
            user={"id": user_id, "first_name": f"Right{index}"},
            option_ids=[0],
            now_iso=f"2026-07-05T21:00:{index:02d}+00:00",
        )

    result = service.handle_update(
        {
            "update_id": 53,
            "message": {
                "message_id": 82,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_review",
            },
        }
    )

    assert result["command"] == "/kvizi_review"
    text = telegram.sent_messages[-1]["text"]
    assert "Ревизия вопросов:" in text
    assert "too-hard | network hard | 0/3 верно (0%), ошибок 3, задан 1 раз" in text
    assert "0% правильных при 3+ ответах" in text
    assert "too-easy | system easy | 5/5 верно (100%), ошибок 0, задан 1 раз" in text
    assert "100% правильных при 5+ ответах" in text
    assert "missing-meta | security normal | нет ответов | нет explanation; нет source" in text
    assert "Итого сигналов: 3 из 3 вопросов; со статистикой=2." in text


def test_admin_review_reports_clean_state(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    telegram = FakeTelegram()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,  # type: ignore[arg-type]
        question_bank=QuestionBank(
            [
                Question(
                    id="clean-1",
                    topic_key="network",
                    difficulty="normal",
                    text="Clean?",
                    options=("A", "B", "C", "D"),
                    correct_option_id=0,
                    explanation="Because.",
                    source="lab",
                )
            ]
        ),
    )

    result = service.handle_update(
        {
            "update_id": 54,
            "message": {
                "message_id": 83,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_review",
            },
        }
    )

    assert result["command"] == "/kvizi_review"
    text = telegram.sent_messages[-1]["text"]
    assert "Проблем не найдено по текущим порогам" in text
    assert "Статистика: questions=1, со статистикой=0." in text


def test_admin_help_lists_commands_and_cron_endpoints(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 52,
            "message": {
                "message_id": 81,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_help_admin",
            },
        }
    )

    assert result["command"] == "/kvizi_help_admin"
    text = telegram.sent_messages[-1]["text"]
    assert "Админ-пульт Квизи:" in text
    assert "Игра:" in text
    assert "Топики и настройки:" in text
    assert "Контент:" in text
    assert "Диагностика:" in text
    assert "/kvizi_help" in text
    assert "/kvizi_prod_check" in text
    assert "/kvizi_version" in text
    assert "/kvizi_status_compact" in text
    assert "/kvizi_recent" in text
    assert "/kvizi_errors" in text
    assert "/kvizi_review" in text
    assert "/kvizi_questions_status" in text
    assert "/kvizi_ai_preview" in text
    assert "/kvizi_questions_template" in text
    assert "/kvizi_upload_questions" in text
    assert "/kvizi_backups" in text
    assert "/kvizi_restore_questions" in text
    assert "POST /cron/maintenance" in text
    assert "POST /cron/backup" in text
    assert "python scripts/local_cron.py daily" in text
    assert "python scripts/local_cron.py backup" in text


def test_admin_version_reports_deploy_identity(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 521,
            "message": {
                "message_id": 811,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_version",
            },
        }
    )

    assert result["command"] == "/kvizi_version"
    text = telegram.sent_messages[-1]["text"]
    assert "Версия Квизи:" in text
    assert "app: 0.1.0" in text
    assert "git:" in text
    assert "project_root:" in text
    assert f"database: {service.settings.database_path}" in text
    assert f"questions: {service.settings.questions_path}" in text
    assert "question_count: 2" in text
    assert "season: main" in text
    assert "timezone: Europe/Moscow" in text


def test_admin_questions_status_reports_csv_coverage(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    service.settings.questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n"
        "q2,security,hard,Question?,A,B,C,D,,,2,Because,\n",
        encoding="utf-8",
    )

    result = service.handle_update(
        {
            "update_id": 54,
            "message": {
                "message_id": 83,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_questions_status",
            },
        }
    )

    assert result["command"] == "/kvizi_questions_status"
    text = telegram.sent_messages[-1]["text"]
    assert "Статус questions.csv:" in text
    assert "Questions OK: 2" in text
    assert "Difficulties: hard=1, normal=1" in text
    assert "- network: total=1 | normal=1" in text
    assert "- security: total=1 | hard=1" in text
    assert "CSV topics not bound in SQLite: security" in text


def test_admin_questions_status_reports_csv_error(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    service.settings.questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,not valid,Question?,A,B,C,D,,,1,Because,\n",
        encoding="utf-8",
    )

    result = service.handle_update(
        {
            "update_id": 55,
            "message": {
                "message_id": 84,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_questions_status",
            },
        }
    )

    assert result["command"] == "/kvizi_questions_status"
    text = telegram.sent_messages[-1]["text"]
    assert "Статус questions.csv:" in text
    assert "Questions ERROR:" in text
    assert "difficulty must be a slug" in text


def test_admin_questions_template_sends_csv_for_bound_topics(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 62,
            "message": {
                "message_id": 91,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_questions_template",
            },
        }
    )

    assert result["command"] == "/kvizi_questions_template"
    assert result["sent"] is True
    assert len(telegram.sent_documents) == 1
    document = telegram.sent_documents[0]
    assert document["chat_id"] == "-1001"
    assert document["message_thread_id"] == 101
    assert document["filename"].startswith("questions-template-")
    assert document["filename"].endswith(".csv")
    assert document["mime_type"] == "text/csv"
    assert "topics=1" in document["caption"]
    assert "difficulties=easy, normal, hard" in document["caption"]

    payload = document["content"].decode("utf-8-sig")
    rows = list(csv.DictReader(StringIO(payload)))
    assert [row["difficulty"] for row in rows] == ["easy", "normal", "hard"]
    assert {row["topic_key"] for row in rows} == {"network"}
    assert rows[0]["id"] == "network_easy_001"
    assert rows[0]["question"] == ""
    assert rows[0]["correct_option_ids"] == ""


def test_admin_questions_template_accepts_custom_difficulty(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 63,
            "message": {
                "message_id": 92,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_questions_template ccna",
            },
        }
    )

    assert result["sent"] is True
    rows = list(
        csv.DictReader(
            StringIO(telegram.sent_documents[0]["content"].decode("utf-8-sig"))
        )
    )
    assert len(rows) == 1
    assert rows[0]["topic_key"] == "network"
    assert rows[0]["difficulty"] == "ccna"
    assert rows[0]["id"] == "network_ccna_001"


def test_admin_questions_template_rejects_invalid_difficulty(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 64,
            "message": {
                "message_id": 93,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_questions_template bad!slug",
            },
        }
    )

    assert result["sent"] is False
    assert telegram.sent_documents == []
    assert "Некорректная сложность" in telegram.sent_messages[-1]["text"]


def test_admin_upload_questions_replaces_csv_after_validation_and_backup(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    original_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "old,network,normal,Old question?,A,B,C,D,,,1,Because,\n"
    )
    uploaded_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "new1,network,easy,Question 1?,A,B,C,D,,,1,Because,\n"
        "new2,network,normal,Question 2?,A,B,C,D,,,2,Because,\n"
        "new3,security,hard,Question 3?,A,B,C,D,,,3,Because,\n"
    )
    service.settings.questions_path.write_text(original_content, encoding="utf-8")
    telegram.downloaded_files["file-ok"] = uploaded_content.encode("utf-8")

    result = service.handle_update(
        {
            "update_id": 56,
            "message": {
                "message_id": 85,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "caption": "/kvizi_upload_questions",
                "document": {
                    "file_id": "file-ok",
                    "file_name": "questions.csv",
                    "file_size": len(uploaded_content.encode("utf-8")),
                },
            },
        }
    )

    assert result["command"] == "/kvizi_upload_questions"
    assert result["uploaded"] is True
    assert service.question_bank.count() == 3
    assert "new1" in service.settings.questions_path.read_text(encoding="utf-8")
    assert "old" not in service.settings.questions_path.read_text(encoding="utf-8")
    backups = list((tmp_path / "backups").glob("questions-*.csv"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original_content

    text = telegram.sent_messages[-1]["text"]
    assert "questions.csv обновлён: 3 вопросов." in text
    assert "Backup: backups/questions-" in text
    assert "Questions OK: 3" in text
    assert "CSV topics not bound in SQLite: security" in text


def test_admin_upload_questions_check_validates_without_replacing_csv(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    original_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "old,network,normal,Old question?,A,B,C,D,,,1,Because,\n"
    )
    uploaded_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "new1,network,easy,Question 1?,A,B,C,D,,,1,Because,\n"
        "new2,security,hard,Question 2?,A,B,C,D,,,2,Because,\n"
    )
    service.settings.questions_path.write_text(original_content, encoding="utf-8")
    before_count = service.question_bank.count()
    telegram.downloaded_files["file-check"] = uploaded_content.encode("utf-8")

    result = service.handle_update(
        {
            "update_id": 58,
            "message": {
                "message_id": 87,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "caption": "/kvizi_upload_questions --check",
                "document": {
                    "file_id": "file-check",
                    "file_name": "questions.csv",
                    "file_size": len(uploaded_content.encode("utf-8")),
                },
            },
        }
    )

    assert result["command"] == "/kvizi_upload_questions"
    assert result["uploaded"] is True
    assert result["check_only"] is True
    assert service.question_bank.count() == before_count
    assert service.settings.questions_path.read_text(encoding="utf-8") == original_content
    assert not (tmp_path / "backups").exists()

    text = telegram.sent_messages[-1]["text"]
    assert "Проверка questions.csv пройдена: 2 вопросов." in text
    assert "Файл не заменён. Для применения отправь без --check." in text
    assert "Questions OK: 2" in text
    assert "Backup:" not in text


def test_admin_upload_questions_rejects_invalid_csv_without_replacing_current_file(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    original_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "old,network,normal,Old question?,A,B,C,D,,,1,Because,\n"
    )
    uploaded_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "bad,network,not valid,Question?,A,B,C,D,,,1,Because,\n"
    )
    service.settings.questions_path.write_text(original_content, encoding="utf-8")
    telegram.downloaded_files["file-bad"] = uploaded_content.encode("utf-8")

    result = service.handle_update(
        {
            "update_id": 57,
            "message": {
                "message_id": 86,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "caption": "/kvizi_upload_questions",
                "document": {
                    "file_id": "file-bad",
                    "file_name": "questions.csv",
                    "file_size": len(uploaded_content.encode("utf-8")),
                },
            },
        }
    )

    assert result["command"] == "/kvizi_upload_questions"
    assert result["uploaded"] is False
    assert service.settings.questions_path.read_text(encoding="utf-8") == original_content
    assert not (tmp_path / "backups").exists()
    text = telegram.sent_messages[-1]["text"]
    assert "Questions ERROR: новый CSV не принят." in text
    assert "Текущий questions.csv не заменён." in text
    assert "difficulty must be a slug" in text


def test_admin_backups_lists_latest_question_backups(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "questions-20260706T010000000000Z.csv").write_text("old", encoding="utf-8")
    (backup_dir / "questions-20260706T020000000000Z.csv").write_text("new", encoding="utf-8")

    result = service.handle_update(
        {
            "update_id": 59,
            "message": {
                "message_id": 88,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_backups",
            },
        }
    )

    assert result["command"] == "/kvizi_backups"
    text = telegram.sent_messages[-1]["text"]
    assert "Backups questions.csv:" in text
    assert "1. questions-20260706T020000000000Z.csv" in text
    assert "2. questions-20260706T010000000000Z.csv" in text
    assert "Восстановить: /kvizi_restore_questions <номер>" in text


def test_admin_restore_questions_restores_backup_and_backs_up_current_csv(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    current_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "current,network,normal,Current question?,A,B,C,D,,,1,Because,\n"
    )
    backup_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "restore1,network,easy,Restore question 1?,A,B,C,D,,,1,Because,\n"
        "restore2,security,hard,Restore question 2?,A,B,C,D,,,2,Because,\n"
    )
    service.settings.questions_path.write_text(current_content, encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    selected_backup = backup_dir / "questions-20260706T020000000000Z.csv"
    selected_backup.write_text(backup_content, encoding="utf-8")

    result = service.handle_update(
        {
            "update_id": 60,
            "message": {
                "message_id": 89,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_restore_questions 1",
            },
        }
    )

    assert result["command"] == "/kvizi_restore_questions"
    assert result["restored"] is True
    assert service.question_bank.count() == 2
    assert "restore1" in service.settings.questions_path.read_text(encoding="utf-8")
    current_backups = [
        path
        for path in backup_dir.glob("questions-*.csv")
        if path.name != selected_backup.name
    ]
    assert len(current_backups) == 1
    assert current_backups[0].read_text(encoding="utf-8") == current_content

    text = telegram.sent_messages[-1]["text"]
    assert "questions.csv восстановлен из backup #1: questions-20260706T020000000000Z.csv." in text
    assert "Backup текущего файла: backups/questions-" in text
    assert "Questions OK: 2" in text


def test_admin_restore_questions_rejects_invalid_backup_without_replacing_current_csv(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)
    current_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "current,network,normal,Current question?,A,B,C,D,,,1,Because,\n"
    )
    bad_backup_content = (
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "bad,network,not valid,Bad question?,A,B,C,D,,,1,Because,\n"
    )
    service.settings.questions_path.write_text(current_content, encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "questions-20260706T020000000000Z.csv").write_text(
        bad_backup_content,
        encoding="utf-8",
    )

    result = service.handle_update(
        {
            "update_id": 61,
            "message": {
                "message_id": 90,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_restore_questions 1",
            },
        }
    )

    assert result["command"] == "/kvizi_restore_questions"
    assert result["restored"] is False
    assert service.settings.questions_path.read_text(encoding="utf-8") == current_content
    assert len(list(backup_dir.glob("questions-*.csv"))) == 1
    text = telegram.sent_messages[-1]["text"]
    assert "Backup #1 повреждён, восстановление отменено." in text
    assert "difficulty must be a slug" in text


def test_admin_help_requires_admin(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    result = service.handle_update(
        {
            "update_id": 53,
            "message": {
                "message_id": 82,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 42, "first_name": "User"},
                "text": "/kvizi_help_admin",
            },
        }
    )

    assert result["command"] == "/kvizi_help_admin"
    assert result["admin"] is False
    assert telegram.sent_messages[-1]["text"] == "Эта ручка только для администраторов Квизи."


def test_admin_status_compact_reports_counts_and_maintenance_hint(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.create_poll(
        poll_id="challenge-poll",
        telegram_message_id=555,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="2026-07-05T20:00:00+00:00",
        closes_at="2999-01-01T00:00:00+00:00",
        explanation="",
        requested_by=7,
        request_cost=10,
        request_reward=25,
    )
    repository.create_poll(
        poll_id="expired-poll",
        telegram_message_id=556,
        question_id="q2",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="hard",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )
    repository.record_cron_run(
        "2026-07-05T20:00:00+00:00",
        "2026-07-05T20:00:01+00:00",
        "maintenance_ok",
        "Closed expired polls: 0",
    )

    result = service.handle_update(
        {
            "update_id": 51,
            "message": {
                "message_id": 80,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_status_compact",
            },
        }
    )

    assert result["command"] == "/kvizi_status_compact"
    text = telegram.sent_messages[-1]["text"]
    assert "Статус Квизи compact:" in text
    assert "Вопросы: 2" in text
    assert "Топики: active=1/1" in text
    assert "Poll: active=1, expired=1, challenge=1" in text
    assert "Занятые топики: network=1" in text
    assert "Maintenance: есть просроченные poll" in text
    assert "Последний cron: maintenance_ok" in text
    assert "01.01.2999 03:00:00 MSK" in text
    assert "05.07.2026 23:00:01 MSK" in text
    assert "poll=challenge-poll" not in text


def test_admin_export_sends_json_document(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.upsert_user({"id": 7, "username": "adminuser", "first_name": "Admin"})

    result = service.handle_update(
        {
            "update_id": 70,
            "message": {
                "message_id": 80,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_export",
            },
        }
    )

    assert result["command"] == "/kvizi_export"
    assert len(telegram.sent_documents) == 1
    document = telegram.sent_documents[0]
    assert document["chat_id"] == "-1001"
    assert document["message_thread_id"] == 101
    assert document["filename"].startswith("kvizi-state-")
    assert document["filename"].endswith(".json")
    assert "users=1" in document["caption"]

    payload = json.loads(document["content"].decode("utf-8"))
    assert payload["topics"][0]["topic_key"] == "network"
    assert payload["users"][0]["username"] == "adminuser"
    assert "processed_updates" not in payload


def test_admin_export_full_includes_processed_updates(tmp_path: Path) -> None:
    service, _repository, telegram = make_service(tmp_path)

    service.handle_update(
        {
            "update_id": 71,
            "message": {
                "message_id": 80,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_export --full",
            },
        }
    )

    payload = json.loads(telegram.sent_documents[0]["content"].decode("utf-8"))
    assert "processed_updates" in payload


def test_admin_daily_posts_summary_to_current_topic(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    _seed_today_answer(repository, user_id=42, username="ada", points_difficulty="normal")

    result = service.handle_update(
        {
            "update_id": 80,
            "message": {
                "message_id": 81,
                "message_thread_id": 101,
                "chat": {"id": "-1001"},
                "from": {"id": 7, "first_name": "Admin"},
                "text": "/kvizi_daily",
            },
        }
    )

    assert result["command"] == "/kvizi_daily"
    assert result["posted"] is True
    message = telegram.sent_messages[-1]
    assert message["message_thread_id"] == 101
    assert _daily_title_matches(message["text"])
    assert "Вопросы: 1" in message["text"]
    assert "Ответы: 1 от 1 участников" in message["text"]
    assert "@ada" in message["text"]


def test_cron_daily_posts_once_and_skips_duplicate(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.set_bot_setting("announce_thread_id", "999")
    _seed_today_answer(repository, user_id=42, username="ada", points_difficulty="normal")
    telegram = FakeTelegram()
    app = create_app(settings=settings, repository=repository, telegram=telegram)  # type: ignore[arg-type]
    client = app.test_client()

    first = client.post("/cron/daily", headers={"X-Kvizi-Cron-Secret": "cron-secret"})
    second = client.post("/cron/daily", headers={"X-Kvizi-Cron-Secret": "cron-secret"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["posted"] is True
    assert second.get_json()["posted"] is False
    assert len(telegram.sent_messages) == 1
    assert telegram.sent_messages[0]["message_thread_id"] == 999
    assert _daily_title_matches(telegram.sent_messages[0]["text"])


def test_cron_backup_requires_secret_and_sends_sqlite_snapshot_to_admin(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.upsert_user({"id": 7, "username": "adminuser", "first_name": "Admin"})
    telegram = FakeTelegram()
    app = create_app(settings=settings, repository=repository, telegram=telegram)  # type: ignore[arg-type]
    client = app.test_client()

    forbidden = client.post("/cron/backup")
    ok = client.post("/cron/backup", headers={"X-Kvizi-Cron-Secret": "cron-secret"})

    assert forbidden.status_code == 403
    assert ok.status_code == 200
    payload = ok.get_json()
    assert payload["ok"] is True
    assert payload["complete"] is True
    assert payload["sent"] == 1
    assert payload["failed"] == 0
    assert payload["admin_ids"] == [7]
    assert payload["filename"].startswith("kvizi-backup-")
    assert payload["filename"].endswith(".sqlite3")

    assert len(telegram.sent_documents) == 1
    document = telegram.sent_documents[0]
    assert document["chat_id"] == "7"
    assert "message_thread_id" not in document
    assert document["filename"] == payload["filename"]
    assert "Backup Квизи" in document["caption"]
    assert document["mime_type"] == "application/vnd.sqlite3"
    assert document["content"].startswith(b"SQLite format 3\x00")
    backup_path = tmp_path / "received-backup.sqlite3"
    backup_path.write_bytes(document["content"])
    with sqlite3.connect(backup_path) as backup_connection:
        backup_user = backup_connection.execute(
            "SELECT username FROM users WHERE user_id = 7"
        ).fetchone()
        integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
    assert backup_user == ("adminuser",)
    assert integrity == ("ok",)
    assert repository.latest_cron_run()["status"] == "backup_sent"


def test_cron_backup_reports_partial_admin_delivery_failure(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), admin_ids={7, 8})
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    telegram = FailDocumentForChatTelegram("8")
    app = create_app(settings=settings, repository=repository, telegram=telegram)  # type: ignore[arg-type]
    client = app.test_client()

    response = client.post("/cron/backup", headers={"X-Kvizi-Cron-Secret": "cron-secret"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["complete"] is False
    assert payload["sent"] == 1
    assert payload["failed"] == 1
    assert payload["admin_ids"] == [7, 8]
    assert "8: bot can't initiate conversation" in payload["errors"]
    assert [document["chat_id"] for document in telegram.sent_documents] == ["7"]
    assert repository.latest_cron_run()["status"] == "backup_partial"


def test_flask_cron_requires_secret_and_posts_question(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n",
        encoding="utf-8",
    )
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    telegram = FakeTelegram()
    app = create_app(settings=settings, repository=repository, telegram=telegram)  # type: ignore[arg-type]

    client = app.test_client()
    forbidden = client.post("/cron/tick")
    ok = client.post("/cron/tick", headers={"X-Kvizi-Cron-Secret": "cron-secret"})

    assert forbidden.status_code == 403
    assert ok.status_code == 200
    assert ok.get_json()["posted"] is True
    assert len(telegram.sent_polls) == 1


def test_webhook_returns_503_and_allows_retry_when_telegram_reply_fails(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    telegram = FailOnceSendMessageTelegram()
    app = create_app(settings=settings, repository=repository, telegram=telegram)  # type: ignore[arg-type]
    client = app.test_client()

    update = {
        "update_id": 999,
        "message": {
            "chat": {"id": "-1001", "type": "supergroup"},
            "from": {"id": 42, "first_name": "Ada"},
            "message_thread_id": 101,
            "text": "/me",
        },
    }

    response = client.post(
        f"/telegram/{settings.webhook_secret}",
        headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret},
        json=update,
    )
    payload = response.get_json()
    assert response.status_code == 503
    assert payload["ok"] is False
    assert "proxy 503" in payload["telegram_error"]
    errors = repository.recent_error_events()
    assert errors[-1]["source"] == "telegram"
    assert errors[-1]["event"] == "webhook_update_failed"
    assert "proxy 503" in errors[-1]["message"]

    retried = client.post(
        f"/telegram/{settings.webhook_secret}",
        headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret},
        json=update,
    )

    assert retried.status_code == 200
    assert retried.get_json()["command"] == "/me"
    assert len(telegram.sent_messages) == 1


def test_update_claim_is_released_after_unexpected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _repository, _telegram = make_service(tmp_path)
    calls = 0

    def fail_once(_message: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic handler failure")
        return {"ok": True, "retried": True}

    monkeypatch.setattr(service, "_handle_message", fail_once)
    update = {"update_id": 1000, "message": {}}

    with pytest.raises(RuntimeError, match="synthetic handler failure"):
        service.handle_update(update)

    assert service.handle_update(update) == {"ok": True, "retried": True}


def test_concurrent_question_posts_use_durable_global_claim(tmp_path: Path) -> None:
    service, repository, _telegram = make_service(tmp_path)
    repository.bind_topic("hardware", 202, 1)
    service.question_bank = QuestionBank(
        [
            Question(
                id="network-question",
                topic_key="network",
                difficulty="normal",
                text="Network question?",
                options=("A", "B"),
                correct_option_id=0,
                explanation="Network explanation.",
                source="",
            ),
            Question(
                id="hardware-question",
                topic_key="hardware",
                difficulty="normal",
                text="Hardware question?",
                options=("A", "B"),
                correct_option_id=0,
                explanation="Hardware explanation.",
                source="",
            ),
        ]
    )
    telegram = BlockingSendPollTelegram()
    service.telegram = telegram  # type: ignore[assignment]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            service.post_question,
            topic_key="network",
            skip_busy_topics=True,
        )
        assert telegram.entered.wait(timeout=2)
        second_future = pool.submit(
            service.post_question,
            topic_key="hardware",
            skip_busy_topics=True,
        )
        try:
            second = second_future.result(timeout=2)
        finally:
            telegram.release.set()
        first = first_future.result(timeout=2)

    assert first.posted is True
    assert second.posted is False
    assert "Защита от дубля публикации активна до" in second.message
    assert "неоднозначным сбоем Telegram" in second.message
    assert len(telegram.sent_polls) == 1
    assert len(repository.active_polls(utc_now_iso())) == 1


def test_concurrent_daily_posts_use_durable_date_claim(tmp_path: Path) -> None:
    service, _repository, _telegram = make_service(tmp_path)
    telegram = BlockingSendMessageTelegram()
    service.telegram = telegram  # type: ignore[assignment]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(service.post_daily_summary)
        assert telegram.entered.wait(timeout=2)
        second_future = pool.submit(service.post_daily_summary)
        try:
            second = second_future.result(timeout=2)
        finally:
            telegram.release.set()
        first = first_future.result(timeout=2)

    assert first.posted is True
    assert second.posted is False
    assert len(telegram.sent_messages) == 1


def test_question_claim_survives_database_failure_after_telegram_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, telegram = make_service(tmp_path)
    original_create_poll = repository.create_poll

    def fail_create_poll(**_kwargs: Any) -> None:
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(repository, "create_poll", fail_create_poll)
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        service.post_question(topic_key="network", skip_busy_topics=True)

    monkeypatch.setattr(repository, "create_poll", original_create_poll)
    retry = service.post_question(topic_key="network", skip_busy_topics=True)

    assert retry.posted is False
    assert len(telegram.sent_polls) == 1


def test_health_returns_503_without_exposing_database_path_on_load_error(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.questions_path.write_text("broken,csv\n", encoding="utf-8")
    app = create_app(settings=settings, telegram=FakeTelegram())  # type: ignore[arg-type]

    response = app.test_client().get("/health")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["ok"] is False
    assert payload["questions_loaded"] is False
    assert "database" not in payload
    assert str(settings.questions_path) not in response.get_data(as_text=True)


def test_empty_secrets_cannot_authorize_webhook_or_cron(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), webhook_secret="", cron_secret="")
    app = create_app(settings=settings, telegram=FakeTelegram())  # type: ignore[arg-type]
    client = app.test_client()

    cron = client.post("/cron/tick", headers={"X-Kvizi-Cron-Secret": ""})
    webhook = client.post(
        "/telegram/anything",
        headers={"X-Telegram-Bot-Api-Secret-Token": ""},
        json={},
    )

    assert cron.status_code == 403
    assert webhook.status_code == 404


def test_create_app_wires_groq_provider_only_when_ai_copy_is_configured(tmp_path: Path) -> None:
    settings = replace(
        make_settings(tmp_path),
        ai_enabled=True,
        ai_copy_enabled=True,
        groq_api_key="groq-secret",
    )
    settings.questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n",
        encoding="utf-8",
    )

    app = create_app(settings=settings, telegram=FakeTelegram())  # type: ignore[arg-type]
    service = app.config["KVIZI_SERVICE"]
    health = app.test_client().get("/health").get_json()

    assert service.ai_provider.name == "groq"
    assert service.ai_provider.model == "qwen/qwen3.6-27b"
    assert health["ai_copy_enabled"] is True
    assert health["ai_provider_configured"] is True


def test_flask_cron_skips_when_topic_has_active_poll(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n",
        encoding="utf-8",
    )
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    telegram = FakeTelegram()
    app = create_app(settings=settings, repository=repository, telegram=telegram)  # type: ignore[arg-type]

    client = app.test_client()
    first = client.post("/cron/tick", headers={"X-Kvizi-Cron-Secret": "cron-secret"})
    second = client.post("/cron/tick", headers={"X-Kvizi-Cron-Secret": "cron-secret"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["posted"] is True
    assert second.get_json()["posted"] is False
    assert len(telegram.sent_polls) == 1


def test_cron_maintenance_requires_secret_and_closes_expired_poll(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    telegram = FakeTelegram()
    app = create_app(settings=settings, repository=repository, telegram=telegram)  # type: ignore[arg-type]
    repository.create_poll(
        poll_id="expired-poll",
        telegram_message_id=321,
        question_id="q1",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty="normal",
        opened_at="1999-01-01T00:00:00+00:00",
        closes_at="2000-01-01T00:00:00+00:00",
        explanation="",
    )

    client = app.test_client()
    forbidden = client.post("/cron/maintenance")
    ok = client.post("/cron/maintenance", headers={"X-Kvizi-Cron-Secret": "cron-secret"})

    assert forbidden.status_code == 403
    assert ok.status_code == 200
    assert ok.get_json()["closed"] == 1
    assert telegram.sent_polls == []
    assert telegram.stopped_polls == [{"chat_id": "-1001", "message_id": 321}]
    assert repository.get_poll("expired-poll")["status"] == "closing"

    with repository.connect() as connection:
        connection.execute(
            "UPDATE polls SET closed_at = ? WHERE poll_id = ?",
            ("2000-01-01T00:00:00+00:00", "expired-poll"),
        )
    finalized = client.post(
        "/cron/maintenance",
        headers={"X-Kvizi-Cron-Secret": "cron-secret"},
    )

    assert finalized.status_code == 200
    assert finalized.get_json()["closed"] == 1
    assert repository.get_poll("expired-poll")["status"] == "closed"
    assert repository.latest_cron_run()["status"] == "maintenance_closed"


def _seed_poll(repository: KviziRepository, difficulty: str = "normal") -> str:
    repository.create_poll(
        poll_id="seed-poll",
        telegram_message_id=999,
        question_id="seed",
        topic_key="seed",
        message_thread_id=999,
        correct_option_id=0,
        difficulty=difficulty,
        opened_at="2026-07-05T20:00:00+00:00",
        closes_at="2026-07-05T21:00:00+00:00",
        explanation="",
    )
    return "seed-poll"


def _seed_topic_answer(
    repository: KviziRepository,
    *,
    poll_id: str,
    topic_key: str,
    user_id: int,
    username: str,
    difficulty: str,
    season: str = "main",
    option_ids: list[int] | None = None,
) -> str:
    repository.create_poll(
        poll_id=poll_id,
        telegram_message_id=1000 + user_id,
        question_id=f"question-{poll_id}",
        topic_key=topic_key,
        message_thread_id=101,
        correct_option_id=0,
        difficulty=difficulty,
        opened_at="2026-07-05T20:00:00+00:00",
        closes_at="2026-07-05T21:00:00+00:00",
        explanation="",
    )
    repository.record_answer(
        season=season,
        poll_id=poll_id,
        user={"id": user_id, "username": username, "first_name": username},
        option_ids=[0] if option_ids is None else option_ids,
        now_iso="2026-07-05T20:01:00+00:00",
    )
    return poll_id


def _seed_today_answer(
    repository: KviziRepository,
    *,
    user_id: int,
    username: str,
    points_difficulty: str,
) -> str:
    now = datetime.now(timezone.utc)
    poll_id = f"today-{user_id}-{points_difficulty}"
    repository.create_poll(
        poll_id=poll_id,
        telegram_message_id=1000 + user_id,
        question_id=f"question-{poll_id}",
        topic_key="network",
        message_thread_id=101,
        correct_option_id=0,
        difficulty=points_difficulty,
        opened_at=now.isoformat(),
        closes_at=(now + timedelta(hours=1)).isoformat(),
        explanation="",
    )
    repository.record_answer(
        season="main",
        poll_id=poll_id,
        user={"id": user_id, "username": username, "first_name": username},
        option_ids=[0],
        now_iso=now.isoformat(),
    )
    return poll_id
