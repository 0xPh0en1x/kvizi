from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import Settings, load_settings  # noqa: E402
from kvizi.database import KviziRepository  # noqa: E402
from kvizi.question_report import build_report, find_duplicate_ids, load_bound_topic_keys  # noqa: E402
from kvizi.questions import QuestionValidationError, load_questions  # noqa: E402
from kvizi.web import create_app  # noqa: E402


REQUIRED_DEPLOY_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "KVIZI_WEBHOOK_SECRET",
    "KVIZI_CRON_SECRET",
    "KVIZI_ADMIN_IDS",
)


class SmokeReport:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, message: str) -> None:
        print(f"[OK] {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"[WARN] {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"[FAIL] {message}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local Kvizi deployment smoke checks.")
    parser.add_argument(
        "--strict-env",
        action="store_true",
        help="Fail if deploy env vars are missing or still use dev defaults.",
    )
    args = parser.parse_args()

    report = SmokeReport()
    settings = load_settings()

    check_environment(settings, report, strict=args.strict_env)
    check_questions(settings, report)
    check_database(settings, report)
    check_flask_routes(settings, report)

    print(f"Smoke check finished: failures={report.failures}, warnings={report.warnings}")
    if report.failures:
        raise SystemExit(1)


def check_environment(settings: Settings, report: SmokeReport, *, strict: bool) -> None:
    missing = [name for name in REQUIRED_DEPLOY_ENV if not os.getenv(name, "").strip()]
    if missing:
        _warn_or_fail(
            report,
            strict,
            f"missing deploy env vars: {', '.join(missing)}",
        )
    else:
        report.ok("required deploy env vars are present")

    default_secrets: list[str] = []
    if settings.webhook_secret == "dev-webhook-secret":
        default_secrets.append("KVIZI_WEBHOOK_SECRET")
    if settings.cron_secret == "dev-cron-secret":
        default_secrets.append("KVIZI_CRON_SECRET")
    if default_secrets:
        _warn_or_fail(
            report,
            strict,
            f"dev default secrets still active: {', '.join(default_secrets)}",
        )
    else:
        report.ok("webhook and cron secrets are not dev defaults")

    if not settings.admin_ids:
        _warn_or_fail(report, strict, "KVIZI_ADMIN_IDS is empty")
    else:
        report.ok(f"admin ids configured: {len(settings.admin_ids)}")

    report.ok(f"database path: {settings.database_path}")
    report.ok(f"questions path: {settings.questions_path}")
    report.ok(f"timezone: {settings.timezone_name}")


def check_questions(settings: Settings, report: SmokeReport) -> None:
    try:
        bank = load_questions(settings.questions_path)
    except QuestionValidationError as exc:
        report.fail(f"questions CSV invalid: {exc}")
        return

    if bank.count() == 0:
        report.fail("questions CSV has no questions")
        return

    duplicate_ids = find_duplicate_ids(settings.questions_path)
    bound_topics = load_bound_topic_keys(settings.database_path)
    lines, warnings = build_report(bank, duplicate_ids, bound_topics)
    report.ok(f"questions CSV valid: {bank.count()} questions")
    for line in lines[1:4]:
        print(f"     {line}")
    for warning in warnings[:8]:
        report.warn(f"questions: {warning}")
    if len(warnings) > 8:
        report.warn(f"questions: {len(warnings) - 8} more warnings hidden")


def check_database(settings: Settings, report: SmokeReport) -> None:
    repository = KviziRepository(settings.database_path)
    try:
        repository.init_db()
        with repository.connect() as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    except Exception as exc:
        report.fail(f"SQLite init/open failed: {exc}")
        return

    if journal_mode == "wal":
        report.ok("SQLite journal_mode=wal")
    else:
        report.fail(f"SQLite journal_mode is {journal_mode}, expected wal")

    if busy_timeout >= 5000:
        report.ok(f"SQLite busy_timeout={busy_timeout}")
    else:
        report.fail(f"SQLite busy_timeout={busy_timeout}, expected >=5000")

    if foreign_keys == 1:
        report.ok("SQLite foreign_keys enabled")
    else:
        report.fail("SQLite foreign_keys disabled")


def check_flask_routes(settings: Settings, report: SmokeReport) -> None:
    try:
        app = create_app(settings=settings)
        client = app.test_client()
        health = client.get("/health")
    except Exception as exc:
        report.fail(f"Flask app creation or /health failed: {exc}")
        return

    if health.status_code != 200:
        report.fail(f"/health returned {health.status_code}")
        return

    payload = health.get_json(silent=True) or {}
    if payload.get("ok") is True and not payload.get("load_error"):
        report.ok(f"/health ok, questions={payload.get('questions')}")
    else:
        report.fail(f"/health payload not healthy: {payload}")

    for endpoint in ("/cron/tick", "/cron/maintenance", "/cron/daily", "/cron/backup"):
        response = client.post(endpoint)
        if response.status_code == 403:
            report.ok(f"{endpoint} rejects missing cron secret")
        else:
            report.fail(f"{endpoint} returned {response.status_code} without cron secret")

    webhook = client.post(f"/telegram/{settings.webhook_secret}", json={})
    if webhook.status_code == 403:
        report.ok("/telegram/<secret> rejects missing Telegram secret token")
    else:
        report.fail(f"/telegram/<secret> returned {webhook.status_code} without secret token")


def _warn_or_fail(report: SmokeReport, strict: bool, message: str) -> None:
    if strict:
        report.fail(message)
    else:
        report.warn(message)


if __name__ == "__main__":
    main()
