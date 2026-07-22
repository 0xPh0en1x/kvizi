from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests


GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class AIResult:
    text: str
    provider: str
    model: str
    latency_ms: int


class AIProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class AIProvider(Protocol):
    name: str
    model: str

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        timeout_seconds: float,
    ) -> AIResult: ...


class GroqProvider:
    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        endpoint: str = GROQ_CHAT_COMPLETIONS_URL,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.endpoint = endpoint

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        timeout_seconds: float,
    ) -> AIResult:
        if not self.api_key:
            raise AIProviderError(
                "GROQ_API_KEY is not configured",
                kind="configuration",
                retryable=False,
            )
        if not self.model:
            raise AIProviderError(
                "Groq model is not configured",
                kind="configuration",
                retryable=False,
            )

        started = time.monotonic()
        try:
            response = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=self._request_payload(messages),
                timeout=timeout_seconds,
            )
        except requests.Timeout as exc:
            raise AIProviderError(
                "Groq request timed out",
                kind="timeout",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise AIProviderError(
                f"Groq network request failed: {_sanitize_error(exc, self.api_key)}",
                kind="network",
                retryable=True,
            ) from exc

        latency_ms = round((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_HTTP_STATUS_CODES
            kind = "rate_limit" if response.status_code == 429 else "http"
            raise AIProviderError(
                f"Groq HTTP {response.status_code}: {_response_error(response, self.api_key)}",
                kind=kind,
                retryable=retryable,
                retry_after_seconds=_retry_after_seconds(response),
            )

        try:
            data = response.json()
            text = str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AIProviderError(
                "Groq returned an invalid completion payload",
                kind="invalid_response",
                retryable=True,
            ) from exc

        if not text:
            raise AIProviderError(
                "Groq returned an empty completion",
                kind="invalid_response",
                retryable=True,
            )
        return AIResult(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
        )

    def _request_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": 160,
            "response_format": {"type": "json_object"},
        }
        if self.model.casefold().startswith("qwen/qwen3.6-"):
            payload.update(
                {
                    "reasoning_effort": "none",
                    "temperature": 0.7,
                    "top_p": 0.8,
                    "top_k": 20,
                    "min_p": 0,
                    "presence_penalty": 1.5,
                }
            )
        else:
            payload["temperature"] = 0.6
        return payload


def normalize_short_intro(
    text: str,
    *,
    max_chars: int = 180,
    forbidden_phrases: tuple[str, ...] = (),
    rejected_patterns: tuple[str, ...] = (),
) -> str:
    normalized = " ".join(text.strip().strip('"\'«»').split())
    if not normalized:
        raise AIProviderError(
            "AI intro is empty",
            kind="invalid_output",
            retryable=False,
        )
    if len(normalized) > max_chars:
        raise AIProviderError(
            f"AI intro is longer than {max_chars} characters",
            kind="invalid_output",
            retryable=False,
        )
    if re.search(r"https?://|t\.me/|@\w+", normalized, re.IGNORECASE):
        raise AIProviderError(
            "AI intro contains a link or mention",
            kind="invalid_output",
            retryable=False,
        )
    if any(character.isdigit() for character in normalized):
        raise AIProviderError(
            "AI intro contains an untrusted number",
            kind="invalid_output",
            retryable=False,
        )
    normalized_tokens = _normalized_tokens(normalized)
    for phrase in forbidden_phrases:
        phrase_tokens = _normalized_tokens(phrase)
        if phrase_tokens and _contains_token_sequence(normalized_tokens, phrase_tokens):
            raise AIProviderError(
                "AI intro contains a protected answer option",
                kind="invalid_output",
                retryable=False,
            )
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in rejected_patterns):
        raise AIProviderError(
            "AI intro matches a low-quality generic pattern",
            kind="invalid_output",
            retryable=False,
        )
    return normalized


def _normalized_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[0-9a-zа-яё]+", value.casefold()))


def _contains_token_sequence(
    tokens: tuple[str, ...],
    candidate: tuple[str, ...],
) -> bool:
    width = len(candidate)
    return any(tokens[index : index + width] == candidate for index in range(len(tokens) - width + 1))


def _retry_after_seconds(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _response_error(response: requests.Response, api_key: str) -> str:
    try:
        data: Any = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return _sanitize_error(error["message"], api_key)
    except ValueError:
        pass
    return _sanitize_error(response.text or "request failed", api_key)


def _sanitize_error(value: object, api_key: str) -> str:
    text = str(value)
    if api_key:
        text = text.replace(api_key, "<groq_api_key>")
    return text[:500]
