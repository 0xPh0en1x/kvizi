from __future__ import annotations

import hmac

from flask import Flask, abort, jsonify, request

from kvizi.ai import AIProvider, GroqProvider
from kvizi.config import Settings, load_settings
from kvizi.database import KviziRepository, utc_now_iso
from kvizi.service import KviziService
from kvizi.telegram import TelegramApiError, TelegramClient


def _secret_matches(configured: str, provided: str | None) -> bool:
    if not configured or not provided:
        return False
    return hmac.compare_digest(configured.encode("utf-8"), provided.encode("utf-8"))


def _runtime_configured(settings: Settings) -> bool:
    return all(
        (
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.webhook_secret,
            settings.cron_secret,
        )
    )


def create_app(
    settings: Settings | None = None,
    repository: KviziRepository | None = None,
    telegram: TelegramClient | None = None,
    ai_provider: AIProvider | None = None,
) -> Flask:
    settings = settings or load_settings()
    repository = repository or KviziRepository(settings.database_path)
    telegram = telegram or TelegramClient(settings.telegram_bot_token)
    if (
        ai_provider is None
        and settings.ai_enabled
        and settings.ai_copy_enabled
        and settings.groq_api_key
    ):
        ai_provider = GroqProvider(settings.groq_api_key, settings.ai_copy_model)

    repository.init_db()
    service = KviziService(
        settings=settings,
        repository=repository,
        telegram=telegram,
        ai_provider=ai_provider,
    )
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
        questions_count = service.question_bank.count()
        configured = _runtime_configured(settings)
        ok = not app.config["KVIZI_LOAD_ERROR"] and questions_count > 0 and configured
        return (
            jsonify(
                {
                    "ok": ok,
                    "questions": questions_count,
                    "configuration_ok": configured,
                    "questions_loaded": not bool(app.config["KVIZI_LOAD_ERROR"]),
                    "ai_copy_enabled": settings.ai_enabled and settings.ai_copy_enabled,
                    "ai_provider_configured": ai_provider is not None,
                }
            ),
            200 if ok else 503,
        )

    @app.post("/telegram/<webhook_secret>")
    def telegram_webhook(webhook_secret: str):
        if not _secret_matches(settings.webhook_secret, webhook_secret):
            abort(404)
        if not _secret_matches(
            settings.webhook_secret,
            request.headers.get("X-Telegram-Bot-Api-Secret-Token"),
        ):
            abort(403)
        update = request.get_json(silent=True) or {}
        try:
            return jsonify(service.handle_update(update))
        except TelegramApiError as exc:
            repository.record_error_event(
                source="telegram",
                event="webhook_update_failed",
                message=str(exc),
            )
            app.logger.warning("Telegram API failure while handling webhook update: %s", exc)
            return jsonify({"ok": False, "telegram_error": str(exc)}), 503

    @app.post("/cron/tick")
    def cron_tick():
        if not _secret_matches(
            settings.cron_secret,
            request.headers.get("X-Kvizi-Cron-Secret"),
        ):
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
            finished_at = utc_now_iso()
            repository.record_cron_run(started_at, finished_at, "failed", str(exc))
            repository.record_error_event(
                source="cron",
                event="tick_failed",
                message=str(exc),
                created_at=finished_at,
            )
            raise

    @app.post("/cron/maintenance")
    def cron_maintenance():
        if not _secret_matches(
            settings.cron_secret,
            request.headers.get("X-Kvizi-Cron-Secret"),
        ):
            abort(403)

        started_at = utc_now_iso()
        try:
            closed = service.close_expired_polls()
            ai_enhanced = service.retry_ai_enhancements()
            message = f"Closed expired polls: {closed}; AI enhanced: {ai_enhanced}"
            status = "maintenance_closed" if closed else "maintenance_ok"
            repository.record_cron_run(started_at, utc_now_iso(), status, message)
            return jsonify(
                {
                    "ok": True,
                    "closed": closed,
                    "ai_enhanced": ai_enhanced,
                    "message": message,
                }
            )
        except Exception as exc:
            finished_at = utc_now_iso()
            repository.record_cron_run(started_at, finished_at, "maintenance_failed", str(exc))
            repository.record_error_event(
                source="cron",
                event="maintenance_failed",
                message=str(exc),
                created_at=finished_at,
            )
            raise

    @app.post("/cron/daily")
    def cron_daily():
        if not _secret_matches(
            settings.cron_secret,
            request.headers.get("X-Kvizi-Cron-Secret"),
        ):
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
            finished_at = utc_now_iso()
            repository.record_cron_run(started_at, finished_at, "daily_failed", str(exc))
            repository.record_error_event(
                source="cron",
                event="daily_failed",
                message=str(exc),
                created_at=finished_at,
            )
            raise

    @app.post("/cron/backup")
    def cron_backup():
        if not _secret_matches(settings.cron_secret, request.headers.get("X-Kvizi-Cron-Secret")):
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
            finished_at = utc_now_iso()
            repository.record_cron_run(started_at, finished_at, "backup_failed", str(exc))
            repository.record_error_event(
                source="cron",
                event="backup_failed",
                message=str(exc),
                created_at=finished_at,
            )
            raise

    return app
