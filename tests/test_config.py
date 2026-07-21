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
