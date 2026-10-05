import re
import time
from dataclasses import replace

import pytest

from drivecheck.config import Config, Settings
from drivecheck.engine import Engine
from drivecheck.hardware import Drive, Hardware
from drivecheck.storage import Store

ORIGINAL_RUN_ID = "f" * 32


class ReconnectHardware(Hardware):
    def __init__(self):
        super().__init__(demo=True)
        self.current: Drive | None = Drive(**self._DEMO_DRIVE.to_dict())
        self.release_calls = 0

    async def discover(self):
        return [Drive(**self.current.to_dict())] if self.current is not None else []

    async def unmount(self, drive):
        self.release_calls += 1
        raise AssertionError("reconnect must not mount or unmount a filesystem")

    async def eject(self, drive):
        self.release_calls += 1
        raise AssertionError("reconnect must not reset or eject hardware")


def completed_run(drive: Drive) -> dict:
    return {
        "id": ORIGINAL_RUN_ID,
        "drive_id": drive.id,
        "drive": drive.to_dict(),
        "profile": "quick",
        "automatic": True,
        "status": "passed",
        "phase": "complete",
        "progress": 100,
        "detail": "Quick test passed.",
        "created_at": "2026-10-03T12:00:00+00:00",
        "started_at": "2026-10-03T12:00:01+00:00",
        "finished_at": "2026-10-03T12:02:00+00:00",
        "results": {},
        "logs": [],
        "workflow_status": "complete",
        "lifecycle": {
            "notification_status": "sent",
            "eject_status": "ejected",
            "eject_detail": "Drive power-off was confirmed.",
        },
    }


async def station(tmp_path):
    config = Config(tmp_path, api_key="test-token-long-enough")
    config.prepare()
    settings = Settings(config)
    settings.value.update(auto_test=False, auto_eject=False)
    settings.value["notifications"].update(
        enabled=True,
        provider="telegram",
        telegram_token="123:test-token",
        telegram_chat_id="42",
        telegram_user_id="42",
    )
    store = Store(tmp_path / "runs.db")
    hardware = ReconnectHardware()
    original = completed_run(hardware.current)
    store.save(original)
    engine = Engine(config, settings, store, hardware)
    await engine.scan()
    return engine, store, hardware, original


def callback_context(result: dict) -> tuple[str, dict]:
    assert result["status"] == "ready"
    assert result["message"] and len(result["message"]) < 500
    assert isinstance(result["reply_markup"], dict)
    callback_data = [
        button["callback_data"]
        for row in result["reply_markup"]["inline_keyboard"]
        for button in row
        if "callback_data" in button and button["callback_data"].endswith((":quick", ":extended"))
    ]
    actions = {data.rsplit(":", 1)[1] for data in callback_data}
    nonces = {data.split(":", 2)[1] for data in callback_data}
    assert actions == {"quick", "extended"}
    assert len(nonces) == 1
    nonce = nonces.pop()
    assert re.fullmatch(r"[0-9a-f]{32}", nonce)
    assert nonce != ORIGINAL_RUN_ID
    return nonce, result["reply_markup"]


async def test_completed_eligible_report_gets_fresh_one_use_quick_context(tmp_path):
    engine, store, hardware, original = await station(tmp_path)
    try:
        nonce, _ = callback_context(await engine.reconnect_run(original["id"]))
        context = engine.reconnect_contexts[nonce]
        assert context["run_id"] == original["id"]
        assert context["drive"]["identity"] == original["drive"]["identity"]
        assert 295 <= context["deadline"] - time.monotonic() <= 300

        # An old report callback never becomes a test authorization merely
        # because the same drive was reconnected.
        with pytest.raises(ValueError):
            await engine.choose_action(original["id"], "extended")

        response = await engine.choose_action(nonce, "quick")
        assert response["status"] in {"accepted", "queued"}
        queued = store.runs()[0]
        assert queued["id"] != original["id"]
        assert queued["profile"] == "quick"
        assert queued["drive"]["identity"] == original["drive"]["identity"]
        assert nonce not in engine.reconnect_contexts
        assert engine.queue.qsize() == 1
        with pytest.raises(ValueError):
            await engine.choose_action(nonce, "quick")
        assert engine.queue.qsize() == 1
        assert hardware.release_calls == 0
    finally:
        store.close()


async def test_reconnect_reports_missing_and_blocked_without_hardware_side_effects(tmp_path):
    engine, store, hardware, original = await station(tmp_path)
    try:
        hardware.current = None
        missing = await engine.reconnect_run(original["id"])
        assert missing["status"] == "needs_reconnect"
        assert missing["message"]
        assert isinstance(missing["reply_markup"], dict)
        assert not engine.reconnect_contexts
        assert engine.queue.empty()

        hardware.current = replace(
            Drive(**original["drive"]),
            eligible=False,
            mounted=True,
            reasons=["mounted"],
        )
        blocked = await engine.reconnect_run(original["id"])
        assert blocked["status"] == "blocked"
        assert blocked["message"]
        assert isinstance(blocked["reply_markup"], dict)
        assert not engine.reconnect_contexts
        assert engine.queue.empty()
        assert hardware.release_calls == 0
    finally:
        store.close()


