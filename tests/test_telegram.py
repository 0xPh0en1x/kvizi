from __future__ import annotations

from typing import Any

import pytest

import kvizi.telegram as telegram_module
from kvizi.telegram import TelegramApiError, TelegramClient


class FakeJsonResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeFileResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="replace")


class FakeNonJsonResponse:
    status_code = 200
    text = "not json"

    def json(self) -> dict[str, Any]:
        raise ValueError("not json")


def test_download_file_resolves_telegram_file_path(monkeypatch: Any) -> None:
    post_calls: list[dict[str, Any]] = []
    get_calls: list[str] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeJsonResponse:
        post_calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeJsonResponse(
            {
                "ok": True,
                "result": {"file_path": "documents/questions.csv"},
            }
        )

    def fake_get(url: str, timeout: int) -> FakeFileResponse:
        get_calls.append(url)
        return FakeFileResponse(b"id,topic_key\n")

    monkeypatch.setattr(telegram_module.requests, "post", fake_post)
    monkeypatch.setattr(telegram_module.requests, "get", fake_get)

    content = TelegramClient("token", timeout_seconds=7).download_file("file-1")

    assert content == b"id,topic_key\n"
    assert post_calls == [
        {
            "url": "https://api.telegram.org/bottoken/getFile",
            "json": {"file_id": "file-1"},
            "timeout": 7,
        }
    ]
    assert get_calls == [
        "https://api.telegram.org/file/bottoken/documents/questions.csv"
    ]


def test_send_poll_uses_current_bot_api_payload(monkeypatch: Any) -> None:
    post_calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeJsonResponse:
        post_calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeJsonResponse(
            {"ok": True, "result": {"message_id": 123, "poll": {"id": "poll-1"}}}
        )

    monkeypatch.setattr(telegram_module.requests, "post", fake_post)

    TelegramClient("token").send_poll(
        chat_id="-1001",
        question="Question?",
        options=["One", "Two"],
        correct_option_id=1,
        explanation="Because",
        message_thread_id=10,
        reply_markup={"inline_keyboard": []},
    )

    payload = post_calls[0]["json"]
    assert payload["options"] == [{"text": "One"}, {"text": "Two"}]
    assert payload["correct_option_ids"] == [1]
    assert "correct_option_id" not in payload


def test_send_message_does_not_retry_ambiguous_proxy_error(monkeypatch: Any) -> None:
    post_calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeJsonResponse:
        post_calls.append({"url": url, "json": json, "timeout": timeout})
        raise telegram_module.requests.exceptions.ProxyError("proxy 503 for bottoken")

    monkeypatch.setattr(telegram_module.requests, "post", fake_post)
    monkeypatch.setattr(telegram_module.time, "sleep", lambda _: None)

    with pytest.raises(TelegramApiError) as error:
        TelegramClient("token", timeout_seconds=7, max_retries=2).send_message(
            chat_id="-1001",
            text="hello",
        )

    assert error.value.ambiguous is True
    assert len(post_calls) == 1


def test_get_file_retries_safe_transient_proxy_error(monkeypatch: Any) -> None:
    post_calls = 0

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeJsonResponse:
        nonlocal post_calls
        post_calls += 1
        if post_calls == 1:
            raise telegram_module.requests.exceptions.ProxyError("proxy 503")
        return FakeJsonResponse({"ok": True, "result": {"file_path": "questions.csv"}})

    monkeypatch.setattr(telegram_module.requests, "post", fake_post)
    monkeypatch.setattr(telegram_module.requests, "get", lambda *args, **kwargs: FakeFileResponse(b"ok"))
    monkeypatch.setattr(telegram_module.time, "sleep", lambda _: None)

    assert TelegramClient("token", max_retries=2).download_file("file-1") == b"ok"
    assert post_calls == 2


def test_non_json_send_response_is_marked_ambiguous(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        telegram_module.requests,
        "post",
        lambda *args, **kwargs: FakeNonJsonResponse(),
    )

    with pytest.raises(TelegramApiError) as error:
        TelegramClient("token").send_message(chat_id="-1001", text="hello")

    assert error.value.ambiguous is True


def test_send_message_wraps_and_sanitizes_request_exception(monkeypatch: Any) -> None:
    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeJsonResponse:
        raise telegram_module.requests.exceptions.ProxyError(f"failed url={url}")

    monkeypatch.setattr(telegram_module.requests, "post", fake_post)
    monkeypatch.setattr(telegram_module.time, "sleep", lambda _: None)

    try:
        TelegramClient("token", max_retries=1).send_message(chat_id="-1001", text="hello")
    except TelegramApiError as exc:
        message = str(exc)
    else:
        raise AssertionError("TelegramApiError was not raised")

    assert "<bot_token>" in message
    assert "bottoken" not in message
