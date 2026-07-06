from __future__ import annotations

from io import BytesIO
from typing import Any

import requests


class TelegramApiError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, bot_token: str, timeout_seconds: int = 15) -> None:
        self.bot_token = bot_token
        self.timeout_seconds = timeout_seconds

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self._request("sendMessage", payload)

    def send_document(
        self,
        *,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str = "",
        mime_type: str = "application/octet-stream",
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self._request_multipart(
            "sendDocument",
            payload,
            files={"document": (filename, BytesIO(content), mime_type)},
        )

    def send_poll(
        self,
        *,
        chat_id: str,
        question: str,
        options: list[str],
        correct_option_id: int,
        explanation: str,
        message_thread_id: int | None,
        reply_markup: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question,
            "options": options,
            "type": "quiz",
            "is_anonymous": False,
            "correct_option_id": correct_option_id,
            "allows_multiple_answers": False,
            "reply_markup": reply_markup,
        }
        if explanation:
            payload["explanation"] = explanation
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self._request("sendPoll", payload)

    def answer_callback_query(self, *, callback_query_id: str, text: str, show_alert: bool = False) -> None:
        self._request(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )

    def stop_poll(self, *, chat_id: str, message_id: int) -> dict[str, Any]:
        return self._request(
            "stopPoll",
            {
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )

    def download_file(self, file_id: str) -> bytes:
        file_info = self._request("getFile", {"file_id": file_id})
        file_path = str((file_info.get("result") or {}).get("file_path") or "")
        if not file_path:
            raise TelegramApiError("Telegram getFile returned no file_path")

        if not self.bot_token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")

        response = requests.get(
            f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}",
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise TelegramApiError(f"Telegram file download failed: {response.text}")
        return response.content

    def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        allowed_updates: list[str],
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": allowed_updates,
                "drop_pending_updates": drop_pending_updates,
            },
        )

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.bot_token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")

        response = requests.post(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramApiError(f"Telegram {method} returned non-JSON response") from exc

        if response.status_code >= 400 or not data.get("ok"):
            description = data.get("description") or response.text
            raise TelegramApiError(f"Telegram {method} failed: {description}")
        return data

    def _request_multipart(
        self,
        method: str,
        payload: dict[str, Any],
        files: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.bot_token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")

        response = requests.post(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            data=payload,
            files=files,
            timeout=self.timeout_seconds,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramApiError(f"Telegram {method} returned non-JSON response") from exc

        if response.status_code >= 400 or not data.get("ok"):
            description = data.get("description") or response.text
            raise TelegramApiError(f"Telegram {method} failed: {description}")
        return data
