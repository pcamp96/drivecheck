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
        self.erase_begins = []
        self.erase_confirms = []

    def publish(self):
        self.published += 1

    async def choose_action(self, run_id, action):
        self.actions.append((run_id, action))
        return {"status": "accepted"}

    async def begin_erase(self, run_id, profile, *, chat_id, user_id):
        self.erase_begins.append((run_id, profile, chat_id, user_id))
        return {
            "intent_id": f"intent-{profile}",
            "message": f"Confirm {profile}: reply with the exact phrase.",
            "expires_in": 120,
        }

    async def confirm_erase(self, intent_id, confirmation, *, chat_id, user_id):
        self.erase_confirms.append((intent_id, confirmation, chat_id, user_id))
        if confirmation == "wrong":
            raise ValueError("The erase phrase must match exactly")
        return {"status": "queued"}


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


def reply(update_id, prompt_id, text, *, chat=-100, user=42):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 1000,
            "from": {"id": user},
            "chat": {"id": chat},
            "reply_to_message": {"message_id": prompt_id},
            "text": text,
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


async def test_erase_buttons_only_create_intent_until_authorized_prompt_reply():
    engine = Engine()
    sent = []
    answers = []
    next_message_id = 700

    def handler(request):
        nonlocal next_message_id
        body = json.loads(request.content)
        if request.url.path.endswith("/sendMessage"):
            sent.append(body)
            next_message_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": next_message_id}})
        answers.append(body)
        return httpx.Response(200, json={"ok": True, "result": True})

    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler))
    settings = engine.settings.value["notifications"]
    signature = ("123:secret_token", "-100", "42")

    for index, profile in enumerate(("quick_erase", "full_erase"), 1):
        confirms_before = len(engine.erase_confirms)
        assert await interface._handle_update(
            "123:secret_token",
            settings,
            callback(index, action=profile, callback_id=f"erase-{index}"),
            signature,
        )
        assert len(engine.erase_confirms) == confirms_before
        prompt_id = next_message_id
        assert sent[-1]["reply_markup"] == {"force_reply": True, "selective": True}
        assert answers[-1]["text"].startswith("Confirmation required")

        # Only the configured principal replying to this bot prompt can advance it.
        assert await interface._handle_update(
            "123:secret_token", settings, reply(10, prompt_id, "phrase", chat=-999), signature
        )
        assert await interface._handle_update(
            "123:secret_token", settings, reply(11, prompt_id, "phrase", user=99), signature
        )
        assert await interface._handle_update(
            "123:secret_token", settings, reply(12, prompt_id + 1, "phrase"), signature
        )
        assert len(engine.erase_confirms) == confirms_before

        assert await interface._handle_update(
            "123:secret_token", settings, reply(13, prompt_id, "wrong"), signature
        )
        assert (-100, 42, prompt_id) in interface._pending
        assert "did not match exactly" in sent[-1]["text"]

        phrase = "QUICK ERASE SERIAL" if profile == "quick_erase" else "FULL ERASE SERIAL"
        assert await interface._handle_update(
            "123:secret_token", settings, reply(14, prompt_id, phrase), signature
        )
        assert (-100, 42, prompt_id) not in interface._pending
        assert sent[-1]["text"].startswith("Erase requested")
        confirms = len(engine.erase_confirms)
        assert await interface._handle_update(
            "123:secret_token", settings, reply(15, prompt_id, phrase), signature
        )
        assert len(engine.erase_confirms) == confirms

    assert [call[1] for call in engine.erase_begins] == ["quick_erase", "full_erase"]


async def test_pending_erase_expires_replaces_older_sender_prompt_and_clears_on_settings_change(
    monkeypatch,
):
    now = 100.0
    monkeypatch.setattr("drivecheck.telegram.time.monotonic", lambda: now)
    engine = Engine()
    message_id = 800

    def handler(request):
        nonlocal message_id
        if request.url.path.endswith("/sendMessage"):
            message_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
        return httpx.Response(200, json={"ok": True, "result": True})

    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler), poll_timeout=1)
    settings = engine.settings.value["notifications"]
    signature = ("123:secret_token", "-100", "42")
    await interface._handle_update(
        "123:secret_token", settings, callback(1, action="quick_erase"), signature
    )
    first_prompt = message_id
    await interface._handle_update(
        "123:secret_token", settings, callback(2, action="full_erase"), signature
    )
    assert (-100, 42, first_prompt) not in interface._pending
    assert len(interface._pending) == 1
    second_prompt = message_id
    now += 121
    await interface._handle_update(
        "123:secret_token", settings, reply(3, second_prompt, "FULL ERASE SERIAL"), signature
    )
    assert interface._pending == {}
    assert engine.erase_confirms == []

    now = 300
    await interface._handle_update(
        "123:secret_token", settings, callback(4, action="quick_erase"), signature
    )
    assert interface._pending
    engine.settings.value["notifications"]["telegram_token"] = "456:new_token"
    interface._signature = signature

    async def no_wait(_seconds):
        return None

    interface.sleep = no_wait
    await interface.poll_once()
    assert interface._pending == {}


async def test_settings_revocation_during_erase_begin_or_confirm_sends_no_stale_reply():
    engine = Engine()
    provider_calls = []

    def handler(request):
        provider_calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 901}})

    async def revoked_begin(run_id, profile, *, chat_id, user_id):
        engine.settings.value["notifications"]["telegram_token"] = "456:new_token"
        return {"intent_id": "intent", "message": "secret-free prompt", "expires_in": 120}

    engine.begin_erase = revoked_begin
    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler))
    settings = engine.settings.value["notifications"]
    signature = ("123:secret_token", "-100", "42")
    assert not await interface._handle_update(
        "123:secret_token", settings, callback(1, action="quick_erase"), signature
    )
    assert provider_calls == [] and interface._pending == {}

    engine = Engine()
    provider_calls = []
    interface = TelegramInterface(engine, transport=httpx.MockTransport(handler))
    settings = engine.settings.value["notifications"]
    await interface._handle_update(
        "123:secret_token", settings, callback(2, action="quick_erase"), signature
    )
    prompt_id = next(iter(interface._pending))[2]
    provider_calls.clear()

    async def revoked_confirm(intent_id, confirmation, *, chat_id, user_id):
        engine.settings.value["notifications"]["telegram_token"] = "456:new_token"
        return {"status": "queued"}

    engine.confirm_erase = revoked_confirm
    assert not await interface._handle_update(
        "123:secret_token", settings, reply(3, prompt_id, "QUICK ERASE SERIAL"), signature
    )
    assert provider_calls == [] and interface._pending == {}


async def test_pending_prompt_memory_is_bounded_and_send_errors_are_sanitized():
    engine = Engine()
    interface = TelegramInterface(engine)
    for user_id in range(1, 102):
        interface._remember_prompt(-100, user_id, user_id, f"intent-{user_id}", 120)
    assert len(interface._pending) == 100
    assert (-100, 1, 1) not in interface._pending

    def handler(_request):
        return httpx.Response(
            400,
            json={"ok": False, "description": "secret_token and private provider detail"},
        )

    interface.transport = httpx.MockTransport(handler)
    assert await interface._send("123:secret_token", -100, "Prompt") is None
    assert engine.telegram_error == "Telegram did not confirm the interactive message."
    assert "secret" not in engine.telegram_error
