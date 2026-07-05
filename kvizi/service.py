from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from kvizi import copy
from kvizi.config import Settings
from kvizi.database import KviziRepository, utc_now_iso
from kvizi.export_state import export_state
from kvizi.questions import Question, QuestionBank, load_questions
from kvizi.routing import TopicRoute, TopicRouter
from kvizi.scoring import challenge_cost, challenge_reward
from kvizi.telegram import TelegramApiError, TelegramClient


@dataclass(frozen=True)
class PostQuestionResult:
    posted: bool
    message: str
    topic_key: str | None = None
    question_id: str | None = None
    poll_id: str | None = None
    question_link: str | None = None


class KviziService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: KviziRepository,
        telegram: TelegramClient,
        question_bank: QuestionBank | None = None,
        router: TopicRouter | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.telegram = telegram
        self.question_bank = question_bank or QuestionBank([])
        self.router = router or TopicRouter(rng)
        self.rng = rng or random.Random()

    def reload_questions(self) -> int:
        self.question_bank = load_questions(self.settings.questions_path)
        return self.question_bank.count()

    def close_expired_polls(self) -> int:
        now_iso = utc_now_iso()
        expired = self.repository.expired_active_polls(now_iso)
        for poll in expired:
            self.repository.settle_unanswered_challenge(
                season=self.settings.season_name,
                poll=poll,
                now_iso=now_iso,
            )
            try:
                self.telegram.stop_poll(
                    chat_id=self.settings.telegram_chat_id,
                    message_id=int(poll["telegram_message_id"]),
                )
            except TelegramApiError:
                pass
            self.repository.mark_poll_closed(str(poll["poll_id"]))
        return len(expired)

    def post_question(
        self,
        topic_key: str | None = None,
        difficulty: str | None = None,
        requested_by: int | None = None,
        request_cost: int = 0,
        request_reward: int = 0,
        skip_busy_topics: bool = False,
    ) -> PostQuestionResult:
        self.close_expired_polls()
        if self.question_bank.count() == 0:
            return PostQuestionResult(False, copy.no_questions_text())

        busy_topic_keys = self.repository.active_poll_topic_keys(utc_now_iso()) if skip_busy_topics else set()
        route = self._select_route(topic_key, excluded_topic_keys=busy_topic_keys)
        if route is None:
            if skip_busy_topics and busy_topic_keys:
                return PostQuestionResult(False, "Все подходящие топики заняты активными вопросами.")
            return PostQuestionResult(False, "Нет активных топиков с вопросами.")

        question = self._select_question(route.topic_key, difficulty)
        if question is None:
            if difficulty:
                return PostQuestionResult(False, f"В теме {route.topic_key} нет вопросов сложности {difficulty}.")
            return PostQuestionResult(False, f"В теме {route.topic_key} нет вопросов.")

        sent = self.telegram.send_poll(
            chat_id=self.settings.telegram_chat_id,
            question=self._poll_title(question),
            options=list(question.options),
            correct_option_id=question.correct_option_id,
            explanation=question.explanation,
            message_thread_id=route.message_thread_id,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Риск x2", "callback_data": "bet:2"},
                        {"text": "Риск x3", "callback_data": "bet:3"},
                    ]
                ]
            },
        )

        result = sent["result"]
        poll = result["poll"]
        message_id = int(result["message_id"])
        opened_at = datetime.now(timezone.utc)
        closes_at = opened_at + timedelta(seconds=self.settings.open_seconds)
        self.repository.create_poll(
            poll_id=str(poll["id"]),
            telegram_message_id=message_id,
            question_id=question.id,
            topic_key=route.topic_key,
            message_thread_id=route.message_thread_id,
            correct_option_id=question.correct_option_id,
            difficulty=question.difficulty,
            opened_at=opened_at.isoformat(),
            closes_at=closes_at.isoformat(),
            explanation=question.explanation,
            requested_by=requested_by,
            request_cost=request_cost,
            request_reward=request_reward,
        )
        self.repository.mark_question_asked(route.topic_key, question.id)
        question_link = self._message_link(message_id)
        self._announce_question(route, question, question_link)

        return PostQuestionResult(
            True,
            copy.question_intro(route.topic_key, question.difficulty),
            topic_key=route.topic_key,
            question_id=question.id,
            poll_id=str(poll["id"]),
            question_link=question_link,
        )

    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        update_id = update.get("update_id")
        if not self.repository.try_claim_update(int(update_id) if update_id is not None else None):
            return {"ok": True, "duplicate": True}

        if "poll_answer" in update:
            return self._handle_poll_answer(update["poll_answer"])
        if "callback_query" in update:
            return self._handle_callback_query(update["callback_query"])
        if "message" in update:
            return self._handle_message(update["message"])
        return {"ok": True, "ignored": True}

    def _select_route(
        self,
        topic_key: str | None = None,
        excluded_topic_keys: set[str] | None = None,
    ) -> TopicRoute | None:
        excluded_topic_keys = excluded_topic_keys or set()
        topic_rows = self.repository.active_topics()
        question_topics = self.question_bank.topics()
        routes = [
            TopicRoute(
                topic_key=str(row["topic_key"]),
                message_thread_id=int(row["message_thread_id"]),
                weight=int(row["weight"]),
                title=str(row["title"] or ""),
            )
            for row in topic_rows
            if str(row["topic_key"]) in question_topics
            and str(row["topic_key"]) not in excluded_topic_keys
        ]

        if topic_key:
            normalized = topic_key.strip().lower()
            return next((route for route in routes if route.topic_key == normalized), None)

        return self.router.choose(routes, self.repository.get_last_topic_key())

    def _select_question(self, topic_key: str, difficulty: str | None = None) -> Question | None:
        questions = self.question_bank.by_topic(topic_key)
        if difficulty:
            normalized = difficulty.strip().lower()
            questions = [question for question in questions if question.difficulty == normalized]
        if not questions:
            return None

        asked = self.repository.asked_question_ids(topic_key)
        available = [question for question in questions if question.id not in asked]
        if not available:
            self.repository.reset_question_history(topic_key)
            available = questions

        return self.rng.choice(available)

    def _handle_poll_answer(self, poll_answer: dict[str, Any]) -> dict[str, Any]:
        user = poll_answer.get("user") or {}
        poll_id = str(poll_answer.get("poll_id") or "")
        option_ids = [int(item) for item in poll_answer.get("option_ids", [])]
        result = self.repository.record_answer(
            season=self.settings.season_name,
            poll_id=poll_id,
            user=user,
            option_ids=option_ids,
            now_iso=utc_now_iso(),
        )

        if result.recorded and (result.stake > 1 or result.streak_bonus > 0 or result.is_challenge):
            poll = self.repository.get_poll(poll_id)
            if poll is not None:
                self.telegram.send_message(
                    chat_id=self.settings.telegram_chat_id,
                    message_thread_id=int(poll["message_thread_id"]),
                    text=self._score_event_text(user, result),
                    disable_notification=True,
                )

        return {
            "ok": True,
            "recorded": result.recorded,
            "delta": result.delta,
            "points": result.points,
            "reason": result.reason,
        }

    def _handle_callback_query(self, query: dict[str, Any]) -> dict[str, Any]:
        data = str(query.get("data") or "")
        user = query.get("from") or {}
        self.repository.upsert_user(user)

        if not data.startswith("bet:"):
            self.telegram.answer_callback_query(
                callback_query_id=str(query["id"]),
                text="Квизи не понял эту кнопку.",
                show_alert=False,
            )
            return {"ok": True, "ignored": True}

        try:
            stake = int(data.split(":", 1)[1])
        except ValueError:
            stake = 1

        message = query.get("message") or {}
        poll_id = str((message.get("poll") or {}).get("id") or "")
        if not poll_id:
            self.telegram.answer_callback_query(
                callback_query_id=str(query["id"]),
                text=copy.bet_rejected("опрос не найден"),
                show_alert=True,
            )
            return {"ok": True, "bet": False}

        ok, reason = self.repository.record_bet(
            poll_id=poll_id,
            user_id=int(user["id"]),
            stake=stake,
            now_iso=utc_now_iso(),
        )
        self.telegram.answer_callback_query(
            callback_query_id=str(query["id"]),
            text=copy.bet_accepted(stake) if ok else copy.bet_rejected(reason),
            show_alert=not ok,
        )
        return {"ok": True, "bet": ok, "reason": reason}

    def _handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if self.settings.telegram_chat_id and chat_id != self.settings.telegram_chat_id:
            return {"ok": True, "ignored": "foreign_chat"}

        user = message.get("from") or {}
        self.repository.upsert_user(user)

        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return {"ok": True, "ignored": "not_command"}

        command_token, *args = text.split()
        command = command_token.split("@", 1)[0].lower()
        thread_id = message.get("message_thread_id")

        if command == "/me":
            self._reply(message, self._format_me(int(user["id"])))
            return {"ok": True, "command": command}
        if command == "/top":
            self._reply(message, self._format_top())
            return {"ok": True, "command": command}
        if command == "/rules":
            self._reply(message, copy.RULES_TEXT)
            return {"ok": True, "command": command}
        if command == "/kvizi_challenge":
            return self._handle_challenge_command(args, message, thread_id, user)

        if command.startswith("/kvizi_"):
            if not self._is_admin(user):
                self._reply(message, copy.admin_only())
                return {"ok": True, "command": command, "admin": False}
            return self._handle_admin_command(command, args, message, thread_id)

        return {"ok": True, "ignored": "unknown_command"}

    def _handle_challenge_command(
        self,
        args: list[str],
        message: dict[str, Any],
        thread_id: int | None,
        user: dict[str, Any],
    ) -> dict[str, Any]:
        if thread_id is None:
            self._reply(message, "Вызов можно создать только внутри привязанного топика.")
            return {"ok": True, "command": "/kvizi_challenge", "posted": False}

        topic = self.repository.get_topic_by_thread(int(thread_id))
        if topic is None:
            self._reply(message, "Этот топик ещё не привязан. Админ должен выполнить /kvizi_bind <topic_key> <weight>.")
            return {"ok": True, "command": "/kvizi_challenge", "posted": False}

        topic_key = str(topic["topic_key"])
        available = sorted(self.question_bank.difficulties(topic_key))
        if not args:
            suffix = ", ".join(available) if available else "нет вопросов"
            self._reply(message, f"Формат: /kvizi_challenge <difficulty>. Доступно здесь: {suffix}.")
            return {"ok": True, "command": "/kvizi_challenge", "posted": False}

        difficulty = args[0].strip().lower()
        if difficulty not in available:
            suffix = ", ".join(available) if available else "нет вопросов"
            self._reply(message, f"В этом топике нет сложности {difficulty}. Доступно: {suffix}.")
            return {"ok": True, "command": "/kvizi_challenge", "posted": False}

        user_id = int(user["id"])
        now_iso = utc_now_iso()
        if self.repository.active_polls_for_thread(int(thread_id), now_iso):
            self._reply(message, "В этом топике уже есть активный вопрос. Дождись закрытия или попроси админа выполнить /kvizi_close_here.")
            return {"ok": True, "command": "/kvizi_challenge", "posted": False}

        if self.repository.has_active_challenge(user_id, now_iso):
            self._reply(message, "У тебя уже есть активный вызов. Сначала ответь на него или дождись закрытия.")
            return {"ok": True, "command": "/kvizi_challenge", "posted": False}

        cost = challenge_cost(difficulty)
        reward = challenge_reward(difficulty)
        score = self.repository.get_score(self.settings.season_name, user_id)
        if int(score["points"]) < cost:
            self._reply(message, f"Для вызова {difficulty} нужно {cost} очков. Сейчас у тебя {score['points']}.")
            return {"ok": True, "command": "/kvizi_challenge", "posted": False}

        result = self.post_question(
            topic_key=topic_key,
            difficulty=difficulty,
            requested_by=user_id,
            request_cost=cost,
            request_reward=reward,
        )
        if not result.posted:
            self._reply(message, result.message)
        return {
            "ok": True,
            "command": "/kvizi_challenge",
            "posted": result.posted,
            "difficulty": difficulty,
            "cost": cost,
            "reward": reward,
        }

    def _handle_admin_command(
        self,
        command: str,
        args: list[str],
        message: dict[str, Any],
        thread_id: int | None,
    ) -> dict[str, Any]:
        if command == "/kvizi_status":
            self._reply(message, self._format_status())
            return {"ok": True, "command": command}

        if command == "/kvizi_export":
            self._send_state_export(message, include_processed_updates="--full" in args)
            return {"ok": True, "command": command}

        if command == "/kvizi_close_here":
            closed = self._close_active_polls_here(message, thread_id)
            return {"ok": True, "command": command, "closed": closed}

        if command == "/kvizi_bind":
            if thread_id is None:
                self._reply(message, "Привязка работает только внутри топика.")
                return {"ok": True, "command": command, "bound": False}
            if len(args) < 2:
                self._reply(message, "Формат: /kvizi_bind <topic_key> <weight>")
                return {"ok": True, "command": command, "bound": False}
            topic_key = args[0].strip().lower()
            try:
                weight = int(args[1])
            except ValueError:
                self._reply(message, "weight должен быть целым числом.")
                return {"ok": True, "command": command, "bound": False}
            if weight <= 0:
                self._reply(message, "weight должен быть больше нуля.")
                return {"ok": True, "command": command, "bound": False}
            self.repository.bind_topic(topic_key, int(thread_id), weight)
            self._reply(message, f"Топик {topic_key} привязан с весом {weight}.")
            return {"ok": True, "command": command, "bound": True}

        if command == "/kvizi_topics":
            topics = self.repository.list_topics()
            lines = ["Топики Квизи:"]
            if not topics:
                lines.append("Пока пусто. Используй /kvizi_bind <topic_key> <weight> в нужном топике.")
            for topic in topics:
                status = "active" if topic["active"] else "off"
                lines.append(
                    f"- {topic['topic_key']}: thread={topic['message_thread_id']}, "
                    f"weight={topic['weight']}, {status}"
                )
            self._reply(message, "\n".join(lines))
            return {"ok": True, "command": command}

        if command == "/kvizi_reload":
            count = self.reload_questions()
            self._reply(message, f"CSV перезагружен: {count} вопросов.")
            return {"ok": True, "command": command, "count": count}

        if command == "/kvizi_postnow":
            topic_key = self._postnow_topic_key(args, thread_id)
            if self._topic_has_active_poll(topic_key, thread_id):
                self._reply(message, "В выбранном топике уже есть активный вопрос. Используй /kvizi_close_here или дождись закрытия.")
                return {"ok": True, "command": command, "posted": False}
            result = self.post_question(topic_key, skip_busy_topics=True)
            if not result.posted:
                self._reply(message, result.message)
            return {"ok": True, "command": command, "posted": result.posted}

        if command == "/kvizi_season_reset":
            count = self.repository.reset_season(self.settings.season_name)
            self._reply(message, f"Сезон {self.settings.season_name} сброшен. Строк удалено: {count}.")
            return {"ok": True, "command": command, "reset": count}

        if command == "/kvizi_announce_here":
            if thread_id is None:
                self._reply(message, "Анонс-топик можно назначить только внутри топика.")
                return {"ok": True, "command": command, "announced": False}
            self.repository.set_bot_setting("announce_thread_id", str(int(thread_id)))
            self._reply(message, f"Анонсы Квизи будут появляться в этом топике: thread={thread_id}.")
            return {"ok": True, "command": command, "announced": True}

        self._reply(message, "Неизвестная админ-команда Квизи.")
        return {"ok": True, "command": command, "ignored": True}

    def _reply(self, message: dict[str, Any], text: str) -> None:
        thread_id = message.get("message_thread_id")
        chat_id = str((message.get("chat") or {}).get("id") or self.settings.telegram_chat_id)
        self.telegram.send_message(
            chat_id=chat_id,
            message_thread_id=int(thread_id) if thread_id is not None else None,
            text=text,
        )

    def _send_state_export(self, message: dict[str, Any], include_processed_updates: bool = False) -> None:
        state = export_state(
            self.settings.database_path,
            include_processed_updates=include_processed_updates,
        )
        content = (
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        exported_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"kvizi-state-{exported_at}.json"
        caption = (
            "Экспорт Квизи: "
            f"users={len(state['users'])}, "
            f"scores={len(state['scores'])}, "
            f"active_polls={len(state['active_polls'])}"
        )
        thread_id = message.get("message_thread_id")
        chat_id = str((message.get("chat") or {}).get("id") or self.settings.telegram_chat_id)
        self.telegram.send_document(
            chat_id=chat_id,
            message_thread_id=int(thread_id) if thread_id is not None else None,
            filename=filename,
            content=content,
            caption=caption,
        )

    def _close_active_polls_here(self, message: dict[str, Any], thread_id: int | None) -> int:
        if thread_id is None:
            self._reply(message, "Закрывать вопросы можно только внутри топика.")
            return 0

        now_iso = utc_now_iso()
        polls = self.repository.active_polls_for_thread(int(thread_id), now_iso)
        if not polls:
            self._reply(message, "В этом топике нет активных вопросов.")
            return 0

        closed = 0
        for poll in polls:
            self.repository.settle_unanswered_challenge(
                season=self.settings.season_name,
                poll=poll,
                now_iso=now_iso,
            )
            try:
                self.telegram.stop_poll(
                    chat_id=self.settings.telegram_chat_id,
                    message_id=int(poll["telegram_message_id"]),
                )
            except TelegramApiError:
                pass
            self.repository.mark_poll_closed(str(poll["poll_id"]))
            closed += 1

        self._reply(message, f"Закрыто активных вопросов в этом топике: {closed}.")
        return closed

    def _postnow_topic_key(self, args: list[str], thread_id: int | None) -> str | None:
        if args:
            return args[0].strip().lower()
        if thread_id is None:
            return None
        topic = self.repository.get_topic_by_thread(int(thread_id))
        return None if topic is None else str(topic["topic_key"])

    def _topic_has_active_poll(self, topic_key: str | None, thread_id: int | None) -> bool:
        now_iso = utc_now_iso()
        if topic_key is not None:
            return topic_key in self.repository.active_poll_topic_keys(now_iso)
        if thread_id is not None:
            return bool(self.repository.active_polls_for_thread(int(thread_id), now_iso))
        return False

    def _is_admin(self, user: dict[str, Any]) -> bool:
        user_id = user.get("id")
        return user_id is not None and int(user_id) in self.settings.admin_ids

    def _format_me(self, user_id: int) -> str:
        score = self.repository.get_score(self.settings.season_name, user_id)
        return (
            f"Твой счет: {score['points']}\n"
            f"Серия: {score['current_streak']} (лучшее: {score['best_streak']})\n"
            f"Верно/ошибки: {score['correct_count']}/{score['wrong_count']}"
        )

    def _format_top(self) -> str:
        rows = self.repository.leaderboard(self.settings.season_name)
        if not rows:
            return copy.top_header(self.settings.season_name) + "\nПока никто не набрал очки."
        lines = [copy.top_header(self.settings.season_name)]
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index}. {self._display_name(row)} - {row['points']} "
                f"(серия {row['current_streak']}, верно {row['correct_count']})"
            )
        return "\n".join(lines)

    def _format_status(self) -> str:
        now_iso = utc_now_iso()
        topics = self.repository.list_topics()
        active_polls = self.repository.active_polls(now_iso)
        challenge_count = sum(
            1
            for poll in active_polls
            if int(poll.get("request_cost") or 0) > 0
            and int(poll.get("requester_answered") or 0) == 0
        )
        cron = self.repository.latest_cron_run()
        announce_thread_id = self._announce_thread_id()
        difficulties = ", ".join(sorted(self.question_bank.difficulties())) or "нет"

        lines = [
            "Статус Квизи:",
            f"Вопросы: {self.question_bank.count()}",
            f"Сложности: {difficulties}",
            f"Сезон: {self.settings.season_name}",
            f"Анонс-топик: {announce_thread_id if announce_thread_id is not None else 'не задан'}",
        ]

        lines.append("Топики:")
        if not topics:
            lines.append("- не привязаны")
        for topic in topics[:10]:
            status = "active" if int(topic["active"]) else "off"
            lines.append(
                f"- {topic['topic_key']}: thread={topic['message_thread_id']}, "
                f"weight={topic['weight']}, {status}"
            )
        if len(topics) > 10:
            lines.append(f"- ... ещё {len(topics) - 10}")

        lines.append(f"Активные вопросы: {len(active_polls)}")
        if not active_polls:
            lines.append("- нет")
        for poll in active_polls[:10]:
            challenge = ""
            if int(poll.get("request_cost") or 0) > 0:
                challenge = (
                    f", challenge user={self._poll_requester_name(poll)} "
                    f"cost={poll['request_cost']} reward={poll['request_reward']}"
                )
                if int(poll.get("requester_answered") or 0):
                    challenge += ", answered"
            lines.append(
                f"- {poll['topic_key']} {poll['difficulty']} "
                f"poll={poll['poll_id']} до {self._short_dt(poll['closes_at'])}{challenge}"
            )
        if len(active_polls) > 10:
            lines.append(f"- ... ещё {len(active_polls) - 10}")

        lines.append(f"Активные challenge: {challenge_count}")
        if cron is None:
            lines.append("Последний cron: нет")
        else:
            lines.append(
                f"Последний cron: {cron['status']} в {self._short_dt(cron['finished_at'])}; "
                f"{cron['message']}"
            )

        return "\n".join(lines)

    def _short_dt(self, value: str) -> str:
        return value.replace("T", " ").split("+", 1)[0].split(".", 1)[0]

    def _poll_requester_name(self, poll: dict[str, Any]) -> str:
        user_id = poll.get("requested_by")
        username = poll.get("requester_username")
        if username:
            return f"@{username} ({user_id})"
        name = " ".join(
            str(poll.get(key) or "")
            for key in ("requester_first_name", "requester_last_name")
        ).strip()
        return f"{name} ({user_id})" if name else str(user_id)

    def _display_name(self, row: dict[str, Any]) -> str:
        if row.get("username"):
            return f"@{row['username']}"
        name = " ".join(str(row.get(key) or "") for key in ("first_name", "last_name")).strip()
        return name or str(row["user_id"])

    def _poll_title(self, question: Question) -> str:
        title = f"Квизи спрашивает: {question.text}"
        return title if len(title) <= 300 else question.text[:300]

    def _announce_question(self, route: TopicRoute, question: Question, question_link: str | None) -> None:
        announce_thread_id = self._announce_thread_id()
        if announce_thread_id is None or not question_link:
            return
        self.telegram.send_message(
            chat_id=self.settings.telegram_chat_id,
            message_thread_id=announce_thread_id,
            text=copy.question_announcement(route.topic_key, question.difficulty, question_link),
            disable_notification=True,
        )

    def _announce_thread_id(self) -> int | None:
        if self.settings.announce_thread_id is not None:
            return self.settings.announce_thread_id
        value = self.repository.get_bot_setting("announce_thread_id")
        return int(value) if value else None

    def _message_link(self, message_id: int) -> str | None:
        username = self.settings.chat_username
        chat_id = self.settings.telegram_chat_id.strip()
        if username:
            return f"https://t.me/{username}/{message_id}"
        if chat_id.startswith("@"):
            return f"https://t.me/{chat_id.lstrip('@')}/{message_id}"
        if chat_id.startswith("-100") and chat_id[4:].isdigit():
            return f"https://t.me/c/{chat_id[4:]}/{message_id}"
        return None

    def _score_event_text(self, user: dict[str, Any], result: Any) -> str:
        name = user.get("first_name") or user.get("username") or user.get("id")
        if result.is_challenge:
            if result.is_correct:
                return f"{name}: вызов пройден! +{result.delta}. Всего {result.points}."
            return f"{name}: вызов провален. {result.delta}. Всего {result.points}."
        if result.is_correct:
            bonus = f", бонус серии +{result.streak_bonus}" if result.streak_bonus else ""
            return f"{name}: верно на x{result.stake}! +{result.delta}{bonus}. Всего {result.points}."
        return f"{name}: риск x{result.stake} щелкнул не туда. {result.delta}. Всего {result.points}."
