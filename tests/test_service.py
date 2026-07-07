from __future__ import annotations

import csv
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from kvizi import copy
from kvizi.config import Settings
from kvizi.database import KviziRepository
from kvizi.questions import Question, QuestionBank
from kvizi.service import KviziService
from kvizi.telegram import TelegramApiError
from kvizi.web import create_app


class FakeTelegram:
    def __init__(self) -> None:
        self.sent_polls: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
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


class FailOnceSendMessageTelegram(FakeTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def send_message(self, **payload: Any) -> dict[str, Any]:
        if not self.failed_once:
            self.failed_once = True
            raise TelegramApiError("Telegram sendMessage request failed after 3 attempts: proxy 503")
        return super().send_message(**payload)


class FailDocumentForChatTelegram(FakeTelegram):
    def __init__(self, failed_chat_id: str) -> None:
        super().__init__()
        self.failed_chat_id = failed_chat_id

    def send_document(self, **payload: Any) -> dict[str, Any]:
        if str(payload["chat_id"]) == self.failed_chat_id:
            raise TelegramApiError("bot can't initiate conversation")
        return super().send_document(**payload)


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
                "message": {"poll": {"id": "poll-1"}},
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
                "message": {"poll": {"id": "poll-1"}},
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
    assert "05.07.2026 23:00:00 MSK | network normal | active" in text
    assert "ответов 2, верно/ошибки 1/1" in text
    assert "@ada" in text
    assert "Bob" in text


def test_admin_errors_reports_error_events_and_failed_cron(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.record_error_event(
        source="telegram",
        event="send_message_failed",
        message="proxy 503",
        created_at="2026-07-05T20:00:00+00:00",
    )
    repository.record_cron_run(
        "2026-07-05T20:10:00+00:00",
        "2026-07-05T20:10:01+00:00",
        "backup_failed",
        "bot can't initiate conversation",
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
    assert "Последние ошибки Квизи:" in text
    assert "События:" in text
    assert "05.07.2026 23:00:00 MSK | telegram/send_message_failed: proxy 503" in text
    assert "Cron:" in text
    assert "05.07.2026 23:10:01 MSK | backup_failed: bot can't initiate conversation" in text


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
    assert "/kvizi_status_compact" in text
    assert "/kvizi_recent" in text
    assert "/kvizi_errors" in text
    assert "/kvizi_review" in text
    assert "/kvizi_questions_status" in text
    assert "/kvizi_questions_template" in text
    assert "/kvizi_upload_questions" in text
    assert "/kvizi_backups" in text
    assert "/kvizi_restore_questions" in text
    assert "POST /cron/maintenance" in text
    assert "POST /cron/backup" in text
    assert "python scripts/local_cron.py daily" in text
    assert "python scripts/local_cron.py backup" in text


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


def test_cron_backup_requires_secret_and_sends_json_export_to_admin(tmp_path: Path) -> None:
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
    assert payload["filename"].endswith(".json")

    assert len(telegram.sent_documents) == 1
    document = telegram.sent_documents[0]
    assert document["chat_id"] == "7"
    assert "message_thread_id" not in document
    assert document["filename"] == payload["filename"]
    assert "Backup Квизи" in document["caption"]
    export_payload = json.loads(document["content"].decode("utf-8"))
    assert export_payload["users"][0]["username"] == "adminuser"
    assert "processed_updates" not in export_payload
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
