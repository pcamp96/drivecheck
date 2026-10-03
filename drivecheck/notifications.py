"""Provider adapters. Never include tokens or webhook URLs in error messages."""

import json
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
    user_id = settings.get("telegram_user_id", "")
    if user_id and not re.fullmatch(r"[1-9]\d*", str(user_id)):
        raise ValueError("Enter a positive numeric Telegram user ID")
    if settings["enabled"]:
        if settings["provider"] == "none":
            raise ValueError("Choose a notification provider before enabling notifications")
        if settings["provider"] == "discord" and not webhook:
            raise ValueError("Discord requires an incoming webhook URL")
        if settings["provider"] == "telegram" and not (token and chat):
            raise ValueError("Telegram requires both a bot token and chat ID")


async def send(
    settings: dict,
    message: str,
    transport=None,
    *,
    attachment: dict | None = None,
    reply_markup: dict | None = None,
) -> None:
    validate_settings(settings)
    if not settings["enabled"] or settings["provider"] == "none":
        raise NotificationError("Notifications are disabled. Save an enabled provider first.")
    provider = settings["provider"]
    markup_json = None
    if provider == "telegram" and reply_markup is not None:
        if not isinstance(reply_markup, dict):
            raise NotificationError("The interactive message controls are invalid")
        try:
            markup_json = json.dumps(reply_markup, separators=(",", ":"))
        except (TypeError, ValueError):
            raise NotificationError("The interactive message controls are invalid") from None
    document = None
    if attachment is not None:
        filename = attachment.get("filename", "")
        text = attachment.get("text")
        if (
            not isinstance(filename, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.txt", filename)
            or not isinstance(text, str)
        ):
            raise NotificationError("The report attachment is invalid")
        content = text.encode("utf-8")
        if len(content) > 1_000_000:
            raise NotificationError("The readable report exceeds the attachment size limit")
        document = (filename, content, "text/plain; charset=utf-8")
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, transport=transport, trust_env=False
        ) as client:
            if provider == "discord":
                # Discord delivery remains notification-only; Telegram keyboards
                # have no equivalent authorization model here.
                payload = {"content": message[:1900], "allowed_mentions": {"parse": []}}
                if document is not None:
                    response = await client.post(
                        settings["discord_webhook"],
                        params={"wait": "true"},
                        data={"payload_json": json.dumps(payload)},
                        files={"files[0]": document},
                    )
                else:
                    response = await client.post(
                        settings["discord_webhook"],
                        params={"wait": "true"},
                        json=payload,
                    )
            else:
                if document is not None:
                    data = {"chat_id": settings["telegram_chat_id"], "caption": message[:1000]}
                    if markup_json is not None:
                        data["reply_markup"] = markup_json
                    response = await client.post(
                        f"https://api.telegram.org/bot{settings['telegram_token']}/sendDocument",
                        data=data,
                        files={"document": document},
                    )
                else:
                    payload = {"chat_id": settings["telegram_chat_id"], "text": message[:4000]}
                    if reply_markup is not None:
                        payload["reply_markup"] = reply_markup
                    response = await client.post(
                        f"https://api.telegram.org/bot{settings['telegram_token']}/sendMessage",
                        json=payload,
                    )
            if response.status_code == 429:
                raise NotificationError(f"{provider.title()} rate limit reached. Try again later")
            if not 200 <= response.status_code < 300:
                raise NotificationError(
                    f"{provider.title()} rejected delivery (HTTP {response.status_code}). Check provider credentials and permissions."
                )
            if provider == "telegram":
                body = response.json()
                if not isinstance(body, dict) or not body.get("ok"):
                    raise NotificationError(
                        "Telegram did not confirm delivery. Check bot access to the chat."
                    )
                result = body.get("result")
                if document is not None and (
                    not isinstance(result, dict) or not isinstance(result.get("document"), dict)
                ):
                    raise NotificationError("Telegram did not confirm the report attachment")
            elif document is not None:
                body = response.json()
                attachments = body.get("attachments", []) if isinstance(body, dict) else []
                if not isinstance(attachments, list) or not any(
                    isinstance(item, dict) and item.get("filename") == document[0]
                    for item in attachments
                ):
                    raise NotificationError("Discord did not confirm the report attachment")
    except NotificationError:
        raise
    except (httpx.HTTPError, ValueError):
        raise NotificationError(
            f"{provider.title()} delivery could not be confirmed. Check connectivity and configuration."
        ) from None


def run_message(run: dict, demo: bool = False) -> str:
    from drivecheck.reports import failure_reason

    drive = run["drive"]
    benchmark = run["results"].get("benchmark", {})
    speed = benchmark.get("read_mbps")
    lines = [
        ("[Simulation] " if demo else "") + f"DriveCheck: {run['status']}",
        f"{drive['model']} | Serial: {drive['serial'] or 'unavailable'}",
        f"Profile: {run['profile']} | {run['detail']}",
    ]
    if run["status"] == "failed":
        lines.append(f"Why it failed: {failure_reason(run)}")
    erase = run.get("results", {}).get("erase", {})
    method = erase.get("method") or run.get("erase_method")
    if method:
        lines.append(
            "Erase method: "
            + {
                "ata_secure_erase": "drive firmware secure erase",
                "quick_format_exfat": "quick exFAT format (NOT secure erasure; old files may be recoverable)",
                "full_overwrite": "complete overwrite with read-back verification",
            }.get(method, str(method))
        )
    if erase.get("recovery_required"):
        lines.append("Firmware erase recovery required. Do not power off or remove the drive.")
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


def station_ready_message(
    *,
    booted_at: str,
    platform: str,
    mode: str,
    capabilities: dict,
    queued: int,
) -> str:
    """Describe station readiness without overstating unavailable hardware features."""
    lines = [
        "DriveCheck station ready",
        f"Started: {booted_at}",
        f"Mode: {mode} | Platform: {platform}",
        "Software: ready.",
    ]
    if mode == "demo":
        lines.append("Testing: simulation ready; host drives are not accessed.")
    elif capabilities.get("can_test"):
        lines.append("Testing: read-only drive intake is available.")
    else:
        lines.append("Testing: inventory is available; drive tests are unavailable.")
    lines.append(
        "Safe eject: available."
        if capabilities.get("can_eject")
        else "Safe eject: unavailable on this station."
    )
    lines.append(f"Queue: {queued} intake job{'s' if queued != 1 else ''} queued.")
    limitations = [
        str(item).strip() for item in capabilities.get("limitations", []) if str(item).strip()
    ]
    if limitations:
        summary = "; ".join(limitations[:3])
        lines.append(f"Limitations: {summary[:600]}")
    return "\n".join(lines)
