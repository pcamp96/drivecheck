import json
from types import SimpleNamespace

import httpx

from drivecheck.hardware import SafetyError
from drivecheck.telegram import TelegramInterface

RUN_ID = "a" * 32


class Engine:
    def __init__(self, *, chat_id="-100", user_id="42"):
        self.settings = SimpleNamespace(
            value={
                "notifications": {
                    "enabled": True,
                    "provider": "telegram",
                    "telegram_token": "123:secret_token",
                    "telegram_chat_id": chat_id,
                    "telegram_user_id": user_id,
                }
            }
        )
        self.telegram_error = None
        self.published = 0
        self.actions = []

    def publish(self):
        self.published += 1

    async def choose_action(self, run_id, action):
        self.actions.append((run_id, action))
        return {"status": "accepted"}


def callback(update_id, *, action="extended", chat=-100, user=42, callback_id="callback"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user, "username": "not-authority"},
            "message": {"chat": {"id": chat}},
            "data": f"dc:{RUN_ID}:{action}",
        },
    }


async def test_initial_backlog_is_discarded_then_authorized_action_runs():
    engine = Engine()
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/getUpdates"):
            body = json.loads(request.content)
            if body["timeout"] == 0:
                assert body["offset"] == -1
                return httpx.Response(200, json={"ok": True, "result": [callback(10)]})
            assert body["offset"] == 11
            return httpx.Response(
                200, json={"ok": True, "result": [callback(11, callback_id="new")]}
            )
        body = json.loads(request.content)
        assert body["callback_query_id"] == "new"
        assert body["text"] == "Extended test requested."
        return httpx.Response(200, json={"ok": True, "result": True})

    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler), poll_timeout=1)
    assert await interface.poll_once()
    assert engine.actions == []
    assert await interface.poll_once()
    assert engine.actions == [(RUN_ID, "extended")]
    engine.settings.value["notifications"]["telegram_token"] = "456:new_token"
    assert await interface.poll_once()
    assert engine.actions == [(RUN_ID, "extended")]
    assert len(calls) == 4


async def test_settings_revoked_during_long_poll_discard_old_authorized_callback():
    engine = Engine()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            assert body["timeout"] == 0
            return httpx.Response(200, json={"ok": True, "result": []})
        if calls == 2:
            assert body["timeout"] == 1
            engine.settings.value["notifications"].update(
                telegram_token="456:new_token",
                telegram_chat_id="-200",
                telegram_user_id="99",
            )
            return httpx.Response(200, json={"ok": True, "result": [callback(20)]})
        assert "bot456:new_token" in request.url.path
        assert body["timeout"] == 0
        assert body["offset"] == -1
        return httpx.Response(
            200,
            json={"ok": True, "result": [callback(21, chat=-200, user=99)]},
        )

    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler), poll_timeout=1)
    assert await interface.poll_once()
    assert not await interface.poll_once()
    assert engine.actions == []
    assert interface.offset is None
    assert interface._signature is None
    assert await interface.poll_once()
    assert engine.actions == []


async def test_chat_and_sender_must_both_match_numeric_configuration():
    engine = Engine()
    answers = []
    round_number = 0

    def handler(request):
        nonlocal round_number
        if request.url.path.endswith("/getUpdates"):
            round_number += 1
            updates = (
                []
                if round_number == 1
                else [
                    callback(1, chat=-999, callback_id="wrong-chat"),
                    callback(2, user=99, callback_id="wrong-user"),
                ]
            )
            return httpx.Response(200, json={"ok": True, "result": updates})
        answers.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler), poll_timeout=1)
    await interface.poll_once()
    await interface.poll_once()
    assert engine.actions == []
    assert [answer["callback_query_id"] for answer in answers] == ["wrong-chat", "wrong-user"]
    assert all(answer["text"].startswith("Not authorized") for answer in answers)


async def test_private_chat_defaults_sender_and_erase_never_executes():
    engine = Engine(chat_id="42", user_id="")
    answers = []
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path.endswith("/getUpdates"):
            count += 1
            updates = [] if count == 1 else [callback(3, action="erase", chat=42, user=42)]
            return httpx.Response(200, json={"ok": True, "result": updates})
        answers.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler), poll_timeout=1)
    await interface.poll_once()
    await interface.poll_once()
    assert engine.actions == []
    assert answers[0]["show_alert"] is True
    assert "dashboard" in answers[0]["text"]


async def test_group_requires_user_id_without_calling_provider():
    engine = Engine(user_id="")
    waits = []

    def handler(request):
        raise AssertionError("invalid authorization must not contact Telegram")

    async def wait(seconds):
        waits.append(seconds)

    interface = TelegramInterface(
        engine, transport=httpx.MockTransport(handler), sleep=wait, error_delay=0.25
    )
    assert not await interface.poll_once()
    assert "numeric Telegram user ID" in engine.telegram_error
    assert waits == [0.25]


async def test_missing_token_uses_bounded_configuration_backoff():
    engine = Engine()
    engine.settings.value["notifications"]["telegram_token"] = ""
    waits = []

    async def wait(seconds):
        waits.append(seconds)

    interface = TelegramInterface(engine, sleep=wait, error_delay=0.5)
    assert not await interface.poll_once()
    assert waits == [0.5]
    assert "bot token" in engine.telegram_error


async def test_safety_error_is_acknowledged_without_exposing_detail():
    engine = Engine()

    async def unsafe(_run_id, _action):
        raise SafetyError("replacement serial SECRET should stay private")

    engine.choose_action = unsafe
    answers = []
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path.endswith("/getUpdates"):
            count += 1
            updates = [] if count == 1 else [callback(5)]
            return httpx.Response(200, json={"ok": True, "result": updates})
        answers.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler), poll_timeout=1)
    await interface.poll_once()
    await interface.poll_once()
    assert answers[0]["text"] == "Action unavailable. Open the dashboard for current status."
    assert "SECRET" not in answers[0]["text"]


async def test_webhook_conflict_is_reported_without_token_or_provider_body():
    engine = Engine()

    def handler(request):
        return httpx.Response(
            409,
            json={"ok": False, "description": "secret_token webhook collision"},
        )

    async def no_wait(_seconds):
        return None

    interface = TelegramInterface(
        engine, transport=httpx.MockTransport(handler), sleep=no_wait, error_delay=0.1
    )
    assert not await interface.poll_once()
    assert "webhook" in engine.telegram_error
    assert "another active poller" in engine.telegram_error
    assert "secret_token" not in engine.telegram_error
