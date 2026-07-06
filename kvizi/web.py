from __future__ import annotations

from flask import Flask, abort, jsonify, request

from kvizi.config import Settings, load_settings
from kvizi.database import KviziRepository, utc_now_iso
from kvizi.service import KviziService
from kvizi.telegram import TelegramApiError, TelegramClient


def create_app(
    settings: Settings | None = None,
    repository: KviziRepository | None = None,
    telegram: TelegramClient | None = None,
) -> Flask:
    settings = settings or load_settings()
    repository = repository or KviziRepository(settings.database_path)
    telegram = telegram or TelegramClient(settings.telegram_bot_token)

    repository.init_db()
    service = KviziService(settings=settings, repository=repository, telegram=telegram)
    load_error = ""
    try:
        service.reload_questions()
    except Exception as exc:  # Keep Flask import alive; admin /reload or health will expose the issue.
        load_error = str(exc)

    app = Flask(__name__)
    app.config["KVIZI_SERVICE"] = service
    app.config["KVIZI_LOAD_ERROR"] = load_error

    @app.get("/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "questions": service.question_bank.count(),
                "load_error": app.config["KVIZI_LOAD_ERROR"],
                "database": str(settings.database_path),
            }
        )

    @app.post("/telegram/<webhook_secret>")
    def telegram_webhook(webhook_secret: str):
        if webhook_secret != settings.webhook_secret:
            abort(404)
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret:
            abort(403)
        update = request.get_json(silent=True) or {}
        try:
            return jsonify(service.handle_update(update))
        except TelegramApiError as exc:
            app.logger.warning("Telegram API failure while handling webhook update: %s", exc)
            return jsonify({"ok": False, "telegram_error": str(exc)}), 503

    @app.post("/cron/tick")
    def cron_tick():
        if request.headers.get("X-Kvizi-Cron-Secret") != settings.cron_secret:
            abort(403)

        started_at = utc_now_iso()
        try:
            if app.config["KVIZI_LOAD_ERROR"]:
                service.reload_questions()
                app.config["KVIZI_LOAD_ERROR"] = ""
            result = service.post_question(skip_busy_topics=True)
            status = "posted" if result.posted else "skipped"
            repository.record_cron_run(started_at, utc_now_iso(), status, result.message)
            return jsonify(
                {
                    "ok": True,
                    "posted": result.posted,
                    "message": result.message,
                    "topic_key": result.topic_key,
                    "question_id": result.question_id,
                    "poll_id": result.poll_id,
                }
            )
        except Exception as exc:
            repository.record_cron_run(started_at, utc_now_iso(), "failed", str(exc))
            raise

    @app.post("/cron/maintenance")
    def cron_maintenance():
        if request.headers.get("X-Kvizi-Cron-Secret") != settings.cron_secret:
            abort(403)

        started_at = utc_now_iso()
        try:
            closed = service.close_expired_polls()
            message = f"Closed expired polls: {closed}"
            status = "maintenance_closed" if closed else "maintenance_ok"
            repository.record_cron_run(started_at, utc_now_iso(), status, message)
            return jsonify(
                {
                    "ok": True,
                    "closed": closed,
                    "message": message,
                }
            )
        except Exception as exc:
            repository.record_cron_run(started_at, utc_now_iso(), "maintenance_failed", str(exc))
            raise

    @app.post("/cron/daily")
    def cron_daily():
        if request.headers.get("X-Kvizi-Cron-Secret") != settings.cron_secret:
            abort(403)

        started_at = utc_now_iso()
        try:
            result = service.post_daily_summary(force=False, remember_sent=True)
            status = "daily_posted" if result.posted else "daily_skipped"
            repository.record_cron_run(started_at, utc_now_iso(), status, result.message)
            return jsonify(
                {
                    "ok": True,
                    "posted": result.posted,
                    "message": result.message,
                    "summary_date": result.summary_date,
                }
            )
        except Exception as exc:
            repository.record_cron_run(started_at, utc_now_iso(), "daily_failed", str(exc))
            raise

    @app.post("/cron/backup")
    def cron_backup():
        if request.headers.get("X-Kvizi-Cron-Secret") != settings.cron_secret:
            abort(403)

        started_at = utc_now_iso()
        try:
            result = service.post_backup_export()
            if result.total_count == 0:
                status = "backup_skipped"
            elif result.sent_count == result.total_count:
                status = "backup_sent"
            elif result.sent_count > 0:
                status = "backup_partial"
            else:
                status = "backup_failed"
            message = (
                f"Backup {result.filename}: sent={result.sent_count}/{result.total_count}, "
                f"failed={result.failed_count}"
            )
            repository.record_cron_run(started_at, utc_now_iso(), status, message)
            return jsonify(
                {
                    "ok": result.sent_count > 0 and result.failed_count == 0,
                    "complete": result.sent_count == result.total_count and result.total_count > 0,
                    "filename": result.filename,
                    "sent": result.sent_count,
                    "failed": result.failed_count,
                    "admin_ids": result.admin_ids,
                    "errors": result.errors,
                    "message": message,
                }
            )
        except Exception as exc:
            repository.record_cron_run(started_at, utc_now_iso(), "backup_failed", str(exc))
            raise

    return app
