from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_LIMITS = {
    "cron_runs": 25,
    "polls": 100,
    "answers": 250,
    "bets": 250,
}


def export_state(database_path: Path, include_processed_updates: bool = False) -> dict[str, Any]:
    exported_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = _table_names(connection)

        state: dict[str, Any] = {
            "meta": {
                "exported_at": exported_at,
                "database_path": str(database_path),
                "schema_tables": sorted(tables),
            },
            "bot_settings": _select_all(connection, tables, "bot_settings", order_by="key"),
            "topics": _select_all(connection, tables, "topics", order_by="topic_key"),
            "users": _select_all(connection, tables, "users", order_by="user_id"),
            "scores": _select_all(
                connection,
                tables,
                "scores",
                order_by="season ASC, points DESC, correct_count DESC, user_id ASC",
            ),
            "active_polls": _select_all(
                connection,
                tables,
                "polls",
                where="status = 'active'",
                order_by="closes_at ASC",
            ),
            "recent_polls": _select_all(
                connection,
                tables,
                "polls",
                order_by="opened_at DESC",
                limit=DEFAULT_LIMITS["polls"],
            ),
            "recent_answers": _select_all(
                connection,
                tables,
                "answers",
                order_by="answered_at DESC",
                limit=DEFAULT_LIMITS["answers"],
            ),
            "recent_bets": _select_all(
                connection,
                tables,
                "bets",
                order_by="created_at DESC",
                limit=DEFAULT_LIMITS["bets"],
            ),
            "question_history": _select_all(
                connection,
                tables,
                "question_history",
                order_by="asked_at DESC",
            ),
            "recent_cron_runs": _select_all(
                connection,
                tables,
                "cron_runs",
                order_by="id DESC",
                limit=DEFAULT_LIMITS["cron_runs"],
            ),
        }

        if include_processed_updates:
            state["processed_updates"] = _select_all(
                connection,
                tables,
                "processed_updates",
                order_by="processed_at DESC",
            )

    return state


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _select_all(
    connection: sqlite3.Connection,
    tables: set[str],
    table: str,
    *,
    where: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if table not in tables:
        return []

    query = f"SELECT * FROM {table}"
    if where:
        query += f" WHERE {where}"
    if order_by:
        query += f" ORDER BY {order_by}"
    if limit is not None:
        query += f" LIMIT {int(limit)}"

    rows = connection.execute(query).fetchall()
    return [dict(row) for row in rows]
