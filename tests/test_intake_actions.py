import asyncio
import time

import pytest

from drivecheck.config import Config, Settings
from drivecheck.engine import Engine
from drivecheck.hardware import Hardware, SafetyError
from drivecheck.storage import Store


class IntakeHardware(Hardware):
    def __init__(self):
        super().__init__(demo=True)
        self.ejected = 0
        self.self_tests = 0
        self.unsafe = False

    async def self_test(self, drive, progress):
        self.self_tests += 1
        return await super().self_test(drive, progress)

    async def validate(self, drive, destructive=False):
        if self.unsafe:
            raise SafetyError("Drive became mounted")
        return await super().validate(drive, destructive)

    async def eject(self, drive):
        self.ejected += 1
        return await super().eject(drive)


async def station(tmp_path, delay=1):
    config = Config(tmp_path, api_key="test-token-long-enough", notification_wait_seconds=0)
    config.prepare()
    settings = Settings(config)
    settings.value.update(auto_test=True, auto_eject=True, auto_eject_delay_seconds=delay)
    store = Store(tmp_path / "runs.db")
    hardware = IntakeHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    return engine, store, hardware


async def awaiting(engine):
    for _ in range(100):
        runs = engine.store.runs()
        if runs and runs[0].get("workflow_status") == "awaiting_action":
            return runs[0]
        await asyncio.sleep(0.01)
    raise AssertionError("No action window")


async def test_quick_times_out_and_ejects_without_extended_or_writes(tmp_path):
    engine, store, hardware = await station(tmp_path, 0.04)
    try:
        await asyncio.wait_for(engine.queue.join(), 1)
        run = store.runs()[0]
        assert run["profile"] == "quick" and run["automatic"]
        assert set(run["results"]) == {"smart_before", "self_test", "benchmark", "smart_after"}
        assert hardware.self_tests == 0 and hardware.ejected == 1
        assert run["workflow_status"] == "complete"
        with pytest.raises(ValueError, match="expired"):
            await engine.choose_action(run["id"], "extended")
    finally:
        await engine.stop()
        store.close()


async def test_extended_choice_transfers_reservation_then_ejects_once(tmp_path):
    engine, store, hardware = await station(tmp_path)
    try:
        run = await awaiting(engine)
        with pytest.raises(ValueError, match="already"):
            await engine.enqueue(run["drive_id"], "extended")
        response = await engine.choose_action(run["id"], "extended")
        assert response["status"] == "accepted"
        with pytest.raises(ValueError):
            await engine.choose_action(run["id"], "extended")
        await asyncio.wait_for(engine.queue.join(), 1)
        assert len(store.runs()) == 2
        original = store.get(run["id"])
        followup = store.get(original["lifecycle"]["followup_run_id"])
        assert followup["profile"] == "extended" and not followup["automatic"]
        assert followup["lifecycle"]["eject_status"] == "ejected"
        assert hardware.self_tests == 1 and hardware.ejected == 1
        assert original["lifecycle"]["eject_status"] == "not_requested"
    finally:
        await engine.stop()
        store.close()


async def test_eject_choice_is_immediate_and_single_use(tmp_path):
    engine, store, hardware = await station(tmp_path, 180)
    try:
        run = await awaiting(engine)
        await engine.choose_action(run["id"], "eject")
        await asyncio.wait_for(engine.queue.join(), 1)
        assert hardware.ejected == 1 and hardware.self_tests == 0
        with pytest.raises(ValueError):
            await engine.choose_action(run["id"], "extended")
    finally:
        await engine.stop()
        store.close()


async def test_actions_refuse_changed_drive_and_timeout(tmp_path):
    engine, store, hardware = await station(tmp_path, 0.08)
    try:
        run = await awaiting(engine)
        hardware.unsafe = True
        with pytest.raises(SafetyError):
            await engine.choose_action(run["id"], "extended")
        with pytest.raises(ValueError, match="erase"):
            await engine.choose_action(run["id"], "verify")
        await asyncio.wait_for(engine.queue.join(), 1)
        assert hardware.self_tests == 0
    finally:
        await engine.stop()
        store.close()


async def test_deadline_checked_after_slow_identity_validation(tmp_path, monkeypatch):
    engine, store, hardware = await station(tmp_path)
    try:
        run = await awaiting(engine)
        original = hardware.validate

        async def slow(drive, destructive=False):
            engine.action_waits[run["id"]]["deadline"] = time.monotonic() - 1
            return await original(drive, destructive)

        monkeypatch.setattr(hardware, "validate", slow)
        with pytest.raises(ValueError, match="expired"):
            await engine.choose_action(run["id"], "extended")
    finally:
        await engine.stop()
        store.close()


