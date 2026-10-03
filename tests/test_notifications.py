import json

import httpx
import pytest

from drivecheck.config import DEFAULT_SETTINGS
from drivecheck.notifications import NotificationError, send


def setting(provider):
    return {**DEFAULT_SETTINGS["notifications"], "provider": provider, "enabled": True,
            "discord_webhook": "https://discord.com/api/webhooks/123/test_secret",
            "telegram_token": "123:token_secret", "telegram_chat_id": "-987"}


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
