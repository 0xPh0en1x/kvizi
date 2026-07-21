from __future__ import annotations

import csv
import json
import random
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from kvizi import __version__
from kvizi import copy
from kvizi.config import PROJECT_ROOT, Settings
from kvizi.database import KviziRepository, utc_now_iso
from kvizi.export_state import export_state
from kvizi.question_report import build_report, find_duplicate_ids, format_report_for_telegram
from kvizi.questions import DIFFICULTY_PATTERN, QUESTION_COLUMNS, Question, QuestionBank, load_questions
from kvizi.questions import QuestionValidationError
from kvizi.routing import TopicRoute, TopicRouter
from kvizi.scoring import base_points, challenge_cost, challenge_reward
from kvizi.telegram import TelegramApiError, TelegramClient

MAX_QUESTIONS_UPLOAD_BYTES = 2_000_000
PROD_CHECK_RECENT_HOURS = 36
OPERATION_CLAIM_SECONDS = 300
ANSWER_DELIVERY_GRACE_SECONDS = 3600
MISMATCH_DELIVERY_GRACE_SECONDS = 86400
TRANSIENT_TELEGRAM_ERROR_MARKERS = (
    "503 service unavailable",
    "unable to connect to proxy",
    "tunnel connection failed",
    "max retries exceeded",
    "httpsconnectionpool(host='api.telegram.org'",
    "proxy 503",
    "read timed out",
    "connecttimeout",
    "connection reset by peer",
    "remote end closed connection",
    "temporary failure in name resolution",
)


@dataclass(frozen=True)
class PostQuestionResult:
    posted: bool
    message: str
    topic_key: str | None = None
    question_id: str | None = None
    poll_id: str | None = None
    question_link: str | None = None


@dataclass(frozen=True)
class DailySummaryResult:
    posted: bool
    message: str
    summary_date: str


@dataclass(frozen=True)
class StateExportFile:
    filename: str
    content: bytes
    caption: str


