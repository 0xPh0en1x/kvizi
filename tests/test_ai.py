from __future__ import annotations

from typing import Any

import pytest

import kvizi.ai as ai_module
from kvizi.ai import AIProviderError, GroqProvider, normalize_short_intro


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def test_groq_provider_returns_completion_and_uses_short_output(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(
            {"choices": [{"message": {"content": "Пульт ожил, вопрос уже в эфире."}}]}
        )

    monkeypatch.setattr(ai_module.requests, "post", fake_post)

    result = GroqProvider("secret-key", "llama-3.1-8b-instant").complete(
        [{"role": "user", "content": "test"}],
        purpose="question_announcement",
        timeout_seconds=2.5,
    )

    assert result.text == "Пульт ожил, вопрос уже в эфире."
    assert result.provider == "groq"
    assert result.model == "llama-3.1-8b-instant"
    assert calls[0]["url"] == ai_module.GROQ_CHAT_COMPLETIONS_URL
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-key"
    assert calls[0]["json"]["max_completion_tokens"] == 96
    assert calls[0]["timeout"] == 2.5


def test_groq_provider_marks_429_retryable_and_reads_retry_after(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        ai_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(
            {"error": {"message": "rate limited"}},
            status_code=429,
            headers={"Retry-After": "12"},
        ),
    )

    with pytest.raises(AIProviderError) as error:
        GroqProvider("secret-key", "model").complete(
            [{"role": "user", "content": "test"}],
            purpose="question_announcement",
            timeout_seconds=1,
        )

    assert error.value.kind == "rate_limit"
    assert error.value.retryable is True
    assert error.value.retry_after_seconds == 12


def test_groq_provider_sanitizes_key_in_network_errors(monkeypatch: Any) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise ai_module.requests.exceptions.ProxyError("proxy rejected secret-key")

    monkeypatch.setattr(ai_module.requests, "post", fail)

    with pytest.raises(AIProviderError) as error:
        GroqProvider("secret-key", "model").complete(
            [{"role": "user", "content": "test"}],
            purpose="question_announcement",
            timeout_seconds=1,
        )

    assert error.value.retryable is True
    assert "secret-key" not in str(error.value)
    assert "<groq_api_key>" in str(error.value)


@pytest.mark.parametrize(
    "text",
    (
        "",
        "Вопрос номер 7 уже здесь.",
        "Смотри https://example.com",
        "x" * 181,
    ),
)
def test_short_intro_rejects_untrusted_output(text: str) -> None:
    with pytest.raises(AIProviderError) as error:
        normalize_short_intro(text)

    assert error.value.retryable is False


def test_short_intro_normalizes_whitespace_and_quotes() -> None:
    assert normalize_short_intro(' « Пульт   ожил, вопрос в эфире. » ') == (
        "Пульт ожил, вопрос в эфире."
    )
