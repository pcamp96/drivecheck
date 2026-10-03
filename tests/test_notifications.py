import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from drivecheck.config import DEFAULT_SETTINGS
from drivecheck.notifications import NotificationError, send


def setting(provider):
    return {
        **DEFAULT_SETTINGS["notifications"],
        "provider": provider,
        "enabled": True,
        "discord_webhook": "https://discord.com/api/webhooks/123/test_secret",
        "telegram_token": "123:token_secret",
        "telegram_chat_id": "-987",
    }


async def test_discord_confirmed_delivery_and_mentions_disabled():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "message"})

    await send(setting("discord"), "Drive passed @everyone", httpx.MockTransport(handler))
    assert captured[0].url.params["wait"] == "true"
    assert json.loads(captured[0].content)["allowed_mentions"] == {"parse": []}


async def test_telegram_payload_and_application_failure():
    def handler(request):
        body = json.loads(request.content)
        assert body["chat_id"] == "-987"
        assert body["text"] == "Drive passed"
        assert "parse_mode" not in body
        return httpx.Response(200, json={"ok": False, "description": "contains a token secret"})

    with pytest.raises(NotificationError, match="did not confirm") as error:
        await send(setting("telegram"), "Drive passed", httpx.MockTransport(handler))
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_provider_http_failures_safe_and_retryable(status):
    def handler(request):
        return httpx.Response(status, json={"secret": "do not log me"})

    with pytest.raises(NotificationError) as error:
        await send(setting("discord"), "message", httpx.MockTransport(handler))
    assert "test_secret" not in str(error.value)
    assert "do not log me" not in str(error.value)


async def test_network_errors_do_not_expose_token_url():
    def handler(request):
        raise httpx.ConnectError(f"connect to {request.url} failed", request=request)

    with pytest.raises(NotificationError) as error:
        await send(setting("telegram"), "message", httpx.MockTransport(handler))
    assert "token_secret" not in str(error.value)


@pytest.mark.parametrize("provider", ["telegram", "discord"])
async def test_readable_attachment_and_reason_delivered_in_one_request(provider):
    captured = []
    report = "DriveCheck report\nWhy it failed: read failure at LBA 622,728.\n"
    caption = "DriveCheck: failed\nWhy it failed: drive could not read its surface."

    def handler(request):
        captured.append(request)
        mime = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
        )
        parts = {
            part.get_param("name", header="content-disposition"): part for part in mime.iter_parts()
        }
        field = "document" if provider == "telegram" else "files[0]"
        assert parts[field].get_filename() == "drivecheck-test.txt"
        assert parts[field].get_payload(decode=True).decode("utf-8") == report
        if provider == "telegram":
            assert request.url.path.endswith("/sendDocument")
            assert parts["caption"].get_payload(decode=True).decode() == caption
            assert parts["chat_id"].get_payload(decode=True).decode() == "-987"
            assert "parse_mode" not in parts
            return httpx.Response(
                200, json={"ok": True, "result": {"document": {"file_name": "drivecheck-test.txt"}}}
            )
        payload = json.loads(parts["payload_json"].get_payload(decode=True))
        assert payload["content"] == caption
        assert payload["allowed_mentions"] == {"parse": []}
        assert request.url.params["wait"] == "true"
        return httpx.Response(
            200, json={"id": "confirmed", "attachments": [{"filename": "drivecheck-test.txt"}]}
        )

    await send(
        setting(provider),
        caption,
        httpx.MockTransport(handler),
        attachment={"filename": "drivecheck-test.txt", "text": report},
    )
    assert len(captured) == 1


async def test_telegram_document_failure_remains_retryable_without_leaking_credentials():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "token_secret"})

    with pytest.raises(NotificationError, match="did not confirm") as error:
        await send(
            setting("telegram"),
            "Failed report",
            httpx.MockTransport(handler),
            attachment={"filename": "report.txt", "text": "Read failure"},
        )
    assert "token_secret" not in str(error.value)


async def test_invalid_attachment_name_never_calls_provider():
    def handler(request):
        raise AssertionError("Invalid file must not be sent")

    with pytest.raises(NotificationError, match="attachment is invalid"):
        await send(
            setting("telegram"),
            "message",
            httpx.MockTransport(handler),
            attachment={"filename": "../private.txt", "text": "report"},
        )


@pytest.mark.parametrize("provider", ["telegram", "discord"])
async def test_delivery_is_not_confirmed_if_provider_omits_attachment(provider):
    def handler(request):
        return httpx.Response(
            200, json={"ok": True, "result": {}, "id": "message", "attachments": []}
        )

    with pytest.raises(NotificationError, match="did not confirm the report attachment"):
        await send(
            setting(provider),
            "Failed report",
            httpx.MockTransport(handler),
            attachment={"filename": "report.txt", "text": "Read failure"},
        )