@dataclass(frozen=True)
class BackupExportResult:
    sent_count: int
    failed_count: int
    admin_ids: list[int]
    filename: str
    errors: list[str]

    @property
    def total_count(self) -> int:
        return len(self.admin_ids)

    @property
    def ok(self) -> bool:
        return self.sent_count > 0 and self.failed_count == 0


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

    def post_daily_summary(
        self,
        *,
        force: bool = False,
        target_thread_id: int | None = None,
        remember_sent: bool = True,
    ) -> DailySummaryResult:
        self.close_expired_polls()
        summary_date, start_iso, end_iso = self._daily_window()
        setting_key = "daily_summary_last_date"
        if not force and self.repository.get_bot_setting(setting_key) == summary_date:
            return DailySummaryResult(False, f"Итоги за {summary_date} уже отправлены.", summary_date)

        operation_key = f"daily_summary:{summary_date}"
        claimed_at = datetime.now(timezone.utc)
        if not force and not self.repository.try_claim_operation(
            operation_key,
            claimed_at=claimed_at.isoformat(),
            expires_at=(claimed_at + timedelta(seconds=OPERATION_CLAIM_SECONDS)).isoformat(),
        ):
            return DailySummaryResult(False, f"Итоги за {summary_date} уже отправляются.", summary_date)

        release_claim = not force
        delivery_attempted = False
        try:
            if not force and self.repository.get_bot_setting(setting_key) == summary_date:
                return DailySummaryResult(False, f"Итоги за {summary_date} уже отправлены.", summary_date)

            stats = self.repository.daily_summary(start_iso, end_iso)
            text = self._format_daily_summary(summary_date, stats)
            thread_id = target_thread_id
            if thread_id is None:
                thread_id = self._announce_thread_id()

            delivery_attempted = True
            self.telegram.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=text,
                disable_notification=True,
            )
            if remember_sent:
                self.repository.set_bot_setting(setting_key, summary_date)
            return DailySummaryResult(True, text, summary_date)
        except TelegramApiError as exc:
            if exc.ambiguous:
                release_claim = False
            raise
        except Exception:
            if delivery_attempted:
                release_claim = False
            raise
        finally:
            if release_claim:
                self.repository.release_operation(operation_key)

    def post_backup_export(self) -> BackupExportResult:
        export_file = self._build_state_export(
            filename_prefix="kvizi-backup",
            caption_prefix="Backup Квизи",
        )
        admin_ids = sorted(self.settings.admin_ids)
        sent_count = 0
        errors: list[str] = []

        for admin_id in admin_ids:
            try:
                self.telegram.send_document(
                    chat_id=str(admin_id),
                    filename=export_file.filename,
                    content=export_file.content,
                    caption=export_file.caption,
                    mime_type="application/json",
                )
            except TelegramApiError as exc:
                error_message = f"{admin_id}: {exc}"
                self.repository.record_error_event(
                    source="telegram",
                    event="backup_send_failed",
                    message=error_message,
                )
                errors.append(error_message)
                continue
            sent_count += 1

        return BackupExportResult(
            sent_count=sent_count,
            failed_count=len(errors),
            admin_ids=admin_ids,
            filename=export_file.filename,
            errors=errors,
        )

    def close_expired_polls(self) -> int:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        changed = 0
        expired = self.repository.expired_active_polls(now_iso)
        for poll in expired:
            if self._close_poll(
                poll,
                now_iso=now_iso,
                stop_error_event="stop_poll_failed",
            ):
                changed += 1

        cutoff_iso = (now - timedelta(seconds=ANSWER_DELIVERY_GRACE_SECONDS)).isoformat()
        mismatch_cutoff_iso = (
            now - timedelta(seconds=MISMATCH_DELIVERY_GRACE_SECONDS)
        ).isoformat()
        for poll in self.repository.closing_polls_due(cutoff_iso):
            telegram_voter_count = poll.get("telegram_voter_count")
            human_answer_count = self.repository.human_answer_count_for_poll(
                str(poll["poll_id"])
            )
            if (
                telegram_voter_count is not None
                and human_answer_count < int(telegram_voter_count)
                and str(poll["closed_at"]) > mismatch_cutoff_iso
            ):
                continue
            if self._finalize_closing_poll(poll, now_iso=now_iso):
                changed += 1
        return changed

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
            return PostQuestionResult(False, copy.no_questions_text(self.rng.choice))

        busy_topic_keys = self.repository.active_poll_topic_keys(utc_now_iso()) if skip_busy_topics else set()
        route = self._select_route(topic_key, excluded_topic_keys=busy_topic_keys)
        if route is None:
            if skip_busy_topics and busy_topic_keys:
                return PostQuestionResult(False, "Все подходящие топики заняты активными вопросами.")
            return PostQuestionResult(False, "Нет активных топиков с вопросами.")

        # Telegram delivery and the following SQLite write are not atomic. A single
        # durable claim keeps overlapping cron/manual requests from publishing two
        # polls before either request has persisted its result.
        operation_key = "post_question"
        claimed_at = datetime.now(timezone.utc)
        if not self.repository.try_claim_operation(
            operation_key,
            claimed_at=claimed_at.isoformat(),
            expires_at=(claimed_at + timedelta(seconds=OPERATION_CLAIM_SECONDS)).isoformat(),
        ):
            return PostQuestionResult(False, f"В теме {route.topic_key} вопрос уже публикуется.")

        release_claim = True
        delivery_attempted = False
        try:
            if (
                skip_busy_topics
                and route.topic_key in self.repository.active_poll_topic_keys(utc_now_iso())
            ):
                return PostQuestionResult(False, f"В теме {route.topic_key} уже есть активный вопрос.")

            question = self._select_question(route.topic_key, difficulty)
            if question is None:
                if difficulty:
                    return PostQuestionResult(
                        False,
                        f"В теме {route.topic_key} нет вопросов сложности {difficulty}.",
                    )
                return PostQuestionResult(False, f"В теме {route.topic_key} нет вопросов.")

            delivery_attempted = True
            sent = self.telegram.send_poll(
                chat_id=self.settings.telegram_chat_id,
                question=self._poll_title(question),
                options=list(question.options),
                correct_option_id=question.correct_option_id,
                explanation=question.explanation,
                open_period=self.settings.open_seconds,
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
                copy.question_intro(
                    route.topic_key,
                    question.difficulty,
                    base_points(question.difficulty, self.settings.difficulty_points),
                    self.rng.choice,
                ),
                topic_key=route.topic_key,
                question_id=question.id,
                poll_id=str(poll["id"]),
                question_link=question_link,
            )
        except TelegramApiError as exc:
            if exc.ambiguous:
                release_claim = False
            raise
        except Exception:
            if delivery_attempted:
                release_claim = False
            raise
        finally:
            if release_claim:
                self.repository.release_operation(operation_key)

    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        update_id = update.get("update_id")
        claimed_update_id = int(update_id) if update_id is not None else None
        if not self.repository.try_claim_update(claimed_update_id):
            return {"ok": True, "duplicate": True}

        try:
            if "poll_answer" in update:
                return self._handle_poll_answer(update["poll_answer"])
            if "poll" in update:
                return self._handle_poll_update(update["poll"])
            if "callback_query" in update:
                return self._handle_callback_query(update["callback_query"])
            if "message" in update:
                return self._handle_message(update["message"])
            return {"ok": True, "ignored": True}
        except Exception:
            self.repository.forget_update(claimed_update_id)
            raise

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
        previous_leader = self._current_season_leader()
        result = self.repository.record_answer(
            season=self.settings.season_name,
            poll_id=poll_id,
            user=user,
            option_ids=option_ids,
            now_iso=utc_now_iso(),
            difficulty_points=self.settings.difficulty_points,
        )

        if not result.recorded and result.reason_code not in {"", "duplicate"}:
            self.repository.record_error_event(
                source="telegram",
                event="poll_answer_rejected",
                message=(
                    f"poll={poll_id}, user={user.get('id')}, "
                    f"reason={result.reason_code}"
                ),
            )

        poll = self.repository.get_poll(poll_id) if result.recorded else None
        should_send_score_event = (
            result.recorded
            and poll is not None
            and (result.stake > 1 or result.streak_bonus > 0 or result.is_challenge)
        )
        if should_send_score_event and poll is not None:
            self.telegram.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=int(poll["message_thread_id"]),
                text=self._score_event_text(user, result),
                disable_notification=True,
            )

        if result.recorded:
            self._announce_first_answer_of_day(user, result, poll)
            self._announce_risk_failure(user, result)
            self._announce_streak_milestone(user, result)
            self._announce_season_leader_change(previous_leader)

        return {
            "ok": True,
            "recorded": result.recorded,
            "delta": result.delta,
            "points": result.points,
            "reason": result.reason,
        }

    def _handle_poll_update(self, poll: dict[str, Any]) -> dict[str, Any]:
        poll_id = str(poll.get("id") or "")
        if not poll_id or not poll.get("is_closed"):
            return {"ok": True, "ignored": True}

        closing = self.repository.mark_poll_closing(
            poll_id,
            closed_at=utc_now_iso(),
            telegram_voter_count=self._telegram_voter_count(poll),
        )
        return {"ok": True, "poll_id": poll_id, "closing": closing}

    def _handle_callback_query(self, query: dict[str, Any]) -> dict[str, Any]:
        data = str(query.get("data") or "")
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        if self.settings.telegram_chat_id and chat_id != self.settings.telegram_chat_id:
            return {"ok": True, "ignored": "foreign_chat"}

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

        poll_id = str((message.get("poll") or {}).get("id") or "")
        if not poll_id:
            self.telegram.answer_callback_query(
                callback_query_id=str(query["id"]),
                text=copy.bet_rejected("опрос не найден", self.rng.choice),
                show_alert=True,
            )
            return {"ok": True, "bet": False}

        ok, reason = self.repository.record_bet(
            poll_id=poll_id,
            user_id=int(user["id"]),
            stake=stake,
            now_iso=utc_now_iso(),
        )
        bet_text = (
            copy.bet_accepted(stake, self.rng.choice)
            if ok
            else copy.bet_rejected(reason, self.rng.choice)
        )
        self.telegram.answer_callback_query(
            callback_query_id=str(query["id"]),
            text=bet_text,
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

        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text.startswith("/"):
            return {"ok": True, "ignored": "not_command"}

        command_token, *args = text.split()
        command = command_token.split("@", 1)[0].lower()
        thread_id = message.get("message_thread_id")

        if command == "/me":
            self._reply(message, self._format_me(int(user["id"])))
            return {"ok": True, "command": command}
        if command == "/top":
            topic_key = args[0].strip().lower() if args else None
            self._reply(message, self._format_top(topic_key))
            return {"ok": True, "command": command, "topic_key": topic_key}
        if command == "/rules":
            self._reply(
                message,
                copy.rules_text(
                    self.settings.difficulty_points,
                    self.settings.challenge_economy,
                ),
            )
            return {"ok": True, "command": command}
        if command == "/kvizi_help":
            self._reply(message, copy.USER_HELP_TEXT)
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

        cost = challenge_cost(difficulty, self.settings.challenge_economy)
        reward = challenge_reward(difficulty, self.settings.challenge_economy)
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
        if command == "/kvizi_help_admin":
            self._reply(message, copy.ADMIN_HELP_TEXT)
            return {"ok": True, "command": command}

        if command == "/kvizi_status":
            self._reply(message, self._format_status())
            return {"ok": True, "command": command}

        if command == "/kvizi_status_compact":
            self._reply(message, self._format_status_compact())
            return {"ok": True, "command": command}

        if command == "/kvizi_config":
            self._reply(
                message,
                copy.config_text(
                    self.settings.difficulty_points,
                    self.settings.challenge_economy,
                    self._announce_flags(),
                ),
            )
            return {"ok": True, "command": command}

        if command == "/kvizi_voice_preview":
            self._reply(message, copy.voice_preview_text(self.rng.choice))
            return {"ok": True, "command": command}

        if command == "/kvizi_prod_check":
            self._reply(message, self._format_prod_check())
            return {"ok": True, "command": command}

        if command == "/kvizi_version":
            self._reply(message, self._format_version())
            return {"ok": True, "command": command}

        if command == "/kvizi_recent":
            self._reply(message, self._format_recent())
            return {"ok": True, "command": command}

        if command == "/kvizi_errors":
            self._reply(message, self._format_errors())
            return {"ok": True, "command": command}

        if command == "/kvizi_review":
            self._reply(message, self._format_question_review())
            return {"ok": True, "command": command}

        if command == "/kvizi_questions_status":
            self._reply(message, self._format_questions_status())
            return {"ok": True, "command": command}

        if command == "/kvizi_questions_template":
            sent = self._send_questions_template(message, args)
            return {"ok": True, "command": command, "sent": sent}

        if command == "/kvizi_upload_questions":
            check_only = "--check" in args
            uploaded = self._handle_upload_questions(message, check_only=check_only)
            return {"ok": True, "command": command, "uploaded": uploaded, "check_only": check_only}

        if command == "/kvizi_backups":
            self._reply(message, self._format_question_backups())
            return {"ok": True, "command": command}

        if command == "/kvizi_restore_questions":
            restored = self._handle_restore_questions(args, message)
            return {"ok": True, "command": command, "restored": restored}

        if command == "/kvizi_export":
            self._send_state_export(message, include_processed_updates="--full" in args)
            return {"ok": True, "command": command}

        if command == "/kvizi_daily":
            thread_id = message.get("message_thread_id")
            result = self.post_daily_summary(
                force=True,
                target_thread_id=int(thread_id) if thread_id is not None else None,
                remember_sent=False,
            )
            return {"ok": True, "command": command, "posted": result.posted}

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
        export_file = self._build_state_export(
            include_processed_updates=include_processed_updates,
            filename_prefix="kvizi-state",
            caption_prefix="Экспорт Квизи",
        )
        thread_id = message.get("message_thread_id")
        chat_id = str((message.get("chat") or {}).get("id") or self.settings.telegram_chat_id)
        self.telegram.send_document(
            chat_id=chat_id,
            message_thread_id=int(thread_id) if thread_id is not None else None,
            filename=export_file.filename,
            content=export_file.content,
            caption=export_file.caption,
            mime_type="application/json",
        )

    def _build_state_export(
        self,
        *,
        include_processed_updates: bool = False,
        filename_prefix: str,
        caption_prefix: str,
    ) -> StateExportFile:
        state = export_state(
            self.settings.database_path,
            include_processed_updates=include_processed_updates,
        )
        content = (
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        exported_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{filename_prefix}-{exported_at}.json"
        caption = (
            f"{caption_prefix}: "
            f"users={len(state['users'])}, "
            f"scores={len(state['scores'])}, "
            f"active_polls={len(state['active_polls'])}"
        )
        return StateExportFile(filename=filename, content=content, caption=caption)

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
            if self._close_poll(
                poll,
                now_iso=now_iso,
                stop_error_event="close_here_stop_poll_failed",
            ):
                closed += 1

        self._reply(message, f"Закрыто активных вопросов в этом топике: {closed}.")
        return closed

    def _close_poll(
        self,
        poll: dict[str, Any],
        *,
        now_iso: str,
        stop_error_event: str,
    ) -> bool:
        poll_id = str(poll["poll_id"])
        telegram_voter_count: int | None = None
        try:
            stopped = self.telegram.stop_poll(
                chat_id=self.settings.telegram_chat_id,
                message_id=int(poll["telegram_message_id"]),
            )
            telegram_voter_count = self._telegram_voter_count(stopped.get("result") or {})
        except TelegramApiError as exc:
            if not self._stop_error_means_poll_closed(exc):
                self.repository.record_error_event(
                    source="telegram",
                    event=stop_error_event,
                    message=(
                        f"poll={poll_id}, message={poll['telegram_message_id']}: {exc}"
                    ),
                )
                return False

        return self.repository.mark_poll_closing(
            poll_id,
            closed_at=now_iso,
            telegram_voter_count=telegram_voter_count,
        )

    def _finalize_closing_poll(self, poll: dict[str, Any], *, now_iso: str) -> bool:
        finalized, _settlement = self.repository.finalize_closing_poll(
            season=self.settings.season_name,
            poll=poll,
            now_iso=now_iso,
        )
        if not finalized:
            return False
        human_answer_count = self.repository.human_answer_count_for_poll(str(poll["poll_id"]))
        telegram_voter_count = poll.get("telegram_voter_count")
        if telegram_voter_count is not None and human_answer_count < int(telegram_voter_count):
            self.repository.record_error_event(
                source="telegram",
                event="poll_answer_count_mismatch",
                message=(
                    f"poll={poll['poll_id']}: telegram={telegram_voter_count}, "
                    f"sqlite={human_answer_count}"
                ),
            )
        if human_answer_count == 0 and (
            telegram_voter_count is None or int(telegram_voter_count) == 0
        ):
            self._announce_no_answers_closed(poll)
        return True

    def _telegram_voter_count(self, poll: dict[str, Any]) -> int | None:
        value = poll.get("total_voter_count")
        return int(value) if value is not None else None

    def _stop_error_means_poll_closed(self, error: TelegramApiError) -> bool:
        if error.ambiguous:
            return False
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "poll has already been closed",
                "message is not a poll",
                "message to stop not found",
            )
        )

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

    def _format_top(self, topic_key: str | None = None) -> str:
        if topic_key:
            rows = self.repository.topic_leaderboard(self.settings.season_name, topic_key)
            known_topics = self._known_topic_keys()
            if not rows and known_topics and topic_key not in known_topics:
                return (
                    f"Сектор {topic_key} не найден. "
                    f"Доступно: {', '.join(sorted(known_topics))}."
                )
            if not rows:
                return f"Табло сектора {topic_key}:\nПока никто не набрал очки в этом секторе."
            lines = [f"Табло сектора {topic_key}:"]
            for index, row in enumerate(rows, start=1):
                lines.append(
                    f"{index}. {self._display_name(row)} - {row['points']} "
                    f"(верно {row['correct_count']}/{row['answered_count']})"
                )
            return "\n".join(lines)

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

    def _known_topic_keys(self) -> set[str]:
        return self.question_bank.topics() | {str(topic["topic_key"]) for topic in self.repository.list_topics()}

    def _announce_flags(self) -> dict[str, bool]:
        return {
            "first_answer": self.settings.announce_first_answer,
            "no_answers": self.settings.announce_no_answers,
            "risk_failures": self.settings.announce_risk_failures,
            "streaks": self.settings.announce_streaks,
        }

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

    def _format_status_compact(self) -> str:
        now_iso = utc_now_iso()
        topics = self.repository.list_topics()
        active_polls = self.repository.active_polls(now_iso)
        expired_polls = self.repository.expired_active_polls(now_iso)
        challenge_count = sum(
            1
            for poll in active_polls
            if int(poll.get("request_cost") or 0) > 0
            and int(poll.get("requester_answered") or 0) == 0
        )
        active_topics = [topic for topic in topics if int(topic["active"]) and int(topic["weight"]) > 0]
        cron = self.repository.latest_cron_run()
        announce_thread_id = self._announce_thread_id()
        difficulties = ", ".join(sorted(self.question_bank.difficulties())) or "нет"
        busy_topics = self._topic_counts(active_polls)

        lines = [
            "Статус Квизи compact:",
            f"Вопросы: {self.question_bank.count()} | сложности: {difficulties}",
            f"Топики: active={len(active_topics)}/{len(topics)} | анонсы: {announce_thread_id if announce_thread_id is not None else 'не задан'}",
            f"Poll: active={len(active_polls)}, expired={len(expired_polls)}, challenge={challenge_count}",
        ]

        if busy_topics:
            lines.append(f"Занятые топики: {busy_topics}")
        else:
            lines.append("Занятые топики: нет")

        if active_polls:
            next_poll = active_polls[0]
            lines.append(
                "Ближайшее закрытие: "
                f"{next_poll['topic_key']} {next_poll['difficulty']} до {self._short_dt(next_poll['closes_at'])}"
            )
        else:
            lines.append("Ближайшее закрытие: нет")

        if expired_polls:
            lines.append("Maintenance: есть просроченные poll, дерни /cron/maintenance.")
        else:
            lines.append("Maintenance: просроченных poll нет.")

        if cron is None:
            lines.append("Последний cron: нет")
        else:
            lines.append(
                f"Последний cron: {cron['status']} в {self._short_dt(cron['finished_at'])}"
            )

        return "\n".join(lines)

    def _format_version(self) -> str:
        commit = self._git_value("rev-parse", "--short=12", "HEAD")
        branch = self._git_value("rev-parse", "--abbrev-ref", "HEAD")
        dirty = self._git_value("status", "--porcelain", "--untracked-files=no")
        if commit and branch:
            state = "dirty" if dirty else "clean"
            git_line = f"{branch}@{commit} ({state})"
        else:
            git_line = "unavailable"

        return "\n".join(
            [
                "Версия Квизи:",
                f"app: {__version__}",
                f"git: {git_line}",
                f"project_root: {PROJECT_ROOT}",
                f"database: {self.settings.database_path}",
                f"questions: {self.settings.questions_path}",
                f"question_count: {self.question_bank.count()}",
                f"season: {self.settings.season_name}",
                f"timezone: {self.settings.timezone_name}",
            ]
        )

    def _git_value(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=PROJECT_ROOT,
                capture_output=True,
                check=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip()

    def _format_prod_check(self) -> str:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        checks: list[tuple[str, str]] = []

        def add(level: str, text: str) -> None:
            checks.append((level, text))

        question_count = self.question_bank.count()
        csv_topics = self.question_bank.topics()
        topics = self.repository.list_topics()
        active_topics = [
            topic for topic in topics if int(topic["active"]) and int(topic["weight"]) > 0
        ]
        active_topic_keys = {str(topic["topic_key"]) for topic in active_topics}
        duplicate_ids = find_duplicate_ids(self.settings.questions_path)

        if question_count <= 0:
            add("FAIL", "questions.csv: вопросов нет")
        elif duplicate_ids:
            add("FAIL", f"questions.csv: {question_count} вопросов, duplicate ids: {', '.join(duplicate_ids[:5])}")
        else:
            add("OK", f"questions.csv: {question_count} вопросов, duplicate ids: none")

        if not active_topics:
            add("FAIL", "топики: нет активных привязанных топиков")
        else:
            add("OK", f"топики: active={len(active_topics)}/{len(topics)}")

        missing_bindings = sorted(csv_topics - active_topic_keys)
        if missing_bindings:
            add("WARN", f"CSV-топики без активной привязки: {', '.join(missing_bindings)}")
        elif csv_topics:
            add("OK", "CSV-топики привязаны")

        bound_without_questions = sorted(active_topic_keys - csv_topics)
        if bound_without_questions:
            add("WARN", f"привязки без вопросов в CSV: {', '.join(bound_without_questions)}")

        announce_thread_id = self._announce_thread_id()
        if announce_thread_id is None:
            add("WARN", "анонс-топик не задан: выполни /kvizi_announce_here")
        else:
            add("OK", f"анонс-топик: {announce_thread_id}")

        expired_polls = self.repository.expired_active_polls(now_iso)
        active_polls = self.repository.active_polls(now_iso)
        if expired_polls:
            add("FAIL", f"просроченные poll: {len(expired_polls)}; дерни /cron/maintenance")
        else:
            add("OK", "просроченных poll нет")
        if active_polls:
            next_poll = active_polls[0]
            add(
                "OK",
                "активные poll: "
                f"{len(active_polls)}, ближайшее закрытие {next_poll['topic_key']} "
                f"{self._short_dt(next_poll['closes_at'])}",
            )
        else:
            add("OK", "активные poll: нет")

        cron_checks = (
            ("cron/tick", ("posted", "skipped", "failed")),
            (
                "cron/maintenance",
                ("maintenance_ok", "maintenance_closed", "maintenance_failed"),
            ),
            ("cron/daily", ("daily_posted", "daily_skipped", "daily_failed")),
            (
                "cron/backup",
                ("backup_sent", "backup_skipped", "backup_partial", "backup_failed"),
            ),
        )
        for label, statuses in cron_checks:
            add(*self._prod_cron_check(label, statuses, now))

        recent_failed = [
            run
            for run in self.repository.recent_failed_cron_runs(limit=3)
            if self._is_recent_iso(str(run["finished_at"]), now, PROD_CHECK_RECENT_HOURS)
        ]
        recent_errors = [
            event
            for event in self.repository.recent_error_events(limit=3)
            if self._is_recent_iso(str(event["created_at"]), now, PROD_CHECK_RECENT_HOURS)
        ]
        transient_errors = [
            event for event in recent_errors if self._is_transient_error_event(event)
        ]
        actionable_errors = [
            event for event in recent_errors if not self._is_transient_error_event(event)
        ]
        if recent_failed:
            add("WARN", f"свежие failed cron: {len(recent_failed)}; смотри /kvizi_errors")
        if actionable_errors:
            add("WARN", f"свежие error events: {len(actionable_errors)}; смотри /kvizi_errors")
        if transient_errors:
            add("INFO", f"transient Telegram/proxy events: {len(transient_errors)}; смотри /kvizi_errors")
        if not recent_failed and not recent_errors:
            add("OK", "свежих ошибок в журнале нет")

        severity = "OK"
        if any(level == "FAIL" for level, _text in checks):
            severity = "FAIL"
        elif any(level == "WARN" for level, _text in checks):
            severity = "WARN"

        lines = [f"Prod-check Квизи: {severity}"]
        lines.extend(f"[{level}] {text}" for level, text in checks)
        return "\n".join(lines)

    def _prod_cron_check(
        self,
        label: str,
        statuses: tuple[str, ...],
        now: datetime,
    ) -> tuple[str, str]:
        run = self.repository.latest_cron_run_for_statuses(statuses)
        if run is None:
            return "WARN", f"{label}: запусков нет"

        status = str(run["status"])
        finished_at = str(run["finished_at"])
        dt = self._parse_utc_dt(finished_at)
        stale = dt is None or now - dt > timedelta(hours=PROD_CHECK_RECENT_HOURS)
        if "failed" in status:
            level = "FAIL"
        elif status in {"backup_partial", "backup_skipped"} or stale:
            level = "WARN"
        else:
            level = "OK"

        age = self._age_text(dt, now) if dt is not None else "возраст неизвестен"
        return level, f"{label}: {status} в {self._short_dt(finished_at)} ({age})"

    def _parse_utc_dt(self, value: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _is_recent_iso(self, value: str, now: datetime, hours: int) -> bool:
        dt = self._parse_utc_dt(value)
        return dt is not None and now - dt <= timedelta(hours=hours)

    def _age_text(self, dt: datetime, now: datetime) -> str:
        seconds = max(0, int((now - dt).total_seconds()))
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        if days:
            return f"{days}д {hours}ч назад"
        if hours:
            return f"{hours}ч {minutes}м назад"
        return f"{minutes}м назад"

    def _format_recent(self) -> str:
        polls = self.repository.recent_poll_summaries(limit=10)
        lines = ["Последние вопросы Квизи:"]
        if not polls:
            lines.append("- пока вопросов нет")
            return "\n".join(lines)

        for poll in polls:
            answers = poll["answers"]
            correct_count = sum(1 for answer in answers if int(answer["is_correct"]))
            wrong_count = len(answers) - correct_count
            answer_text = "ответов нет"
            if answers:
                names = [self._display_name(answer) for answer in answers[:4]]
                if len(answers) > 4:
                    names.append(f"ещё {len(answers) - 4}")
                answer_text = (
                    f"ответов {len(answers)}, верно/ошибки {correct_count}/{wrong_count}; "
                    f"{', '.join(names)}"
                )
            challenge = ""
            if int(poll.get("request_cost") or 0) > 0:
                challenge = f", challenge cost={poll['request_cost']} reward={poll['request_reward']}"
            lines.append(
                f"- {self._short_dt(str(poll['opened_at']))} | "
                f"{poll['topic_key']} {poll['difficulty']} | "
                f"{poll['status']} | {answer_text}{challenge}"
            )

        return "\n".join(lines)

    def _format_errors(self) -> str:
        events = self.repository.recent_error_events(limit=10)
        failed_cron = self.repository.recent_failed_cron_runs(limit=10)
        lines = ["Последние ошибки Квизи:"]

        if not events and not failed_cron:
            lines.append("- ошибок нет")
            return "\n".join(lines)

        transient_events = [
            event for event in events if self._is_transient_error_event(event)
        ]
        actionable_events = [
            event for event in events if not self._is_transient_error_event(event)
        ]
        lines.append(
            "Сводка: "
            f"transient Telegram/proxy={len(transient_events)}, "
            f"прочие events={len(actionable_events)}, "
            f"failed cron={len(failed_cron)}"
        )

        if actionable_events:
            lines.append("Требуют внимания:")
            for event in actionable_events[:5]:
                lines.append(
                    f"- {self._short_dt(str(event['created_at']))} | "
                    f"{event['source']}/{event['event']}: "
                    f"{self._compact_error_message(str(event['message']))}"
                )

        if transient_events:
            lines.append("Временные Telegram/proxy:")
            for event in transient_events[:5]:
                lines.append(
                    f"- {self._short_dt(str(event['created_at']))} | "
                    f"{event['source']}/{event['event']}: "
                    f"{self._compact_error_message(str(event['message']))}"
                )

        if failed_cron:
            lines.append("Cron:")
            for run in failed_cron:
                lines.append(
                    f"- {self._short_dt(str(run['finished_at']))} | "
                    f"{run['status']}: {self._compact_error_message(str(run['message']))}"
                )

        return "\n".join(lines)

    def _is_transient_error_event(self, event: dict[str, Any]) -> bool:
        source = str(event.get("source") or "").lower()
        message = str(event.get("message") or "")
        return source == "telegram" and self._looks_like_transient_telegram_error(message)

    def _looks_like_transient_telegram_error(self, message: str) -> bool:
        lower_message = message.lower()
        return any(marker in lower_message for marker in TRANSIENT_TELEGRAM_ERROR_MARKERS)

    def _compact_error_message(self, message: str, limit: int = 160) -> str:
        text = " ".join(message.strip().split())
        if self._looks_like_transient_telegram_error(text):
            details: list[str] = []
            lower_text = text.lower()
            if "503" in lower_text:
                details.append("503")
            if "proxy" in lower_text:
                details.append("proxy")
            if "max retries" in lower_text:
                details.append("after retries")
            if "timed out" in lower_text or "timeout" in lower_text:
                details.append("timeout")
            detail_text = ", ".join(dict.fromkeys(details)) or "network"
            return f"временный Telegram/proxy сбой ({detail_text})"

        text = re.sub(r"/bot[^/\s]+/", "/bot<token>/", text)
        text = re.sub(r"https?://\S+", "<url>", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > limit:
            return text[: limit - 3].rstrip() + "..."
        return text

    def _format_question_review(self) -> str:
        stats = self.repository.question_answer_stats()
        review_items: list[tuple[int, str]] = []
        questions_with_stats = 0

        for question in self.question_bank.questions:
            question_stats = stats.get(question.id, {})
            answers_count = int(question_stats.get("answers_count") or 0)
            correct_count = int(question_stats.get("correct_count") or 0)
            wrong_count = int(question_stats.get("wrong_count") or 0)
            asked_count = int(question_stats.get("asked_count") or 0)
            if answers_count:
                questions_with_stats += 1

            issues: list[str] = []
            severity = 0
            if answers_count >= 3 and correct_count == 0:
                issues.append("0% правильных при 3+ ответах")
                severity += 100 + answers_count
            if answers_count >= 5 and correct_count == answers_count:
                issues.append("100% правильных при 5+ ответах")
                severity += 80 + answers_count
            if not question.explanation.strip():
                issues.append("нет explanation")
                severity += 15
            if not question.source.strip():
                issues.append("нет source")
                severity += 10

            if not issues:
                continue

            stats_text = "нет ответов"
            if answers_count:
                percent = round(correct_count * 100 / answers_count)
                stats_text = f"{correct_count}/{answers_count} верно ({percent}%), ошибок {wrong_count}"
            asked_text = f", задан {asked_count} раз" if asked_count else ""
            review_items.append(
                (
                    severity,
                    (
                        f"- {question.id} | {question.topic_key} {question.difficulty} | "
                        f"{stats_text}{asked_text} | {'; '.join(issues)}"
                    ),
                )
            )

        lines = ["Ревизия вопросов:"]
        if not review_items:
            lines.append(
                "Проблем не найдено по текущим порогам: 0% при 3+ ответах, "
                "100% при 5+ ответах, пустые explanation/source."
            )
            lines.append(
                f"Статистика: questions={self.question_bank.count()}, "
                f"со статистикой={questions_with_stats}."
            )
            return "\n".join(lines)

        review_items.sort(key=lambda item: item[0], reverse=True)
        for _, line in review_items[:15]:
            lines.append(line)
        if len(review_items) > 15:
            lines.append(f"- ... ещё {len(review_items) - 15}")
        lines.append(
            f"Итого сигналов: {len(review_items)} из {self.question_bank.count()} вопросов; "
            f"со статистикой={questions_with_stats}."
        )
        return "\n".join(lines)

    def _format_questions_status(self) -> str:
        duplicate_ids = find_duplicate_ids(self.settings.questions_path)
        try:
            bank = load_questions(self.settings.questions_path)
        except QuestionValidationError as exc:
            lines = [
                "Статус questions.csv:",
                f"Questions ERROR: {exc}",
            ]
            if duplicate_ids:
                lines.append(f"Duplicate ids: {', '.join(duplicate_ids)}")
            return "\n".join(lines)

        bound_topics = {str(topic["topic_key"]) for topic in self.repository.list_topics()}
        lines, warnings = build_report(bank, duplicate_ids, bound_topics)
        return format_report_for_telegram(lines, warnings)

    def _send_questions_template(self, message: dict[str, Any], args: list[str]) -> bool:
        difficulties = [arg.strip().lower() for arg in args if arg.strip()]
        if not difficulties:
            difficulties = ["easy", "normal", "hard"]

        invalid = [
            difficulty
            for difficulty in difficulties
            if not DIFFICULTY_PATTERN.match(difficulty)
        ]
        if invalid:
            self._reply(
                message,
                "Некорректная сложность для шаблона: "
                f"{', '.join(invalid)}. Используй slug вроде easy, hard, ccna.",
            )
            return False

        topics = self._template_topic_keys()
        if not topics:
            self._reply(
                message,
                "Нет топиков для шаблона. Сначала привяжи топик через /kvizi_bind или добавь вопросы в CSV.",
            )
            return False

        content = self._build_questions_template_csv(topics, difficulties)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"questions-template-{timestamp}.csv"
        caption = (
            "Шаблон questions.csv: "
            f"topics={len(topics)}, difficulties={', '.join(difficulties)}. "
            "Заполни question/options/correct_option_ids и проверь через /kvizi_upload_questions --check."
        )
        thread_id = message.get("message_thread_id")
        chat_id = str((message.get("chat") or {}).get("id") or self.settings.telegram_chat_id)
        self.telegram.send_document(
            chat_id=chat_id,
            message_thread_id=int(thread_id) if thread_id is not None else None,
            filename=filename,
            content=content,
            caption=caption,
            mime_type="text/csv",
        )
        return True

    def _template_topic_keys(self) -> list[str]:
        bound_topics = [
            str(topic["topic_key"])
            for topic in self.repository.active_topics()
        ]
        if bound_topics:
            return sorted(set(bound_topics))
        return sorted(self.question_bank.topics())

    def _build_questions_template_csv(self, topics: list[str], difficulties: list[str]) -> bytes:
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=QUESTION_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for topic_key in topics:
            for difficulty in difficulties:
                writer.writerow(
                    {
                        "id": f"{topic_key}_{difficulty}_001",
                        "topic_key": topic_key,
                        "difficulty": difficulty,
                        "question": "",
                        "option_1": "",
                        "option_2": "",
                        "option_3": "",
                        "option_4": "",
                        "option_5": "",
                        "option_6": "",
                        "correct_option_ids": "",
                        "explanation": "",
                        "source": "",
                    }
                )
        return buffer.getvalue().encode("utf-8-sig")

    def _handle_upload_questions(self, message: dict[str, Any], *, check_only: bool = False) -> bool:
        document = message.get("document") or {}
        file_id = str(document.get("file_id") or "")
        filename = str(document.get("file_name") or "")
        file_size = int(document.get("file_size") or 0)

        if not file_id:
            self._reply(
                message,
                "Прикрепи CSV документом и добавь caption: /kvizi_upload_questions",
            )
            return False
        if filename and not filename.lower().endswith(".csv"):
            self._reply(message, f"Это не похоже на CSV: {filename}")
            return False
        if file_size > MAX_QUESTIONS_UPLOAD_BYTES:
            self._reply(
                message,
                f"CSV слишком большой: {file_size} bytes. Лимит {MAX_QUESTIONS_UPLOAD_BYTES}.",
            )
            return False

        try:
            content = self.telegram.download_file(file_id)
        except TelegramApiError as exc:
            self._reply(message, f"Не удалось скачать CSV: {exc}")
            return False

        if len(content) > MAX_QUESTIONS_UPLOAD_BYTES:
            self._reply(
                message,
                f"CSV слишком большой после скачивания: {len(content)} bytes. Лимит {MAX_QUESTIONS_UPLOAD_BYTES}.",
            )
            return False

        temp_path = self._questions_upload_temp_path()
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(content)

        duplicate_ids = find_duplicate_ids(temp_path)
        try:
            uploaded_bank = load_questions(temp_path)
        except (QuestionValidationError, UnicodeDecodeError) as exc:
            temp_path.unlink(missing_ok=True)
            lines = [
                "Questions ERROR: новый CSV не принят.",
                str(exc),
                "Текущий questions.csv не заменён.",
            ]
            if duplicate_ids:
                lines.append(f"Duplicate ids: {', '.join(duplicate_ids)}")
            self._reply(message, "\n".join(lines))
            return False

        if uploaded_bank.count() == 0:
            temp_path.unlink(missing_ok=True)
            self._reply(message, "Questions ERROR: новый CSV пустой. Текущий questions.csv не заменён.")
            return False

        bound_topics = {str(topic["topic_key"]) for topic in self.repository.list_topics()}
        lines, warnings = build_report(uploaded_bank, duplicate_ids, bound_topics)
        report = format_report_for_telegram(lines, warnings)

        if check_only:
            temp_path.unlink(missing_ok=True)
            self._reply(
                message,
                f"Проверка questions.csv пройдена: {uploaded_bank.count()} вопросов.\n"
                "Файл не заменён. Для применения отправь без --check.\n\n"
                f"{report}",
            )
            return True

        try:
            backup_path = self._backup_questions_file()
            temp_path.replace(self.settings.questions_path)
            self.question_bank = load_questions(self.settings.questions_path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            self._reply(message, f"Не удалось заменить questions.csv: {exc}")
            return False

        prefix = [
            f"questions.csv обновлён: {self.question_bank.count()} вопросов.",
        ]
        if backup_path is not None:
            prefix.append(f"Backup: {self._display_questions_path(backup_path)}")
        self._reply(message, "\n".join(prefix) + "\n\n" + report)
        return True

    def _questions_upload_temp_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self.settings.questions_path.with_name(
            f".{self.settings.questions_path.name}.upload-{timestamp}.tmp"
        )

    def _backup_questions_file(self) -> Path | None:
        if not self.settings.questions_path.exists():
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = self._questions_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"questions-{timestamp}.csv"
        shutil.copy2(self.settings.questions_path, backup_path)
        return backup_path

    def _format_question_backups(self) -> str:
        backups = self._list_question_backups()
        if not backups:
            return "Backups questions.csv: нет. Они появятся после /kvizi_upload_questions."

        lines = ["Backups questions.csv:"]
        for index, path in enumerate(backups, start=1):
            lines.append(f"{index}. {path.name}")
        lines.append("Восстановить: /kvizi_restore_questions <номер>")
        return "\n".join(lines)

    def _handle_restore_questions(self, args: list[str], message: dict[str, Any]) -> bool:
        backups = self._list_question_backups()
        if not backups:
            self._reply(message, "Backups questions.csv: нет файлов для восстановления.")
            return False
        if not args:
            self._reply(message, "Формат: /kvizi_restore_questions <номер из /kvizi_backups>")
            return False

        try:
            backup_index = int(args[0])
        except ValueError:
            self._reply(message, "Номер backup должен быть целым числом.")
            return False

        if backup_index < 1 or backup_index > len(backups):
            self._reply(message, f"Backup #{backup_index} не найден. Доступно: 1-{len(backups)}.")
            return False

        selected_backup = backups[backup_index - 1]
        duplicate_ids = find_duplicate_ids(selected_backup)
        try:
            backup_bank = load_questions(selected_backup)
        except (QuestionValidationError, UnicodeDecodeError) as exc:
            lines = [
                f"Backup #{backup_index} повреждён, восстановление отменено.",
                str(exc),
            ]
            if duplicate_ids:
                lines.append(f"Duplicate ids: {', '.join(duplicate_ids)}")
            self._reply(message, "\n".join(lines))
            return False

        if backup_bank.count() == 0:
            self._reply(message, f"Backup #{backup_index} пустой, восстановление отменено.")
            return False

        temp_path = self._questions_upload_temp_path()
        try:
            current_backup = self._backup_questions_file()
            shutil.copy2(selected_backup, temp_path)
            temp_path.replace(self.settings.questions_path)
            self.question_bank = load_questions(self.settings.questions_path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            self._reply(message, f"Не удалось восстановить questions.csv: {exc}")
            return False

        bound_topics = {str(topic["topic_key"]) for topic in self.repository.list_topics()}
        lines, warnings = build_report(self.question_bank, duplicate_ids, bound_topics)
        prefix = [
            f"questions.csv восстановлен из backup #{backup_index}: {selected_backup.name}.",
        ]
        if current_backup is not None:
            prefix.append(f"Backup текущего файла: {self._display_questions_path(current_backup)}")
        report = format_report_for_telegram(lines, warnings)
        self._reply(message, "\n".join(prefix) + "\n\n" + report)
        return True

    def _list_question_backups(self, limit: int = 10) -> list[Path]:
        backup_dir = self._questions_backup_dir()
        if not backup_dir.exists():
            return []
        return sorted(
            backup_dir.glob("questions-*.csv"),
            key=lambda path: path.name,
            reverse=True,
        )[:limit]

    def _questions_backup_dir(self) -> Path:
        return self.settings.questions_path.parent / "backups"

    def _display_questions_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.settings.questions_path.parent).as_posix()
        except ValueError:
            return str(path)

    def _topic_counts(self, polls: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for poll in polls:
            topic_key = str(poll["topic_key"])
            counts[topic_key] = counts.get(topic_key, 0) + 1
        return ", ".join(f"{topic_key}={count}" for topic_key, count in sorted(counts.items()))

    def _daily_window(self) -> tuple[str, str, str]:
        now = datetime.now(self.settings.timezone)
        start_local = datetime.combine(now.date(), time.min, tzinfo=self.settings.timezone)
        end_local = start_local + timedelta(days=1)
        return (
            now.date().isoformat(),
            start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat(),
        )

    def _format_daily_summary(self, summary_date: str, stats: dict[str, Any]) -> str:
        lines = [
            copy.daily_title(self._short_date(summary_date), self.rng.choice),
            f"Вопросы: {stats['questions_count']}",
            (
                f"Ответы: {stats['answers_count']} от {stats['participants_count']} участников. "
                f"Верно/ошибки: {stats['correct_count']}/{stats['wrong_count']}."
            ),
            f"Очки за день: {self._signed(int(stats['points_delta']))}",
            f"Challenge: {stats['challenge_count']} запусков, пройдено {stats['challenge_wins']}.",
        ]

        top_players = stats["top_players"]
        lines.append(copy.daily_top_header(self.rng.choice))
        if top_players:
            for index, row in enumerate(top_players, start=1):
                lines.append(
                    f"{index}. {self._display_name(row)} — {self._signed(int(row['points_delta']))} "
                    f"({row['correct_count']}/{row['answers_count']} верно/ответов)"
                )
        else:
            lines.append(copy.daily_empty_top(self.rng.choice))

        challenge_players = stats["challenge_players"]
        if challenge_players:
            lines.append(copy.daily_challenge_header(self.rng.choice))
            for index, row in enumerate(challenge_players, start=1):
                lines.append(
                    f"{index}. {self._display_name(row)} — {row['challenge_count']} выз., "
                    f"{row['challenge_wins']} пройдено, {self._signed(int(row['challenge_delta']))}"
                )

        risky_players = stats["risky_players"]
        if risky_players:
            lines.append(copy.daily_risk_header(self.rng.choice))
            for index, row in enumerate(risky_players, start=1):
                lines.append(
                    f"{index}. {self._display_name(row)} — {row['risky_answers']} ставок, "
                    f"{self._signed(int(row['risk_delta']))}"
                )

        season_top = self.repository.leaderboard(self.settings.season_name, limit=1)
        if season_top:
            leader = season_top[0]
            lines.append(
                copy.season_leader_line(
                    self._display_name(leader),
                    int(leader["points"]),
                    self.rng.choice,
                )
            )
        else:
            lines.append(copy.no_season_leader_line(self.rng.choice))

        return "\n".join(lines)

    def _signed(self, value: int) -> str:
        return f"+{value}" if value > 0 else str(value)

    def _short_date(self, value: str) -> str:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.settings.timezone)
        timezone_label = dt.astimezone(self.settings.timezone).tzname() or self.settings.timezone_name
        return f"{dt:%d.%m.%Y} {timezone_label}"

    def _short_dt(self, value: str) -> str:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value.replace("T", " ").split("+", 1)[0].split(".", 1)[0]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(self.settings.timezone)
        timezone_label = local_dt.tzname() or self.settings.timezone_name
        return f"{local_dt:%d.%m.%Y %H:%M:%S} {timezone_label}"

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
        title = copy.poll_title(question.text, self.rng.choice)
        return title if len(title) <= 300 else question.text[:300]

    def _announce_question(self, route: TopicRoute, question: Question, question_link: str | None) -> None:
        if not question_link:
            return

        self._send_announcement(
            text=copy.question_announcement(
                route.topic_key,
                question.difficulty,
                question_link,
                base_points(question.difficulty, self.settings.difficulty_points),
                self.rng.choice,
            ),
            event="question_announcement_failed",
        )

    def _announce_no_answers_closed(self, poll: dict[str, Any]) -> None:
        if not self.settings.announce_no_answers:
            return

        announce_thread_id = self._announce_thread_id()
        question_link = self._message_link(int(poll["telegram_message_id"]))
        if announce_thread_id is None or not question_link:
            return

        self._send_announcement(
            text=copy.no_answers_closed(
                topic_key=str(poll["topic_key"]),
                difficulty=str(poll["difficulty"]),
                link=question_link,
                chooser=self.rng.choice,
            ),
            event="no_answers_announcement_failed",
            message_thread_id=announce_thread_id,
        )

    def _announce_thread_id(self) -> int | None:
        if self.settings.announce_thread_id is not None:
            return self.settings.announce_thread_id
        value = self.repository.get_bot_setting("announce_thread_id")
        return int(value) if value else None

    def _send_announcement(
        self,
        *,
        text: str,
        event: str,
        message_thread_id: int | None = None,
    ) -> bool:
        thread_id = self._announce_thread_id() if message_thread_id is None else message_thread_id
        if thread_id is None:
            return False

        try:
            self.telegram.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=text,
                disable_notification=True,
            )
        except TelegramApiError as exc:
            self.repository.record_error_event(
                source="telegram",
                event=event,
                message=str(exc),
            )
            return False
        return True

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
        return copy.score_event_text(
            name=self._user_event_name(user),
            is_challenge=bool(result.is_challenge),
            is_correct=bool(result.is_correct),
            stake=int(result.stake),
            delta=int(result.delta),
            points=int(result.points),
            streak_bonus=int(result.streak_bonus),
            chooser=self.rng.choice,
        )

    def _announce_first_answer_of_day(
        self,
        user: dict[str, Any],
        result: Any,
        poll: dict[str, Any] | None,
    ) -> None:
        if not self.settings.announce_first_answer:
            return

        if poll is None:
            return

        _summary_date, start_iso, end_iso = self._daily_window()
        if self.repository.human_answer_count_between(start_iso, end_iso) != 1:
            return

        announce_thread_id = self._announce_thread_id()
        if announce_thread_id is None:
            return

        self._send_announcement(
            text=copy.first_answer_of_day(
                name=self._user_event_name(user),
                topic_key=str(poll["topic_key"]),
                difficulty=str(poll["difficulty"]),
                is_correct=bool(result.is_correct),
                delta=int(result.delta),
                points=int(result.points),
                chooser=self.rng.choice,
            ),
            event="first_answer_announcement_failed",
            message_thread_id=announce_thread_id,
        )

    def _current_season_leader(self) -> dict[str, Any] | None:
        rows = self.repository.leaderboard(self.settings.season_name, limit=1)
        return rows[0] if rows else None

    def _announce_season_leader_change(self, previous_leader: dict[str, Any] | None) -> None:
        if previous_leader is None:
            return

        current_leader = self._current_season_leader()
        if current_leader is None:
            return
        if int(current_leader["user_id"]) == int(previous_leader["user_id"]):
            return
        if int(current_leader["points"]) <= 0:
            return

        announce_thread_id = self._announce_thread_id()
        if announce_thread_id is None:
            return

        self._send_announcement(
            text=copy.season_leader_change(
                new_name=self._display_name(current_leader),
                old_name=self._display_name(previous_leader),
                points=int(current_leader["points"]),
                chooser=self.rng.choice,
            ),
            event="leader_announcement_failed",
            message_thread_id=announce_thread_id,
        )

    def _announce_risk_failure(self, user: dict[str, Any], result: Any) -> None:
        if not self.settings.announce_risk_failures:
            return

        if bool(result.is_correct) or int(result.stake) <= 1:
            return

        announce_thread_id = self._announce_thread_id()
        if announce_thread_id is None:
            return

        self._send_announcement(
            text=copy.risk_failure(
                name=self._user_event_name(user),
                stake=int(result.stake),
                delta=int(result.delta),
                points=int(result.points),
                chooser=self.rng.choice,
            ),
            event="risk_failure_announcement_failed",
            message_thread_id=announce_thread_id,
        )

    def _announce_streak_milestone(self, user: dict[str, Any], result: Any) -> None:
        if not self.settings.announce_streaks:
            return

        if int(result.streak_bonus) <= 0:
            return

        announce_thread_id = self._announce_thread_id()
        if announce_thread_id is None:
            return

        self._send_announcement(
            text=copy.streak_milestone(
                name=self._user_event_name(user),
                streak=int(result.streak),
                bonus=int(result.streak_bonus),
                points=int(result.points),
                chooser=self.rng.choice,
            ),
            event="streak_announcement_failed",
            message_thread_id=announce_thread_id,
        )

    def _user_event_name(self, user: dict[str, Any]) -> str:
        return str(user.get("first_name") or user.get("username") or user.get("id"))
