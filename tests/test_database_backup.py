from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from kvizi.database import KviziRepository
from kvizi.database_backup import (
    DatabaseBackupError,
    backup_database,
    create_database_backup,
    inspect_database,
    restore_database,
)


def test_create_database_backup_contains_complete_valid_state(tmp_path: Path) -> None:
    database_path = tmp_path / "source.sqlite3"
    repository = KviziRepository(database_path)
    repository.init_db()
    repository.upsert_user(
        {"id": 7, "username": "admin", "first_name": "Ada", "last_name": "L"}
    )
    repository.bind_topic("network", 101, 2, "Network")

    artifact = create_database_backup(database_path)

    snapshot_path = tmp_path / "snapshot.sqlite3"
    snapshot_path.write_bytes(artifact.content)
    inspection = inspect_database(snapshot_path)
    with sqlite3.connect(snapshot_path) as connection:
        user = connection.execute(
            "SELECT username, first_name FROM users WHERE user_id = 7"
        ).fetchone()
        topic = connection.execute(
            "SELECT message_thread_id, weight FROM topics WHERE topic_key = 'network'"
        ).fetchone()

    assert artifact.content.startswith(b"SQLite format 3\x00")
    assert artifact.size_bytes == len(artifact.content)
    assert artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()
    assert inspection.sha256 == artifact.sha256
    assert inspection.table_counts["users"] == 1
    assert inspection.table_counts["topics"] == 1
    assert user == ("admin", "Ada")
    assert topic == (101, 2)


def test_restore_database_replaces_state_and_preserves_previous_database(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.sqlite3"
    source_repository = KviziRepository(source_path)
    source_repository.init_db()
    source_repository.upsert_user(
        {"id": 1, "username": "restored", "first_name": "Restore"}
    )
    source_repository.bind_topic("security", 202, 3, "Security")
    source_repository.create_poll(
        poll_id="poll-restore",
        telegram_message_id=303,
        question_id="question-restore",
        topic_key="security",
        message_thread_id=202,
        correct_option_id=1,
        difficulty="normal",
        opened_at="2026-07-22T10:00:00+00:00",
        closes_at="2026-07-22T12:00:00+00:00",
        explanation="Restore test",
    )
    answer = source_repository.record_answer(
        season="main",
        poll_id="poll-restore",
        user={"id": 1, "username": "restored", "first_name": "Restore"},
        option_ids=[1],
        now_iso="2026-07-22T10:05:00+00:00",
    )
    assert answer.recorded is True
    backup_path = tmp_path / "downloaded-backup.sqlite3"
    backup_database(source_path, backup_path)

    target_path = tmp_path / "target.sqlite3"
    target_repository = KviziRepository(target_path)
    target_repository.init_db()
    target_repository.upsert_user(
        {"id": 2, "username": "previous", "first_name": "Previous"}
    )

    result = restore_database(
        backup_path,
        target_path,
        pre_restore_backup_dir=tmp_path / "pre-restore",
    )

    assert result.database_path == target_path.resolve()
    assert result.pre_restore_backup_path is not None
    assert result.pre_restore_backup_path.is_file()
    assert result.inspection.table_counts["users"] == 1
    assert result.inspection.table_counts["polls"] == 1
    assert result.inspection.table_counts["answers"] == 1
    assert result.inspection.table_counts["scores"] == 1
    with sqlite3.connect(target_path) as connection:
        restored_users = connection.execute(
            "SELECT user_id, username FROM users ORDER BY user_id"
        ).fetchall()
        restored_topics = connection.execute(
            "SELECT topic_key, message_thread_id FROM topics"
        ).fetchall()
        restored_answers = connection.execute(
            "SELECT poll_id, user_id, is_correct FROM answers"
        ).fetchall()
        restored_scores = connection.execute(
            "SELECT user_id, points, answered_count FROM scores"
        ).fetchall()
    with sqlite3.connect(result.pre_restore_backup_path) as connection:
        previous_users = connection.execute(
            "SELECT user_id, username FROM users ORDER BY user_id"
        ).fetchall()

    assert restored_users == [(1, "restored")]
    assert restored_topics == [("security", 202)]
    assert restored_answers == [("poll-restore", 1, 1)]
    assert restored_scores == [(1, 10, 1)]
    assert previous_users == [(2, "previous")]


def test_restore_rejects_invalid_backup_without_changing_target(tmp_path: Path) -> None:
    target_path = tmp_path / "target.sqlite3"
    target_repository = KviziRepository(target_path)
    target_repository.init_db()
    target_repository.upsert_user(
        {"id": 9, "username": "untouched", "first_name": "Safe"}
    )
    invalid_backup = tmp_path / "invalid.sqlite3"
    invalid_backup.write_bytes(b"not a sqlite database")

    with pytest.raises(DatabaseBackupError, match="Invalid SQLite database"):
        restore_database(
            invalid_backup,
            target_path,
            pre_restore_backup_dir=tmp_path / "pre-restore",
        )

    with sqlite3.connect(target_path) as connection:
        users = connection.execute(
            "SELECT user_id, username FROM users ORDER BY user_id"
        ).fetchall()
    assert users == [(9, "untouched")]
    assert not (tmp_path / "pre-restore").exists()
