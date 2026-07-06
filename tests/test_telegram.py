from __future__ import annotations

from typing import Any

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


def test_send_message_retries_transient_proxy_error(monkeypatch: Any) -> None:
    post_calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> FakeJsonResponse:
        post_calls.append({"url": url, "json": json, "timeout": timeout})
        if len(post_calls) == 1:
            raise telegram_module.requests.exceptions.ProxyError("proxy 503 for bottoken")
        return FakeJsonResponse({"ok": True, "result": {"message_id": 123}})

    monkeypatch.setattr(telegram_module.requests, "post", fake_post)
    monkeypatch.setattr(telegram_module.time, "sleep", lambda _: None)

    result = TelegramClient("token", timeout_seconds=7, max_retries=2).send_message(
        chat_id="-1001",
        text="hello",
    )

    assert result["ok"] is True
    assert len(post_calls) == 2
    assert post_calls[1] == {
        "url": "https://api.telegram.org/bottoken/sendMessage",
        "json": {"chat_id": "-1001", "text": "hello", "disable_notification": False},
        "timeout": 7,
    }


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