def test_wait_recovery_never_replays_buttons_or_resumes_test(tmp_path):
    store = Store(tmp_path / "runs.db")
    store.save(
        {
            "id": "old",
            "created_at": "2026-10-03",
            "status": "passed",
            "workflow_status": "awaiting_action",
            "lifecycle": {},
        }
    )
    store.recover()
    assert store.get("old")["workflow_status"] == "interrupted"
    assert store.get("old")["status"] == "passed"
    store.close()


async def test_telegram_keyboard_grants_and_expired_notice_without_provider_io(tmp_path):
    engine, store, hardware = await station(tmp_path, 180)
    try:
        run = await awaiting(engine)
        engine.settings.value["notifications"].update(provider="telegram", telegram_chat_id="123")
        from urllib.parse import parse_qs, urlsplit

        from drivecheck.access import AccessLinks

        engine.access_links = AccessLinks(Config(tmp_path, public_origin="http://station.test"))
        markup = engine.telegram_markup(f"{run['id']}:finished")
        buttons = markup["inline_keyboard"]
        assert buttons[0][0]["callback_data"] == f"dc:{run['id']}:extended"
        assert buttons[0][1]["callback_data"] == f"dc:{run['id']}:eject"
        token = parse_qs(urlsplit(buttons[-1][0]["url"]).fragment)["access"][0]
        assert engine.access_links.redeem(token) == run["id"]
        assert token not in str(engine.state())
        engine.settings.value["notifications"]["provider"] = "discord"
        assert engine.telegram_markup(f"{run['id']}:finished") is None
        engine.settings.value["notifications"]["provider"] = "none"
        await engine.choose_action(run["id"], "eject")
        await asyncio.wait_for(engine.queue.join(), 1)
        refreshed = engine.notice_message(f"{run['id']}:finished", "Choose within 180 seconds")
        assert "action window has ended" in refreshed and "180 seconds" not in refreshed
        assert refreshed.startswith("✅ Quick test passed")
        assert "Safe eject: Confirmed" in refreshed
        engine.settings.value["notifications"]["provider"] = "telegram"
        expired = engine.telegram_markup(f"{run['id']}:finished")
        assert all(
            button.get("callback_data", "").endswith(":reconnect") or "callback_data" not in button
            for row in expired["inline_keyboard"]
            for button in row
        )
    finally:
        await engine.stop()
        store.close()


async def test_unexpected_extended_validation_error_still_releases(tmp_path, monkeypatch):
    engine, store, hardware = await station(tmp_path)
    try:
        run = await awaiting(engine)
        await engine.choose_action(run["id"], "extended")
        original = hardware.validate
        raised = False

        async def fail_once(drive, destructive=False):
            nonlocal raised
            if not raised:
                raised = True
                raise RuntimeError("Unexpected adapter response")
            return await original(drive, destructive)

        monkeypatch.setattr(hardware, "validate", fail_once)
        await asyncio.wait_for(engine.queue.join(), 1)
        assert len(store.runs()) == 1
        assert store.get(run["id"])["lifecycle"]["eject_status"] == "ejected"
        assert hardware.ejected == 1 and hardware.self_tests == 0
    finally:
        await engine.stop()
        store.close()


def test_delayed_notice_after_restart_no_longer_invites_actions(tmp_path):
    config = Config(tmp_path, api_key="test-token-long-enough")
    store = Store(tmp_path / "runs.db")
    run = {
        "id": "restart",
        "created_at": "2026-10-03",
        "status": "passed",
        "profile": "quick",
        "automatic": True,
        "detail": "Quick complete",
        "results": {},
        "drive": {"model": "Drive", "serial": "Serial"},
        "workflow_status": "awaiting_action",
        "lifecycle": {"action_deadline": "2026-10-03", "eject_status": "pending"},
    }
    store.save(run)
    store.recover()
    engine = Engine(config, Settings(config), store, IntakeHardware())
    message = engine.notice_message("restart:finished", "Choose within 180 seconds")
    assert "180 seconds" not in message and "window has ended" in message
    assert "not confirmed" in message
    store.close()


