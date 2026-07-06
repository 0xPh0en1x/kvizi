from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from kvizi.scoring import ScoreInput, ScoreResult, calculate_score

SQLITE_BUSY_TIMEOUT_MS = 5000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


@dataclass(frozen=True)
class AnswerResult:
    recorded: bool
    is_correct: bool
    delta: int
    points: int
    streak: int
    streak_bonus: int
    stake: int
    reason: str = ""
    is_challenge: bool = False


class KviziRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_db(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA_SQL)
            self._migrate_poll_columns(connection)

    def _migrate_poll_columns(self, connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(polls)").fetchall()
        }
        columns = {
            "requested_by": "INTEGER",
            "request_cost": "INTEGER NOT NULL DEFAULT 0",
            "request_reward": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE polls ADD COLUMN {name} {definition}")

    def upsert_user(self, user: dict[str, Any]) -> None:
        user_id = user.get("id")
        if user_id is None:
            return
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    last_name = COALESCE(excluded.last_name, users.last_name),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    int(user_id),
                    user.get("username"),
                    user.get("first_name"),
                    user.get("last_name"),
                    utc_now_iso(),
                ),
            )

    def bind_topic(self, topic_key: str, message_thread_id: int, weight: int, title: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO topics (topic_key, message_thread_id, weight, title, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    message_thread_id = excluded.message_thread_id,
                    weight = excluded.weight,
                    title = excluded.title,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (topic_key, message_thread_id, weight, title, utc_now_iso(), utc_now_iso()),
            )

    def list_topics(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM topics ORDER BY active DESC, topic_key ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def active_topics(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT topic_key, message_thread_id, weight, title
                FROM topics
                WHERE active = 1 AND weight > 0
                ORDER BY topic_key ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_topic_by_thread(self, message_thread_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT topic_key, message_thread_id, weight, title
                FROM topics
                WHERE active = 1 AND message_thread_id = ?
                ORDER BY topic_key ASC
                LIMIT 1
                """,
                (message_thread_id,),
            ).fetchone()
        return _row_to_dict(row)

    def get_last_topic_key(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT topic_key FROM polls ORDER BY opened_at DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row["topic_key"])

    def asked_question_ids(self, topic_key: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT question_id FROM question_history WHERE topic_key = ?",
                (topic_key,),
            ).fetchall()
        return {str(row["question_id"]) for row in rows}

    def reset_question_history(self, topic_key: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM question_history WHERE topic_key = ?", (topic_key,))

    def mark_question_asked(self, topic_key: str, question_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO question_history (topic_key, question_id, asked_at)
                VALUES (?, ?, ?)
                """,
                (topic_key, question_id, utc_now_iso()),
            )

    def create_poll(
        self,
        *,
        poll_id: str,
        telegram_message_id: int,
        question_id: str,
        topic_key: str,
        message_thread_id: int,
        correct_option_id: int,
        difficulty: str,
        opened_at: str,
        closes_at: str,
        explanation: str,
        requested_by: int | None = None,
        request_cost: int = 0,
        request_reward: int = 0,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO polls (
                    poll_id, telegram_message_id, question_id, topic_key, message_thread_id,
                    correct_option_id, difficulty, opened_at, closes_at, closed_at, status, explanation,
                    requested_by, request_cost, request_reward
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?, ?, ?)
                """,
                (
                    poll_id,
                    telegram_message_id,
                    question_id,
                    topic_key,
                    message_thread_id,
                    correct_option_id,
                    difficulty,
                    opened_at,
                    closes_at,
                    explanation,
                    requested_by,
                    request_cost,
                    request_reward,
                ),
            )

    def get_poll(self, poll_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM polls WHERE poll_id = ?", (poll_id,)).fetchone()
        return _row_to_dict(row)

    def expired_active_polls(self, now_iso: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM polls
                WHERE status = 'active' AND closes_at <= ?
                ORDER BY closes_at ASC
                """,
                (now_iso,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_poll_topic_keys(self, now_iso: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT topic_key
                FROM polls
                WHERE status = 'active' AND closes_at > ?
                """,
                (now_iso,),
            ).fetchall()
        return {str(row["topic_key"]) for row in rows}

    def active_polls(self, now_iso: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.*,
                    u.username AS requester_username,
                    u.first_name AS requester_first_name,
                    u.last_name AS requester_last_name,
                    CASE
                        WHEN p.requested_by IS NOT NULL AND a.user_id IS NOT NULL THEN 1
                        ELSE 0
                    END AS requester_answered
                FROM polls AS p
                LEFT JOIN users AS u ON u.user_id = p.requested_by
                LEFT JOIN answers AS a
                    ON a.poll_id = p.poll_id
                   AND a.user_id = p.requested_by
                WHERE p.status = 'active' AND p.closes_at > ?
                ORDER BY p.closes_at ASC
                """,
                (now_iso,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_polls_for_thread(self, message_thread_id: int, now_iso: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.*,
                    u.username AS requester_username,
                    u.first_name AS requester_first_name,
                    u.last_name AS requester_last_name,
                    CASE
                        WHEN p.requested_by IS NOT NULL AND a.user_id IS NOT NULL THEN 1
                        ELSE 0
                    END AS requester_answered
                FROM polls AS p
                LEFT JOIN users AS u ON u.user_id = p.requested_by
                LEFT JOIN answers AS a
                    ON a.poll_id = p.poll_id
                   AND a.user_id = p.requested_by
                WHERE p.status = 'active'
                  AND p.closes_at > ?
                  AND p.message_thread_id = ?
                ORDER BY p.closes_at ASC
                """,
                (now_iso, message_thread_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_poll_closed(self, poll_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE polls SET status = 'closed', closed_at = ? WHERE poll_id = ?",
                (utc_now_iso(), poll_id),
            )

    def record_bet(self, poll_id: str, user_id: int, stake: int, now_iso: str) -> tuple[bool, str]:
        poll = self.get_poll(poll_id)
        if poll is None:
            return False, "опрос не найден"
        if poll["status"] != "active" or poll["closes_at"] <= now_iso:
            return False, "время вышло"
        if poll.get("requested_by") is not None and int(poll["requested_by"]) == user_id:
            return False, "для вызова ставка уже выбрана"
        if stake not in (2, 3):
            return False, "можно выбрать только x2 или x3"

        with self.connect() as connection:
            existing_answer = connection.execute(
                "SELECT 1 FROM answers WHERE poll_id = ? AND user_id = ?",
                (poll_id, user_id),
            ).fetchone()
            if existing_answer is not None:
                return False, "ответ уже зафиксирован"

            connection.execute(
                """
                INSERT INTO bets (poll_id, user_id, stake, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(poll_id, user_id) DO UPDATE SET
                    stake = excluded.stake,
                    created_at = excluded.created_at
                """,
                (poll_id, user_id, stake, now_iso),
            )
        return True, "ok"

    def get_score(self, season: str, user_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scores WHERE season = ? AND user_id = ?",
                (season, user_id),
            ).fetchone()
        if row is not None:
            return dict(row)
        return {
            "season": season,
            "user_id": user_id,
            "points": 0,
            "current_streak": 0,
            "best_streak": 0,
            "correct_count": 0,
            "wrong_count": 0,
            "answered_count": 0,
        }

    def leaderboard(self, season: str, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    scores.*,
                    users.username,
                    users.first_name,
                    users.last_name
                FROM scores
                LEFT JOIN users ON users.user_id = scores.user_id
                WHERE scores.season = ?
                ORDER BY scores.points DESC, scores.correct_count DESC, scores.updated_at ASC
                LIMIT ?
                """,
                (season, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def daily_summary(self, start_iso: str, end_iso: str, limit: int = 3) -> dict[str, Any]:
        with self.connect() as connection:
            totals = dict(
                connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM polls WHERE opened_at >= ? AND opened_at < ?) AS questions_count,
                        COUNT(a.poll_id) AS answers_count,
                        COUNT(DISTINCT a.user_id) AS participants_count,
                        COALESCE(SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END), 0) AS correct_count,
                        COALESCE(SUM(CASE WHEN a.is_correct = 0 THEN 1 ELSE 0 END), 0) AS wrong_count,
                        COALESCE(SUM(a.points_delta), 0) AS points_delta
                    FROM answers AS a
                    WHERE a.answered_at >= ? AND a.answered_at < ?
                    """,
                    (start_iso, end_iso, start_iso, end_iso),
                ).fetchone()
            )
            challenge_totals = dict(
                connection.execute(
                    """
                    SELECT
                        COUNT(*) AS challenge_count,
                        COALESCE(SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END), 0) AS challenge_wins
                    FROM polls AS p
                    LEFT JOIN answers AS a
                      ON a.poll_id = p.poll_id
                     AND a.user_id = p.requested_by
                    WHERE p.opened_at >= ?
                      AND p.opened_at < ?
                      AND p.request_cost > 0
                    """,
                    (start_iso, end_iso),
                ).fetchone()
            )
            top_players = connection.execute(
                """
                SELECT
                    a.user_id,
                    u.username,
                    u.first_name,
                    u.last_name,
                    COALESCE(SUM(a.points_delta), 0) AS points_delta,
                    COALESCE(SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END), 0) AS correct_count,
                    COUNT(*) AS answers_count
                FROM answers AS a
                LEFT JOIN users AS u ON u.user_id = a.user_id
                WHERE a.answered_at >= ? AND a.answered_at < ?
                GROUP BY a.user_id
                ORDER BY points_delta DESC, correct_count DESC, answers_count DESC, a.user_id ASC
                LIMIT ?
                """,
                (start_iso, end_iso, limit),
            ).fetchall()
            risky_players = connection.execute(
                """
                SELECT
                    a.user_id,
                    u.username,
                    u.first_name,
                    u.last_name,
                    COUNT(*) AS risky_answers,
                    COALESCE(SUM(a.points_delta), 0) AS risk_delta
                FROM answers AS a
                LEFT JOIN users AS u ON u.user_id = a.user_id
                WHERE a.answered_at >= ?
                  AND a.answered_at < ?
                  AND a.stake > 1
                GROUP BY a.user_id
                ORDER BY risky_answers DESC, risk_delta DESC, a.user_id ASC
                LIMIT ?
                """,
                (start_iso, end_iso, limit),
            ).fetchall()
            challenge_players = connection.execute(
                """
                SELECT
                    p.requested_by AS user_id,
                    u.username,
                    u.first_name,
                    u.last_name,
                    COUNT(*) AS challenge_count,
                    COALESCE(SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END), 0) AS challenge_wins,
                    COALESCE(SUM(a.points_delta), 0) AS challenge_delta
                FROM polls AS p
                LEFT JOIN answers AS a
                  ON a.poll_id = p.poll_id
                 AND a.user_id = p.requested_by
                LEFT JOIN users AS u ON u.user_id = p.requested_by
                WHERE p.opened_at >= ?
                  AND p.opened_at < ?
                  AND p.request_cost > 0
                GROUP BY p.requested_by
                ORDER BY challenge_count DESC, challenge_wins DESC, challenge_delta DESC, p.requested_by ASC
                LIMIT ?
                """,
                (start_iso, end_iso, limit),
            ).fetchall()

        return {
            **totals,
            **challenge_totals,
            "top_players": [dict(row) for row in top_players],
            "risky_players": [dict(row) for row in risky_players],
            "challenge_players": [dict(row) for row in challenge_players],
        }

    def has_active_challenge(self, user_id: int, now_iso: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM polls AS p
                LEFT JOIN answers AS a
                  ON a.poll_id = p.poll_id
                 AND a.user_id = p.requested_by
                WHERE p.requested_by = ?
                  AND p.request_cost > 0
                  AND p.status = 'active'
                  AND p.closes_at > ?
                  AND a.user_id IS NULL
                LIMIT 1
                """,
                (user_id, now_iso),
            ).fetchone()
        return row is not None

    def record_answer(
        self,
        *,
        season: str,
        poll_id: str,
        user: dict[str, Any],
        option_ids: list[int],
        now_iso: str,
    ) -> AnswerResult:
        user_id = int(user["id"])
        self.upsert_user(user)

        with self.connect() as connection:
            poll = connection.execute("SELECT * FROM polls WHERE poll_id = ?", (poll_id,)).fetchone()
            if poll is None:
                return AnswerResult(False, False, 0, 0, 0, 0, 1, "опрос не найден")

            existing = connection.execute(
                "SELECT * FROM answers WHERE poll_id = ? AND user_id = ?",
                (poll_id, user_id),
            ).fetchone()
            if existing is not None:
                score = self.get_score(season, user_id)
                return AnswerResult(
                    False,
                    bool(existing["is_correct"]),
                    int(existing["points_delta"]),
                    int(score["points"]),
                    int(score["current_streak"]),
                    int(existing["streak_bonus"]),
                    int(existing["stake"]),
                    "ответ уже учтен",
                )

            if poll["status"] != "active" or poll["closes_at"] <= now_iso:
                return AnswerResult(False, False, 0, 0, 0, 0, 1, "время вышло")

            stake_row = connection.execute(
                "SELECT stake FROM bets WHERE poll_id = ? AND user_id = ?",
                (poll_id, user_id),
            ).fetchone()
            stake = int(stake_row["stake"]) if stake_row is not None else 1
            is_correct = option_ids == [int(poll["correct_option_id"])]
            is_challenge = (
                poll["requested_by"] is not None
                and int(poll["requested_by"]) == user_id
                and int(poll["request_cost"]) > 0
            )

            score_row = connection.execute(
                "SELECT * FROM scores WHERE season = ? AND user_id = ?",
                (season, user_id),
            ).fetchone()
            current_points = int(score_row["points"]) if score_row is not None else 0
            current_streak = int(score_row["current_streak"]) if score_row is not None else 0

            if is_challenge:
                stake = 1
                new_streak = current_streak + 1 if is_correct else 0
                delta = int(poll["request_reward"]) if is_correct else -int(poll["request_cost"])
                score_result = ScoreResult(
                    delta=delta,
                    new_points=max(0, current_points + delta),
                    new_streak=new_streak,
                    streak_bonus=0,
                )
            else:
                score_result = calculate_score(
                    ScoreInput(
                        difficulty=str(poll["difficulty"]),
                        stake=stake,
                        is_correct=is_correct,
                        current_points=current_points,
                        current_streak=current_streak,
                    )
                )
            best_streak = max(
                score_result.new_streak,
                int(score_row["best_streak"]) if score_row is not None else 0,
            )
            correct_count = (int(score_row["correct_count"]) if score_row is not None else 0) + (
                1 if is_correct else 0
            )
            wrong_count = (int(score_row["wrong_count"]) if score_row is not None else 0) + (
                0 if is_correct else 1
            )
            answered_count = (int(score_row["answered_count"]) if score_row is not None else 0) + 1

            connection.execute(
                """
                INSERT INTO answers (
                    poll_id, user_id, selected_option_ids, is_correct, stake,
                    points_delta, streak_bonus, answered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    poll_id,
                    user_id,
                    ",".join(str(item) for item in option_ids),
                    1 if is_correct else 0,
                    stake,
                    score_result.delta,
                    score_result.streak_bonus,
                    now_iso,
                ),
            )
            connection.execute(
                """
                INSERT INTO scores (
                    season, user_id, points, current_streak, best_streak,
                    correct_count, wrong_count, answered_count, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(season, user_id) DO UPDATE SET
                    points = excluded.points,
                    current_streak = excluded.current_streak,
                    best_streak = excluded.best_streak,
                    correct_count = excluded.correct_count,
                    wrong_count = excluded.wrong_count,
                    answered_count = excluded.answered_count,
                    updated_at = excluded.updated_at
                """,
                (
                    season,
                    user_id,
                    score_result.new_points,
                    score_result.new_streak,
                    best_streak,
                    correct_count,
                    wrong_count,
                    answered_count,
                    now_iso,
                ),
            )

        return AnswerResult(
            True,
            is_correct,
            score_result.delta,
            score_result.new_points,
            score_result.new_streak,
            score_result.streak_bonus,
            stake,
            is_challenge=is_challenge,
        )

    def settle_unanswered_challenge(
        self,
        *,
        season: str,
        poll: dict[str, Any],
        now_iso: str,
    ) -> AnswerResult | None:
        requested_by = poll.get("requested_by")
        request_cost = int(poll.get("request_cost") or 0)
        if requested_by is None or request_cost <= 0:
            return None

        user_id = int(requested_by)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM answers WHERE poll_id = ? AND user_id = ?",
                (poll["poll_id"], user_id),
            ).fetchone()
            if existing is not None:
                return None

            score_row = connection.execute(
                "SELECT * FROM scores WHERE season = ? AND user_id = ?",
                (season, user_id),
            ).fetchone()
            current_points = int(score_row["points"]) if score_row is not None else 0
            best_streak = int(score_row["best_streak"]) if score_row is not None else 0
            correct_count = int(score_row["correct_count"]) if score_row is not None else 0
            wrong_count = (int(score_row["wrong_count"]) if score_row is not None else 0) + 1
            answered_count = (int(score_row["answered_count"]) if score_row is not None else 0) + 1
            delta = -request_cost
            new_points = max(0, current_points + delta)

            connection.execute(
                """
                INSERT INTO answers (
                    poll_id, user_id, selected_option_ids, is_correct, stake,
                    points_delta, streak_bonus, answered_at
                )
                VALUES (?, ?, '', 0, 1, ?, 0, ?)
                """,
                (poll["poll_id"], user_id, delta, now_iso),
            )
            connection.execute(
                """
                INSERT INTO scores (
                    season, user_id, points, current_streak, best_streak,
                    correct_count, wrong_count, answered_count, updated_at
                )
                VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(season, user_id) DO UPDATE SET
                    points = excluded.points,
                    current_streak = excluded.current_streak,
                    best_streak = excluded.best_streak,
                    correct_count = excluded.correct_count,
                    wrong_count = excluded.wrong_count,
                    answered_count = excluded.answered_count,
                    updated_at = excluded.updated_at
                """,
                (
                    season,
                    user_id,
                    new_points,
                    best_streak,
                    correct_count,
                    wrong_count,
                    answered_count,
                    now_iso,
                ),
            )

        return AnswerResult(
            recorded=True,
            is_correct=False,
            delta=delta,
            points=new_points,
            streak=0,
            streak_bonus=0,
            stake=1,
            reason="вызов истек",
            is_challenge=True,
        )

    def reset_season(self, season: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM scores WHERE season = ?", (season,))
        return int(cursor.rowcount)

    def record_cron_run(self, started_at: str, finished_at: str, status: str, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO cron_runs (started_at, finished_at, status, message)
                VALUES (?, ?, ?, ?)
                """,
                (started_at, finished_at, status, message),
            )

    def latest_cron_run(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cron_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _row_to_dict(row)

    def set_bot_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, utc_now_iso()),
            )

    def get_bot_setting(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def try_claim_update(self, update_id: int | None) -> bool:
        if update_id is None:
            return True
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO processed_updates (update_id, processed_at) VALUES (?, ?)",
                    (update_id, utc_now_iso()),
                )
        except sqlite3.IntegrityError:
            return False
        return True


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    topic_key TEXT PRIMARY KEY,
    message_thread_id INTEGER NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    poll_id TEXT PRIMARY KEY,
    telegram_message_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    message_thread_id INTEGER NOT NULL,
    correct_option_id INTEGER NOT NULL,
    difficulty TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    requested_by INTEGER,
    request_cost INTEGER NOT NULL DEFAULT 0,
    request_reward INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS answers (
    poll_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    selected_option_ids TEXT NOT NULL,
    is_correct INTEGER NOT NULL,
    stake INTEGER NOT NULL,
    points_delta INTEGER NOT NULL,
    streak_bonus INTEGER NOT NULL DEFAULT 0,
    answered_at TEXT NOT NULL,
    PRIMARY KEY (poll_id, user_id),
    FOREIGN KEY (poll_id) REFERENCES polls(poll_id)
);

CREATE TABLE IF NOT EXISTS bets (
    poll_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    stake INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (poll_id, user_id),
    FOREIGN KEY (poll_id) REFERENCES polls(poll_id)
);

CREATE TABLE IF NOT EXISTS scores (
    season TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    current_streak INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    wrong_count INTEGER NOT NULL DEFAULT 0,
    answered_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season, user_id)
);

CREATE TABLE IF NOT EXISTS question_history (
    topic_key TEXT NOT NULL,
    question_id TEXT NOT NULL,
    asked_at TEXT NOT NULL,
    PRIMARY KEY (topic_key, question_id)
);

CREATE TABLE IF NOT EXISTS cron_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_polls_status_closes_at ON polls(status, closes_at);
CREATE INDEX IF NOT EXISTS idx_scores_leaderboard ON scores(season, points DESC, correct_count DESC);
"""