async def test_reconnect_detects_auto_intake_queue_without_duplicate(tmp_path):
    engine, store, hardware, original = await station(tmp_path)
    try:
        engine.settings.value["auto_test"] = True
        engine.auto_attempted.clear()
        response = await engine.reconnect_run(original["id"])
        assert response["status"] == "busy"
        assert response["message"]
        assert isinstance(response["reply_markup"], dict)
        queued = [run for run in store.runs() if run["status"] == "queued"]
        assert len(queued) == 1
        assert queued[0]["profile"] == "quick" and queued[0]["automatic"]
        assert engine.queue.qsize() == 1
        assert not engine.reconnect_contexts

        again = await engine.reconnect_run(original["id"])
        assert again["status"] == "busy"
        assert len([run for run in store.runs() if run["status"] == "queued"]) == 1
        assert engine.queue.qsize() == 1
    finally:
        store.close()


async def test_reconnect_context_expires_before_extended_can_queue(tmp_path):
    engine, store, _hardware, original = await station(tmp_path)
    try:
        nonce, _ = callback_context(await engine.reconnect_run(original["id"]))
        engine.reconnect_contexts[nonce]["deadline"] = time.monotonic() - 1
        with pytest.raises(ValueError, match="expired|valid"):
            await engine.choose_action(nonce, "extended")
        assert engine.queue.empty()
        assert len(store.runs()) == 1
    finally:
        store.close()


@pytest.mark.parametrize("replacement", ["path", "identity"])
async def test_reconnect_context_rejects_changed_drive(tmp_path, replacement):
    engine, store, hardware, original = await station(tmp_path)
    try:
        nonce, _ = callback_context(await engine.reconnect_run(original["id"]))
        connected = Drive(**original["drive"])
        if replacement == "path":
            hardware.current = replace(connected, path="/dev/drivecheck-replacement")
        else:
            hardware.current = replace(
                connected,
                identity="0" * 64,
                serial="REPLACEMENT-SERIAL",
            )
        with pytest.raises((ValueError, RuntimeError)):
            await engine.choose_action(nonce, "extended")
        assert engine.queue.empty()
        assert len(store.runs()) == 1
    finally:
        store.close()


async def test_context_expiry_during_estimate_cannot_queue(tmp_path, monkeypatch):
    engine, store, _hardware, original = await station(tmp_path)
    try:
        nonce, _ = callback_context(await engine.reconnect_run(original["id"]))
        estimate = engine.test_estimate

        async def expire(drive_id, profile):
            result = await estimate(drive_id, profile)
            engine.reconnect_contexts[nonce]["deadline"] = time.monotonic() - 1
            return result

        monkeypatch.setattr(engine, "test_estimate", expire)
        with pytest.raises(ValueError, match="expired"):
            await engine.choose_action(nonce, "extended")
        assert engine.queue.empty()
        assert len(store.runs()) == 1
    finally:
        store.close()


async def test_reconnected_new_path_can_confirm_erase_but_buttons_remain_one_use(tmp_path):
    engine, store, hardware, original = await station(tmp_path)
    try:
        engine.config.allow_destructive = True
        engine.capabilities["can_erase"] = True
        hardware.current = replace(hardware.current, path="/dev/new-path")
        nonce, markup = callback_context(await engine.reconnect_run(original["id"]))
        assert any(
            button.get("callback_data") == f"dc:{nonce}:quick_erase"
            for row in markup["inline_keyboard"]
            for button in row
        )
        intent = await engine.begin_erase(nonce, "quick_erase", chat_id=42, user_id=42)
        assert engine.erase_intents[intent["intent_id"]]["drive"]["path"] == "/dev/new-path"
        result = await engine.confirm_erase(
            intent["intent_id"],
            "QUICK FORMAT " + hardware.current.serial + " /dev/demo1",
            chat_id=42,
            user_id=42,
        )
        assert result["status"] == "queued" and result["run"]["profile"] == "quick_erase"
        assert nonce not in engine.reconnect_contexts
        with pytest.raises(ValueError):
            await engine.choose_action(nonce, "quick")
        assert engine.queue.qsize() == 1
        assert hardware.release_calls == 0
    finally:
        store.close()


async def test_dashboard_job_invalidates_previously_offered_telegram_controls(tmp_path):
    engine, store, _hardware, original = await station(tmp_path)
    try:
        nonce, _ = callback_context(await engine.reconnect_run(original["id"]))
        queued = await engine.enqueue(original["drive_id"], "quick")
        assert nonce not in engine.reconnect_contexts
        queued.update(status="passed", workflow_status="complete")
        store.save(queued)
        with pytest.raises(ValueError):
            await engine.choose_action(nonce, "extended")
        assert engine.queue.qsize() == 1
        assert len(store.runs()) == 2
    finally:
        store.close()