async def test_group_buttons_require_sender_but_never_share_sign_in_grants(tmp_path):
    engine, store, hardware = await station(tmp_path, 180)
    try:
        run = await awaiting(engine)
        from drivecheck.access import AccessLinks

        engine.config.public_origin = "https://station.test"
        engine.access_links = AccessLinks(engine.config)
        notice = engine.settings.value["notifications"]
        notice.update(provider="telegram", telegram_chat_id="-123", telegram_user_id="456")
        rows = engine.telegram_markup(f"{run['id']}:finished")["inline_keyboard"]
        assert rows[0][0]["callback_data"].endswith(":extended")
        assert rows[-1][0]["url"] == "https://station.test"
        assert not engine.access_links._links
        notice["telegram_user_id"] = ""
        rows = engine.telegram_markup(f"{run['id']}:finished")["inline_keyboard"]
        assert all("callback_data" not in button for row in rows for button in row)
        notice.update(telegram_chat_id="123", telegram_user_id="456")
        rows = engine.telegram_markup(f"{run['id']}:finished")["inline_keyboard"]
        assert all("callback_data" not in button for row in rows for button in row)
        assert not engine.access_links._links
        notice["telegram_chat_id"] = "@channel"
        assert (
            engine.telegram_markup(f"{run['id']}:finished")["inline_keyboard"][0][0]["url"]
            == "https://station.test"
        )
    finally:
        await engine.stop()
        store.close()


async def test_stop_wins_race_with_choice_event_and_never_starts_extended(tmp_path):
    engine, store, hardware = await station(tmp_path, 180)
    run = await awaiting(engine)
    wait = engine.action_waits[run["id"]]
    wait["choice"] = "extended"
    wait["event"].set()
    async with asyncio.timeout(0.5):
        await engine.stop()
    assert hardware.self_tests == 0 and hardware.ejected == 0
    assert store.get(run["id"])["workflow_status"] == "interrupted"
    assert not engine.action_waits
    store.close()


async def test_stop_during_callback_validation_refuses_orphaned_action(tmp_path, monkeypatch):
    engine, store, hardware = await station(tmp_path, 180)
    run = await awaiting(engine)
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = hardware.validate

    async def slow(drive, destructive=False):
        entered.set()
        await resume.wait()
        return await original(drive, destructive)

    monkeypatch.setattr(hardware, "validate", slow)
    callback = asyncio.create_task(engine.choose_action(run["id"], "extended"))
    await entered.wait()
    await asyncio.wait_for(engine.stop(), 0.5)
    resume.set()
    with pytest.raises(ValueError, match="interrupted"):
        await callback
    assert hardware.self_tests == 0
    store.close()


async def test_manual_quick_receives_same_action_window_and_buttons(tmp_path):
    config = Config(tmp_path, api_key="test-token-long-enough", notification_wait_seconds=0)
    config.prepare()
    settings = Settings(config)
    settings.value.update(auto_test=False, auto_eject=True, auto_eject_delay_seconds=180)
    store = Store(tmp_path / "runs.db")
    hardware = IntakeHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    try:
        queued = await engine.enqueue(engine.drives[0].id, "quick")
        run = await awaiting(engine)
        assert run["id"] == queued["id"] and not run["automatic"]
        assert run["extended_estimate"]["minimum_seconds"] > 0
        assert hardware.ejected == 0
        engine.settings.value["notifications"].update(provider="telegram", telegram_chat_id="123")
        engine.config.allow_destructive = True
        engine.capabilities["can_erase"] = True
        markup = engine.telegram_markup(f"{run['id']}:finished")
        actions = [
            button.get("callback_data", "") for row in markup["inline_keyboard"] for button in row
        ]
        assert f"dc:{run['id']}:extended" in actions
        assert f"dc:{run['id']}:eject" in actions
        erase_rows = [
            row
            for row in markup["inline_keyboard"]
            if any(
                button.get("callback_data", "").endswith(
                    (":quick_erase", ":initialize_disk", ":secure_erase", ":full_erase")
                )
                for button in row
            )
        ]
        assert [[button["text"] for button in row] for row in erase_rows] == [
            ["Quick erase"],
            ["Initialize/reset disk"],
            ["Firmware secure erase"],
            ["Full erase"],
        ]
        await engine.choose_action(run["id"], "eject")
        await asyncio.wait_for(engine.queue.join(), 1)
        assert hardware.ejected == 1
        ready = engine.telegram_markup(f"{run['id']}:ready")
        assert ready["inline_keyboard"][0][0]["callback_data"] == f"dc:{run['id']}:reconnect"
    finally:
        await engine.stop()
        store.close()
