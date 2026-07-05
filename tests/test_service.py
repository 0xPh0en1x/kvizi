from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kvizi.config import Settings
from kvizi.database import KviziRepository
from kvizi.questions import Question, QuestionBank
from kvizi.service import KviziService
from kvizi.web import create_app


class FakeTelegram:
    def __init__(self) -> None:
        self.sent_polls: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_documents: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.stopped_polls: list[dict[str, Any]] = []

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


def test_post_question_sends_announcement_with_private_group_link(tmp_path: Path) -> None:
    service, repository, telegram = make_service(tmp_path)
    repository.set_bot_setting("announce_thread_id", "999")

    posted = service.post_question(difficulty="normal")

    assert posted.posted is True
    assert posted.question_link == "https://t.me/c/1/1"
    assert telegram.sent_messages[-1] == {
        "chat_id": "-1001",
        "message_thread_id": 999,
        "text": (
            "Квизи выкатывает вопрос в сектор network! Сложность normal, база 10.\n"
            "https://t.me/c/1/1"
        ),
        "disable_notification": True,
    }


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
    assert telegram.sent_polls[-1]["question"] == "Квизи спрашивает: Which record maps a name to IPv4?"

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
    assert "Анонс-топик: 999" in text
    assert "Последний cron: posted" in text


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
