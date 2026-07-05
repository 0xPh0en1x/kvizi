from __future__ import annotations

from flask import Flask, abort, jsonify, request

from kvizi.config import Settings, load_settings
from kvizi.database import KviziRepository, utc_now_iso
from kvizi.service import KviziService
from kvizi.telegram import TelegramClient


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
        return jsonify(service.handle_update(update))

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

    return app
