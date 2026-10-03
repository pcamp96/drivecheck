"""Provider adapters. Never include tokens or webhook URLs in error messages."""

import json
import re
from datetime import UTC, datetime
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


def _clean_line(value: object, *, fallback: str = "", limit: int = 280) -> str:
    """Keep provider messages short and prevent structured payloads leaking into chat."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return fallback
    if text[:1] in "[{" or "\"json_format_version\"" in text:
        return fallback
    if len(text) > limit:
        text = text[: limit - 1].rstrip(" ,;:") + "…"
    return text


def _profile_name(profile: object) -> str:
    return {
        "quick": "Quick test",
        "extended": "Extended test",
        "verify": "Full verification",
        "quick_erase": "Quick erase",
        "full_erase": "Full erase",
    }.get(str(profile), "Drive test")


def _duration(seconds: object) -> str | None:
    try:
        remaining = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(remaining, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} hr {minutes} min" if minutes else f"{hours} hr"
    if minutes:
        return f"{minutes} min" if not secs else f"{minutes + 1} min"
    return f"{secs} sec"


def _action_remaining(lifecycle: dict) -> str | None:
    explicit = lifecycle.get("action_window_seconds")
    if explicit is not None:
        return _duration(explicit)
    deadline = lifecycle.get("action_deadline")
    if not isinstance(deadline, str):
        return None
    try:
        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = (parsed - datetime.now(UTC)).total_seconds()
    except ValueError:
        return None
    return _duration(seconds) if seconds > 0 else None


def _erase_lines(run: dict) -> list[str]:
    erase = run.get("results", {}).get("erase", {})
    method = erase.get("method") or run.get("erase_method")
    if not method:
        return []
    label = {
        "ata_secure_erase": "Drive firmware secure erase",
        "quick_format_exfat": "Quick exFAT format",
        "full_overwrite": "Complete overwrite with read-back verification",
    }.get(method, _clean_line(method, fallback="Unknown method", limit=80))
    lines = [f"Method: {label}."]
    if method == "quick_format_exfat":
        lines.append("Security: Not a secure erase; old files may be recoverable.")
    return lines


def _extended_estimate(run: dict) -> str | None:
    estimate = run.get("extended_estimate")
    if not isinstance(estimate, dict):
        return None
    duration = _duration(estimate.get("total_seconds"))
    if duration:
        return f"Extended estimate: about {duration}."
    minimum = _duration(estimate.get("minimum_seconds"))
    if minimum:
        return f"Extended estimate: at least {minimum}; full timing unavailable."
    return None


def _started_estimate(run: dict) -> str | None:
    estimate = run.get("estimate")
    timing = run.get("timing", {})
    if isinstance(estimate, dict):
        if estimate.get("total_seconds") is None:
            minimum = _duration(estimate.get("minimum_seconds"))
            if minimum:
                return f"Estimated time: at least {minimum}; full timing unavailable."
            return None
        remaining = _duration(timing.get("remaining_seconds"))
        total = _duration(estimate.get("total_seconds"))
        duration = remaining or total
        return f"Estimated time: about {duration}." if duration else None
    duration = _duration(timing.get("remaining_seconds"))
    return f"Estimated time: about {duration}." if duration else None


def run_message(run: dict, demo: bool = False, event: str | None = None) -> str:
    from drivecheck.reports import failure_reason

    drive = run.get("drive", {})
    results = run.get("results", {})
    benchmark = results.get("benchmark", {})
    speed = benchmark.get("read_mbps")
    lifecycle = run.get("lifecycle", {})
    status = str(run.get("status", "incomplete"))
    profile = _profile_name(run.get("profile"))
    model = _clean_line(drive.get("model"), fallback="Unknown drive", limit=100)
    serial = _clean_line(drive.get("serial"), fallback="Unavailable", limit=100)
    simulation = " [Simulation]" if demo else ""
    recovery = bool(results.get("erase", {}).get("recovery_required"))

    if event is None:
        if status in {"queued", "running"}:
            event = "started"
        elif lifecycle.get("eject_status") == "ejected":
            event = "ready"
        elif lifecycle.get("eject_status") in {"failed", "unsupported"}:
            event = "release_failed"
        else:
            event = "finished"
    if event.startswith("report-"):
        event = "finished"

    identity = [model, f"Serial: {serial}"]
    if event == "ready":
        summary = {
            "passed": "passed",
            "warning": "completed with a warning",
            "failed": "failed",
            "incomplete": "was incomplete",
            "cancelled": "was cancelled",
        }.get(status, status.replace("_", " "))
        return "\n".join(
            [
                f"🟢 Safe to remove{simulation}",
                "",
                *identity,
                "",
                f"Previous result: {profile} {summary}.",
                "You can unplug this drive now.",
            ]
        )

    if event == "release_failed":
        release_detail = _clean_line(
            lifecycle.get("eject_detail"),
            fallback="The station could not confirm a safe eject.",
        )
        result_word = {
            "passed": "passed",
            "warning": "completed with a warning",
            "failed": "failed",
            "incomplete": "was incomplete",
            "cancelled": "was cancelled",
        }.get(status, status.replace("_", " "))
        return "\n".join(
            [
                f"⛔ Eject failed{simulation}",
                "",
                *identity,
                "",
                f"The {profile.lower()} {result_word}, but the drive was not released.",
                f"Reason: {release_detail}",
                "Keep the drive connected and open the dashboard.",
            ]
        )

    if event == "started":
        stage = {
            "quick": "SMART health, short self-test, and read speed.",
            "extended": "SMART health, extended self-test, read speed, and a full read scan.",
            "verify": "A complete write, read-back verification, and final SMART health.",
            "quick_erase": "The confirmed quick erase method.",
            "full_erase": "A complete overwrite with read-back verification.",
        }.get(str(run.get("profile")), "Drive health and read checks.")
        lines = [f"🔎 {profile} started{simulation}", "", *identity, "", f"Checking: {stage}"]
        estimate = _started_estimate(run)
        if estimate:
            lines.append(estimate)
        return "\n".join(lines)

    if recovery:
        lines = [f"⚠️ Firmware erase needs recovery{simulation}", "", *identity, ""]
        lines.extend(_erase_lines(run))
        lines.extend(
            [
                "DO NOT power off or remove this drive.",
                "Open the dashboard and follow the recovery instructions.",
            ]
        )
        return "\n".join(lines)

    heading = {
        "passed": f"✅ {profile} passed",
        "warning": f"⚠️ {profile} completed with a warning",
        "failed": f"❌ {profile} failed",
        "incomplete": f"⚠️ {profile} incomplete",
        "cancelled": f"⏹️ {profile} cancelled",
    }.get(status, f"ℹ️ {profile} {status.replace('_', ' ')}")
    lines = [heading + simulation, "", *identity]
    if status not in {"passed", "warning"}:
        reason = _clean_line(
            failure_reason(run),
            fallback="The check did not provide a readable reason. Open the attached report.",
        )
        lines.extend(["", f"Reason: {reason}"])
    elif status == "warning":
        reason = _clean_line(failure_reason(run))
        if reason and "no specific" not in reason.lower():
            lines.extend(["", f"Warning: {reason}"])

    result_lines = _erase_lines(run)
    if speed is not None:
        try:
            result_lines.append(f"Read speed: {float(speed):.1f} MB/s.")
        except (TypeError, ValueError):
            pass
    if run.get("profile") == "quick" and status in {"passed", "warning"}:
        result_lines.append("Scope: Short SMART test + read sample.")
        estimate = _extended_estimate(run)
        if estimate:
            result_lines.append(estimate)
    if result_lines:
        lines.extend(["", *result_lines])

    if lifecycle.get("eject_status") == "pending":
        remaining = _action_remaining(lifecycle)
        if run.get("workflow_status") == "awaiting_action" and remaining:
            lines.extend(["", f"Choose the next action below. Auto-eject in {remaining}."])
        else:
            lines.extend(["", "Safe eject is in progress.", "Wait for the green Safe to remove message."])
    elif lifecycle.get("eject_status") == "ejected":
        lines.extend(["", "Safe eject: Confirmed."])
    elif lifecycle.get("eject_status") == "not_requested":
        lines.extend(["", "The drive is still connected."])
    elif lifecycle.get("eject_status") in {"failed", "unsupported", "interrupted", "unknown"}:
        lines.extend(
            [
                "",
                "Safe eject was not confirmed.",
                "Keep the drive connected and check the dashboard.",
            ]
        )
    return "\n".join(lines)


def station_ready_message(
    *,
    booted_at: str,
    platform: str,
    mode: str,
    capabilities: dict,
    queued: int,
    auto_test: bool = True,
) -> str:
    """Describe station readiness without overstating unavailable hardware features."""
    del booted_at
    lines = ["🟢 DriveCheck is ready", ""]
    if mode == "demo":
        lines.append("Simulation mode is ready. Host drives will not be accessed.")
    elif capabilities.get("can_test") and auto_test:
        lines.append("Dock a drive to start its read-only Quick test.")
    elif capabilities.get("can_test"):
        lines.append("Open the dashboard to start a drive test.")
    else:
        lines.append("Drive inventory is available, but testing is unavailable on this station.")
    lines.append(f"Station: {_clean_line(platform, fallback='Unknown platform', limit=100)}.")
    if not capabilities.get("can_eject"):
        lines.append("Safe eject is unavailable on this station.")
    if queued:
        lines.append(f"Waiting: {queued} queued intake job{'s' if queued != 1 else ''}.")
    limitations = [
        str(item).strip() for item in capabilities.get("limitations", []) if str(item).strip()
    ]
    if limitations:
        summary = "; ".join(limitations[:3])
        lines.append(f"Limited: {_clean_line(summary, limit=300)}")
    return "\n".join(lines)
