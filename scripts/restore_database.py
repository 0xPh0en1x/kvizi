from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import load_settings  # noqa: E402
from kvizi.database import KviziRepository  # noqa: E402
from kvizi.database_backup import (  # noqa: E402
    DatabaseBackupError,
    DatabaseInspection,
    inspect_database,
    restore_database,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate or restore a complete Kvizi SQLite backup."
    )
    parser.add_argument("--input", type=Path, required=True, help="SQLite backup file")
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Target database path. Defaults to KVIZI_DB_PATH.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Restore after validation. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--confirm-app-stopped",
        action="store_true",
        help="Confirm that the PythonAnywhere web app and Kvizi cron jobs are stopped.",
    )
    parser.add_argument(
        "--pre-restore-backup-dir",
        type=Path,
        default=PROJECT_ROOT / "backups" / "database",
        help="Directory for the automatic backup of the current database.",
    )
    args = parser.parse_args()

    settings = load_settings()
    database_path = args.database or settings.database_path

    try:
        inspection = inspect_database(args.input)
    except DatabaseBackupError as exc:
        raise SystemExit(f"Backup validation failed: {exc}") from exc

    _print_inspection("Backup valid", inspection)
    if not args.apply:
        print("Validation only; the current database was not changed.")
        print("Use --apply --confirm-app-stopped after stopping the web app and cron jobs.")
        return

    if not args.confirm_app_stopped:
        parser.error("--apply requires --confirm-app-stopped")

    try:
        result = restore_database(
            args.input,
            database_path,
            pre_restore_backup_dir=args.pre_restore_backup_dir,
        )
        KviziRepository(database_path).init_db()
        restored_inspection = inspect_database(database_path)
    except DatabaseBackupError as exc:
        raise SystemExit(f"Restore failed: {exc}") from exc

    print(f"Restored Kvizi database: {result.database_path}")
    if result.pre_restore_backup_path is not None:
        print(f"Previous database saved: {result.pre_restore_backup_path}")
    _print_inspection("Restored database valid", restored_inspection)


def _print_inspection(label: str, inspection: DatabaseInspection) -> None:
    print(
        f"{label}: size={inspection.size_bytes} bytes, "
        f"sha256={inspection.sha256}"
    )
    print(
        "Rows: "
        f"users={inspection.table_counts['users']}, "
        f"scores={inspection.table_counts['scores']}, "
        f"polls={inspection.table_counts['polls']}, "
        f"answers={inspection.table_counts['answers']}"
    )


if __name__ == "__main__":
    main()
