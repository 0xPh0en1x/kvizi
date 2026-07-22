from __future__ import annotations

from pathlib import Path

import pytest

from kvizi.config import load_settings


def test_load_settings_parses_announcement_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KVIZI_DB_PATH", str(tmp_path / "kvizi.sqlite3"))
    monkeypatch.setenv("KVIZI_QUESTIONS_PATH", str(tmp_path / "questions.csv"))
    monkeypatch.setenv("KVIZI_ANNOUNCE_FIRST_ANSWER", "false")
    monkeypatch.setenv("KVIZI_ANNOUNCE_NO_ANSWERS", "0")
    monkeypatch.setenv("KVIZI_ANNOUNCE_RISK_FAILURES", "off")
    monkeypatch.setenv("KVIZI_ANNOUNCE_STREAKS", "no")

    settings = load_settings()

    assert settings.announce_first_answer is False
    assert settings.announce_no_answers is False
    assert settings.announce_risk_failures is False
    assert settings.announce_streaks is False


def test_load_settings_rejects_invalid_announcement_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KVIZI_ANNOUNCE_FIRST_ANSWER", "maybe")

    with pytest.raises(ValueError, match="Invalid boolean value"):
        load_settings()


def test_load_settings_has_no_known_secret_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KVIZI_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("KVIZI_CRON_SECRET", raising=False)

    settings = load_settings()

    assert settings.webhook_secret == ""
    assert settings.cron_secret == ""


def test_ai_copy_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KVIZI_AI_ENABLED", raising=False)
    monkeypatch.delenv("KVIZI_AI_COPY_ENABLED", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    settings = load_settings()

    assert settings.ai_enabled is False
    assert settings.ai_copy_enabled is False
    assert settings.groq_api_key == ""
    assert settings.ai_copy_model == "llama-3.1-8b-instant"
    assert settings.ai_timeout_seconds == 7.0
    assert settings.ai_max_attempts == 3


def test_load_settings_parses_ai_copy_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KVIZI_AI_ENABLED", "true")
    monkeypatch.setenv("KVIZI_AI_COPY_ENABLED", "1")
    monkeypatch.setenv("GROQ_API_KEY", " groq-secret ")
    monkeypatch.setenv("KVIZI_AI_COPY_MODEL", "test-model")
    monkeypatch.setenv("KVIZI_AI_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("KVIZI_AI_RETRY_DELAY_SECONDS", "17")
    monkeypatch.setenv("KVIZI_AI_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("KVIZI_AI_JOB_TTL_SECONDS", "900")

    settings = load_settings()

    assert settings.ai_enabled is True
    assert settings.ai_copy_enabled is True
    assert settings.groq_api_key == "groq-secret"
    assert settings.ai_copy_model == "test-model"
    assert settings.ai_timeout_seconds == 2.5
    assert settings.ai_retry_delay_seconds == 17
    assert settings.ai_max_attempts == 4
    assert settings.ai_job_ttl_seconds == 900


def test_load_settings_rejects_non_positive_ai_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KVIZI_AI_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match="KVIZI_AI_TIMEOUT_SECONDS"):
        load_settings()
