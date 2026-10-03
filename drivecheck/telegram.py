"""Authenticated Telegram callback polling for DriveCheck actions."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from drivecheck.hardware import SafetyError

CALLBACK = re.compile(r"dc:([0-9a-f]{32}):(extended|eject|erase)\Z")


class TelegramInterface:
    """Long-poll Telegram callbacks without persisting provider credentials."""

    def __init__(
        self,
        engine,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_timeout: int = 25,
        error_delay: float = 5,
        idle_delay: float = 2,
    ) -> None:
        self.engine = engine
        self.transport = transport
        self.sleep = sleep
        self.poll_timeout = max(1, min(50, int(poll_timeout)))
        self.error_delay = max(0.1, error_delay)
        self.idle_delay = max(0.1, idle_delay)
        self.offset: int | None = None
        self._signature: tuple[str, str, str] | None = None

    def _settings(self) -> dict[str, Any]:
        value = getattr(getattr(self.engine, "settings", None), "value", {})
        notice = value.get("notifications", {}) if isinstance(value, dict) else {}
        return notice if isinstance(notice, dict) else {}

    def _set_error(self, message: str | None) -> None:
        if getattr(self.engine, "telegram_error", None) == message:
            return
        self.engine.telegram_error = message
        publish = getattr(self.engine, "publish", None)
        if callable(publish):
            publish()

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    def _authorization(self, settings: dict[str, Any]) -> tuple[int, int] | None:
        chat_id = self._integer(settings.get("telegram_chat_id"))
        user_id = self._integer(settings.get("telegram_user_id"))
        if chat_id is None:
            self._set_error("Interactive Telegram requires a numeric chat ID.")
            return None
        if user_id is None and chat_id > 0:
            user_id = chat_id
        if user_id is None or user_id <= 0:
            self._set_error(
                "Interactive Telegram group or channel controls require a numeric Telegram user ID."
            )
            return None
        return chat_id, user_id

    async def run(self) -> None:
        """Poll until cancelled, pausing when Telegram controls are disabled."""

        while True:
            settings = self._settings()
            active = settings.get("enabled") and settings.get("provider") == "telegram"
            if not active or not settings.get("telegram_token"):
                self.offset = None
                self._signature = None
                self._set_error(None)
                await self.sleep(self.idle_delay)
                continue
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._set_error("Telegram controls encountered an unexpected error and will retry.")
                await self.sleep(self.error_delay)

    async def poll_once(self) -> bool:
        """Perform one bounded poll; the first poll after configuration discards backlog."""

        settings = self._settings()
        if not settings.get("enabled") or settings.get("provider") != "telegram":
            self.offset = None
            self._signature = None
            self._set_error(None)
            return False
        token = str(settings.get("telegram_token") or "")
        if not token:
            self._set_error("Interactive Telegram requires a configured bot token.")
            await self.sleep(self.error_delay)
            return False
        if self._authorization(settings) is None:
            await self.sleep(self.error_delay)
            return False
        signature = (
            token,
            str(settings.get("telegram_chat_id") or ""),
            str(settings.get("telegram_user_id") or ""),
        )
        initial = signature != self._signature
        if initial:
            # Telegram defines a negative offset relative to the end of the
            # queue. -1 retrieves only the newest update and forgets older
            # backlog once the following positive offset is acknowledged.
            self.offset = -1

        updates = await self._get_updates(token, timeout=0 if initial else self.poll_timeout)
        current = self._settings()
        current_signature = (
            str(current.get("telegram_token") or ""),
            str(current.get("telegram_chat_id") or ""),
            str(current.get("telegram_user_id") or ""),
        )
        if (
            not current.get("enabled")
            or current.get("provider") != "telegram"
            or current_signature != signature
        ):
            # Never authorize a callback against a settings snapshot that was
            # revoked while getUpdates was in flight. The next cycle performs
            # a fresh backlog-discard poll for the replacement configuration.
            self.offset = None
            self._signature = None
            self._set_error(None)
            return False
        if updates is None:
            await self.sleep(self.error_delay)
            return False
        if updates:
            self.offset = max(update["update_id"] for update in updates) + 1
        elif initial:
            self.offset = None
        if initial:
            self._signature = signature
            self._set_error(None)
            return True
        handled = True
        for update in updates:
            handled = await self._handle_update(token, settings, update) and handled
        if handled:
            self._set_error(None)
        return True

    async def _get_updates(self, token: str, *, timeout: int) -> list[dict[str, Any]] | None:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["callback_query"],
        }
        if self.offset is not None:
            payload["offset"] = self.offset
        try:
            async with httpx.AsyncClient(
                timeout=timeout + 10,
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/getUpdates", json=payload
                )
            if response.status_code == 409:
                self._set_error(
                    "Telegram controls cannot poll because this bot has a webhook or another active poller. Remove the webhook manually or stop the other poller."
                )
                return None
            if not 200 <= response.status_code < 300:
                self._set_error(f"Telegram controls polling failed (HTTP {response.status_code}).")
                return None
            body = response.json()
            if (
                not isinstance(body, dict)
                or not body.get("ok")
                or not isinstance(body.get("result"), list)
            ):
                self._set_error("Telegram did not confirm the controls polling request.")
                return None
            return [
                update
                for update in body["result"]
                if isinstance(update, dict) and isinstance(update.get("update_id"), int)
            ]
        except (httpx.HTTPError, ValueError):
            self._set_error("Telegram controls could not reach the provider and will retry.")
            return None

    async def _handle_update(
        self, token: str, settings: dict[str, Any], update: dict[str, Any]
    ) -> bool:
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return True
        callback_id = callback.get("id")
        if not isinstance(callback_id, str) or not callback_id:
            return True
        authorization = self._authorization(settings)
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        actual_chat = self._integer(chat.get("id"))
        actual_user = self._integer(sender.get("id"))
        if authorization is None or (actual_chat, actual_user) != authorization:
            return await self._answer(
                token, callback_id, "Not authorized for this DriveCheck station."
            )

        match = CALLBACK.fullmatch(str(callback.get("data") or ""))
        if not match:
            return await self._answer(
                token, callback_id, "This DriveCheck action is no longer valid."
            )
        run_id, action = match.groups()
        if action == "erase":
            return await self._answer(
                token,
                callback_id,
                "Erase verification requires the DriveCheck dashboard and exact serial confirmation.",
                alert=True,
            )
        try:
            await self.engine.choose_action(run_id, action)
        except (KeyError, SafetyError, ValueError):
            return await self._answer(
                token, callback_id, "Action unavailable. Open the dashboard for current status."
            )
        confirmation = (
            "Extended test requested." if action == "extended" else "Safe eject requested."
        )
        return await self._answer(token, callback_id, confirmation)

    async def _answer(
        self, token: str, callback_id: str, text: str, *, alert: bool = False
    ) -> bool:
        payload = {
            "callback_query_id": callback_id,
            "text": text[:200],
            "show_alert": alert,
        }
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery", json=payload
                )
            body = response.json()
            if (
                not 200 <= response.status_code < 300
                or not isinstance(body, dict)
                or not body.get("ok")
            ):
                self._set_error("Telegram did not confirm an interactive action acknowledgment.")
                return False
            return True
        except (httpx.HTTPError, ValueError):
            self._set_error("Telegram could not acknowledge an interactive action.")
            return False


__all__ = ["TelegramInterface"]
