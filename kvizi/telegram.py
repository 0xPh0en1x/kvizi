from __future__ import annotations

import time
from collections.abc import Callable
from io import BytesIO
from typing import Any

import requests


class TelegramApiError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


RETRY_STATUS_CODES = {500, 502, 503, 504}


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        timeout_seconds: int = 15,
        max_retries: int = 3,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        self.bot_token = bot_token
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self.retry_delay_seconds = retry_delay_seconds

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

    def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
    ) -> dict[str, Any]:
        return self._request(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            },
        )

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
        open_period: int | None,
        message_thread_id: int | None,
        reply_markup: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question,
            "options": [{"text": option} for option in options],
            "type": "quiz",
            "is_anonymous": False,
            "correct_option_ids": [correct_option_id],
            "allows_multiple_answers": False,
            "reply_markup": reply_markup,
        }
        if explanation:
            payload["explanation"] = explanation
        if open_period is not None:
            payload["open_period"] = open_period
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
        file_info = self._request("getFile", {"file_id": file_id}, retry=True)
        file_path = str((file_info.get("result") or {}).get("file_path") or "")
        if not file_path:
            raise TelegramApiError("Telegram getFile returned no file_path")

        if not self.bot_token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")

        return self._download_file_bytes(file_path)

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
            retry=True,
        )

    def _request(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        retry: bool = False,
    ) -> dict[str, Any]:
        if not self.bot_token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")

        return self._request_with_retries(
            method,
            lambda: requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/{method}",
                json=payload,
                timeout=self.timeout_seconds,
            ),
            retry=retry,
        )

    def _request_multipart(
        self,
        method: str,
        payload: dict[str, Any],
        files: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.bot_token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")

        return self._request_with_retries(
            method,
            lambda: requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/{method}",
                data=payload,
                files=files,
                timeout=self.timeout_seconds,
            ),
            before_attempt=lambda: self._rewind_files(files),
            retry=False,
        )

    def _request_with_retries(
        self,
        method: str,
        send_request: Callable[[], requests.Response],
        before_attempt: Callable[[], None] | None = None,
        *,
        retry: bool,
    ) -> dict[str, Any]:
        max_attempts = self.max_retries if retry else 1
        for attempt in range(1, max_attempts + 1):
            if before_attempt is not None:
                before_attempt()
            try:
                response = send_request()
            except requests.RequestException as exc:
                if attempt < max_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise TelegramApiError(
                    f"Telegram {method} request failed after {attempt} attempts: "
                    f"{self._sanitize_error(exc)}",
                    ambiguous=True,
                ) from exc

            try:
                data = response.json()
            except ValueError as exc:
                if response.status_code in RETRY_STATUS_CODES and attempt < max_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise TelegramApiError(
                    f"Telegram {method} returned non-JSON response",
                    # A malformed response cannot prove that Telegram did not
                    # perform a non-idempotent send.
                    ambiguous=True,
                ) from exc

            if response.status_code in RETRY_STATUS_CODES and attempt < max_attempts:
                self._sleep_before_retry(attempt)
                continue

            if response.status_code >= 400 or not data.get("ok"):
                description = data.get("description") or response.text
                raise TelegramApiError(
                    f"Telegram {method} failed: {self._sanitize_error(description)}",
                    ambiguous=response.status_code in RETRY_STATUS_CODES,
                )
            return data

        raise TelegramApiError(f"Telegram {method} request failed")

    def _download_file_bytes(self, file_path: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.get(
                    f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}",
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if self._can_retry(attempt):
                    self._sleep_before_retry(attempt)
                    continue
                raise TelegramApiError(
                    f"Telegram file download failed after {attempt} attempts: "
                    f"{self._sanitize_error(exc)}"
                ) from exc

            if response.status_code in RETRY_STATUS_CODES and self._can_retry(attempt):
                self._sleep_before_retry(attempt)
                continue
            if response.status_code >= 400:
                raise TelegramApiError(f"Telegram file download failed: {self._sanitize_error(response.text)}")
            return response.content

        raise TelegramApiError("Telegram file download failed")

    def _can_retry(self, attempt: int) -> bool:
        return attempt < self.max_retries

    def _sleep_before_retry(self, attempt: int) -> None:
        if self.retry_delay_seconds <= 0:
            return
        time.sleep(self.retry_delay_seconds * attempt)

    def _sanitize_error(self, value: object) -> str:
        text = str(value)
        if self.bot_token:
            text = text.replace(self.bot_token, "<bot_token>")
        return text

    def _rewind_files(self, files: dict[str, Any]) -> None:
        for file_info in files.values():
            stream = file_info[1] if isinstance(file_info, tuple) and len(file_info) > 1 else file_info
            if hasattr(stream, "seek"):
                stream.seek(0)
