"""Provider adapters. Never include tokens or webhook URLs in error messages."""

import re
from urllib.parse import urlsplit

import httpx


class NotificationError(Exception):
    pass


def validate_settings(settings: dict) -> None:
    if settings["provider"] not in {"none", "discord", "telegram"}:
        raise ValueError("Choose none, Discord, or Telegram")
    webhook = settings.get("discord_webhook", "")
    if webhook:
        url = urlsplit(webhook)
        if (
            url.scheme != "https"
            or url.hostname not in {"discord.com", "canary.discord.com", "ptb.discord.com"}
            or url.port not in {None, 443}
            or url.username
            or url.password
            or url.fragment
            or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+", url.path)
        ):
            raise ValueError("Enter a valid HTTPS Discord incoming webhook URL")
    token = settings.get("telegram_token", "")
    if token and not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise ValueError("Enter a valid Telegram bot token")
    chat = settings.get("telegram_chat_id", "")
    if chat and not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", chat):
        raise ValueError("Enter a numeric Telegram chat ID or @channel name")
    if settings["enabled"]:
        if settings["provider"] == "none":
            raise ValueError("Choose a notification provider before enabling notifications")
        if settings["provider"] == "discord" and not webhook:
            raise ValueError("Discord requires an incoming webhook URL")
        if settings["provider"] == "telegram" and not (token and chat):
            raise ValueError("Telegram requires both a bot token and chat ID")


async def send(settings: dict, message: str, transport=None) -> None:
    validate_settings(settings)
    if not settings["enabled"] or settings["provider"] == "none":
        raise NotificationError("Notifications are disabled. Save an enabled provider first.")
    provider = settings["provider"]
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, transport=transport, trust_env=False
        ) as client:
            if provider == "discord":
                response = await client.post(
                    settings["discord_webhook"],
                    params={"wait": "true"},
                    json={"content": message[:1900], "allowed_mentions": {"parse": []}},
                )
            else:
                response = await client.post(
                    f"https://api.telegram.org/bot{settings['telegram_token']}/sendMessage",
                    json={"chat_id": settings["telegram_chat_id"], "text": message[:4000]},
                )
            if response.status_code == 429:
                raise NotificationError(f"{provider.title()} rate limit reached. Try again later")
            if not 200 <= response.status_code < 300:
                raise NotificationError(
                    f"{provider.title()} rejected delivery (HTTP {response.status_code}). Check provider credentials and permissions."
                )
            if provider == "telegram" and not response.json().get("ok"):
                raise NotificationError(
                    "Telegram did not confirm delivery. Check bot access to the chat."
                )
    except NotificationError:
        raise
    except (httpx.HTTPError, ValueError):
        raise NotificationError(
            f"{provider.title()} delivery could not be confirmed. Check connectivity and configuration."
        ) from None


def run_message(run: dict, demo: bool = False) -> str:
    drive = run["drive"]
    benchmark = run["results"].get("benchmark", {})
    speed = benchmark.get("read_mbps")
    lines = [
        ("[Simulation] " if demo else "") + f"DriveCheck: {run['status']}",
        f"{drive['model']} | Serial: {drive['serial'] or 'unavailable'}",
        f"Profile: {run['profile']} | {run['detail']}",
    ]
    if speed is not None:
        lines.append(f"Sequential read: {speed:.1f} MB/s")
    lifecycle = run.get("lifecycle", {})
    if lifecycle.get("eject_status") == "ejected":
        lines.append("Drive safely ejected; ready to remove.")
    elif lifecycle.get("eject_status") == "pending":
        lines.append("Safe eject pending. Wait for the release confirmation before removal.")
    elif lifecycle.get("eject_status") in {"failed", "unsupported"}:
        lines.append("Safe eject was not confirmed. Check the station before removal.")
    lines.append(f"Run: {run['id']}")
    return "\n".join(lines)
