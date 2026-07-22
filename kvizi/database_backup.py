from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


SQLITE_BUSY_TIMEOUT_SECONDS = 5
REQUIRED_TABLES = (
    "users",
    "topics",
    "polls",
    "answers",
    "bets",
    "scores",
    "question_history",
    "cron_runs",
    "error_events",
    "processed_updates",
    "bot_settings",
    "operation_claims",
)


class DatabaseBackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseInspection:
    path: Path
    size_bytes: int
    sha256: str
    table_counts: dict[str, int]


@dataclass(frozen=True)
class DatabaseBackupArtifact:
    content: bytes
    size_bytes: int
    sha256: str
    table_counts: dict[str, int]


@dataclass(frozen=True)
class DatabaseRestoreResult:
    database_path: Path
    pre_restore_backup_path: Path | None
    inspection: DatabaseInspection


def create_database_backup(database_path: Path) -> DatabaseBackupArtifact:
    """Create a consistent SQLite snapshot, including committed WAL changes."""
    with TemporaryDirectory(prefix="kvizi-database-backup-") as temp_dir:
        snapshot_path = Path(temp_dir) / "kvizi.sqlite3"
        inspection = backup_database(database_path, snapshot_path)
        content = snapshot_path.read_bytes()
    return DatabaseBackupArtifact(
        content=content,
        size_bytes=inspection.size_bytes,
        sha256=inspection.sha256,
        table_counts=inspection.table_counts,
    )


def backup_database(source_path: Path, destination_path: Path) -> DatabaseInspection:
    """Write an integrity-checked SQLite snapshot and atomically publish it."""
    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    if source_path == destination_path:
        raise DatabaseBackupError("Source and destination database paths must differ")
    if not source_path.is_file():
        raise DatabaseBackupError(f"SQLite database not found: {source_path}")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_path.with_name(
        f".{destination_path.name}.{uuid4().hex}.tmp"
    )
    try:
        with closing(_connect_read_only(source_path)) as source, closing(
            sqlite3.connect(temp_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        ) as destination:
            destination.execute(
                f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_SECONDS * 1000}"
            )
            source.backup(destination)
            destination.commit()

        inspect_database(temp_path)
        os.replace(temp_path, destination_path)
        return inspect_database(destination_path)
    except DatabaseBackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseBackupError(f"Could not create SQLite backup: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def inspect_database(database_path: Path) -> DatabaseInspection:
    """Validate integrity, foreign keys, and the tables required by Kvizi."""
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise DatabaseBackupError(f"SQLite database not found: {database_path}")

    try:
        with closing(_connect_read_only(database_path)) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity_messages = [str(row[0]) for row in integrity_rows]
            if integrity_messages != ["ok"]:
                details = "; ".join(integrity_messages[:5])
                raise DatabaseBackupError(f"SQLite integrity_check failed: {details}")

            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise DatabaseBackupError(
                    f"SQLite foreign_key_check failed: {len(foreign_key_rows)} violation(s)"
                )

            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing_tables = sorted(set(REQUIRED_TABLES) - table_names)
            if missing_tables:
                raise DatabaseBackupError(
                    "SQLite backup is missing required tables: "
                    + ", ".join(missing_tables)
                )

            table_counts = {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                )
                for table in REQUIRED_TABLES
            }
    except DatabaseBackupError:
        raise
    except sqlite3.Error as exc:
        raise DatabaseBackupError(f"Invalid SQLite database: {exc}") from exc

    return DatabaseInspection(
        path=database_path,
        size_bytes=database_path.stat().st_size,
        sha256=_sha256(database_path),
        table_counts=table_counts,
    )


def restore_database(
    backup_path: Path,
    database_path: Path,
    *,
    pre_restore_backup_dir: Path,
) -> DatabaseRestoreResult:
    """Replace a stopped Kvizi database after preserving its current state."""
    backup_path = backup_path.resolve()
    database_path = database_path.resolve()
    if backup_path == database_path:
        raise DatabaseBackupError("Backup and target database paths must differ")

    inspect_database(backup_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    pre_restore_path: Path | None = None
    if database_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        pre_restore_path = (
            pre_restore_backup_dir.resolve()
            / f"kvizi-before-restore-{timestamp}.sqlite3"
        )
        backup_database(database_path, pre_restore_path)

    restore_temp_path = database_path.with_name(
        f".{database_path.name}.{uuid4().hex}.restore"
    )
    try:
        backup_database(backup_path, restore_temp_path)
        if database_path.exists():
            _checkpoint_database(database_path)
            _remove_sidecars(database_path)
        os.replace(restore_temp_path, database_path)
        _remove_sidecars(database_path)
        inspection = inspect_database(database_path)
    except DatabaseBackupError:
        raise
    except OSError as exc:
        raise DatabaseBackupError(f"Could not restore SQLite database: {exc}") from exc
    finally:
        restore_temp_path.unlink(missing_ok=True)

    return DatabaseRestoreResult(
        database_path=database_path,
        pre_restore_backup_path=pre_restore_path,
        inspection=inspection,
    )


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    )
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _checkpoint_database(database_path: Path) -> None:
    try:
        with closing(
            sqlite3.connect(database_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        ) as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None and int(row[0]) != 0:
                raise DatabaseBackupError(
                    "Target database is busy; stop the web app and cron jobs before restore"
                )
    except sqlite3.Error as exc:
        raise DatabaseBackupError(f"Could not checkpoint target database: {exc}") from exc


def _remove_sidecars(database_path: Path) -> None:
    try:
        Path(f"{database_path}-wal").unlink(missing_ok=True)
        Path(f"{database_path}-shm").unlink(missing_ok=True)
    except OSError as exc:
        raise DatabaseBackupError(
            "Could not remove SQLite WAL files; stop the web app before restore"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
