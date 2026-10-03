import asyncio

import pytest

from drivecheck import notifications
from drivecheck.config import Config, Settings
from drivecheck.engine import AUTO_DETACH_SCANS, Engine
from drivecheck.hardware import Hardware
from drivecheck.storage import Store


class HeadlessHardware(Hardware):
    def __init__(self):
        super().__init__(demo=True)
        self.present = True
        self.unmount_calls = 0
        self.eject_calls = 0

    def capabilities(self):
        return {
            "platform": "test",
            "can_test": True,
            "can_verify": False,
            "can_unmount": True,
            "can_eject": True,
            "tools": {},
            "limitations": [],
        }

    async def discover(self):
        return await super().discover() if self.present else []

    async def unmount(self, drive):
        self.unmount_calls += 1
        return {"status": "unmounted", "detail": "No mounted filesystems remained."}

    async def eject(self, drive):
        self.eject_calls += 1
        return {"status": "ejected", "detail": "Drive power-off was confirmed."}


def configured(tmp_path, *, headless=True, wait=0.5):
    config = Config(tmp_path, api_key="test-token-long-enough")
    config.headless = headless
    config.notification_wait_seconds = wait
    config.prepare()
    settings = Settings(config)
    settings.value["notifications"].update(
        provider="discord",
        enabled=True,
        discord_webhook="https://discord.com/api/webhooks/123/test_token",
    )
    store = Store(tmp_path / "runs.db")
    return config, settings, store


async def test_headless_requires_configured_notifications_before_scan(tmp_path):
    config = Config(tmp_path, api_key="test-token-long-enough")
    config.headless = True
    config.notification_wait_seconds = 0
    config.prepare()
    store = Store(tmp_path / "runs.db")
    hardware = HeadlessHardware()
    engine = Engine(config, Settings(config), store, hardware)
    with pytest.raises(RuntimeError, match="requires an enabled"):
        await engine.start()
    assert engine.drives == []
    assert hardware.eject_calls == 0
    store.close()


async def test_headless_rejects_malformed_saved_notification_settings(tmp_path):
    config, settings, store = configured(tmp_path)
    settings.value["notifications"]["discord_webhook"] = "http://example.test/hook"
    engine = Engine(config, settings, store, HeadlessHardware())
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        await engine.start()
    assert engine.drives == []
    store.close()


async def test_headless_delivers_then_unmounts_ejects_and_sends_ready(tmp_path, monkeypatch):
    delivered = []

    async def send(settings, message, transport=None):
        delivered.append(message)

    monkeypatch.setattr(notifications, "send", send)
    config, settings, store = configured(tmp_path)
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    try:
        await asyncio.wait_for(engine.queue.join(), 2)
        run = store.runs()[0]
        assert run["profile"] == "extended"
        assert run["status"] == "passed"
        assert run["workflow_status"] == "complete"
        assert run["lifecycle"] == {
            "notification_status": "sent",
            "eject_status": "ejected",
            "eject_detail": "Drive power-off was confirmed.",
        }
        assert hardware.unmount_calls == 0
        assert hardware.eject_calls == 1
        assert "Safe eject pending" in delivered[0]
        for _ in range(20):
            if any("ready to remove" in message for message in delivered):
                break
            await asyncio.sleep(0.01)
        assert any("ready to remove" in message for message in delivered)
        state = engine.state()
        assert state["settings"]["auto_test"] is True
        assert state["settings"]["auto_eject"] is True
        assert state["system"]["platform"] == "test"
    finally:
        await engine.stop()
        store.close()


async def test_notification_timeout_retains_outbox_but_still_ejects(tmp_path, monkeypatch):
    async def fail(settings, message, transport=None):
        raise notifications.NotificationError("offline")

    monkeypatch.setattr(notifications, "send", fail)
    config, settings, store = configured(tmp_path, wait=0.01)
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    try:
        await asyncio.wait_for(engine.queue.join(), 2)
        run = store.runs()[0]
        assert run["lifecycle"]["notification_status"] == "pending"
        assert run["lifecycle"]["eject_status"] == "ejected"
        notice = store.notice_state(f"{run['id']}:finished")
        assert notice is not None and notice["delivered"] is False
        assert hardware.eject_calls == 1
    finally:
        await engine.stop()
        store.close()


async def test_non_headless_auto_eject_works_without_notifications(tmp_path):
    config = Config(tmp_path, api_key="test-token-long-enough")
    config.prepare()
    settings = Settings(config)
    settings.value["auto_eject"] = True
    store = Store(tmp_path / "runs.db")
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    try:
        run = await engine.enqueue(engine.drives[0].id, "extended")
        await asyncio.wait_for(engine.queue.join(), 1)
        saved = store.get(run["id"])
        assert saved["status"] == "passed"
        assert saved["workflow_status"] == "complete"
        assert saved["lifecycle"]["notification_status"] == "disabled"
        assert saved["lifecycle"]["eject_status"] == "ejected"
        assert hardware.eject_calls == 1
    finally:
        await engine.stop()
        store.close()


async def test_cancelled_headless_test_never_auto_ejects(tmp_path, monkeypatch):
    class SlowHeadless(HeadlessHardware):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()

        async def self_test(self, drive, progress):
            self.entered.set()
            await asyncio.sleep(100)

    async def send(settings, message, transport=None):
        return None

    monkeypatch.setattr(notifications, "send", send)
    config, settings, store = configured(tmp_path)
    hardware = SlowHeadless()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    try:
        await asyncio.wait_for(hardware.entered.wait(), 1)
        run = store.runs()[0]
        await engine.cancel(run["id"])
        saved = store.get(run["id"])
        assert saved["status"] == "cancelled"
        assert saved["lifecycle"]["eject_status"] == "not_requested"
        assert hardware.unmount_calls == 0
        assert hardware.eject_calls == 0
    finally:
        await engine.stop()
        store.close()


async def test_auto_tombstone_requires_stable_absence_before_requeue(tmp_path):
    config, settings, store = configured(tmp_path, headless=False)
    settings.value["auto_test"] = True
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)

    await engine.scan()
    first = await engine.queue.get()
    engine.queue.task_done()
    run = store.get(first)
    run.update(status="passed", workflow_status="complete")
    store.save(run)

    hardware.present = False
    await engine.scan()
    hardware.present = True
    await engine.scan()
    assert engine.queue.empty()

    hardware.present = False
    for _ in range(AUTO_DETACH_SCANS):
        await engine.scan()
    hardware.present = True
    await engine.scan()
    assert engine.queue.qsize() == 1
    store.close()


async def test_manual_release_rejects_busy_station_and_ejects_when_idle(tmp_path):
    config, settings, store = configured(tmp_path, headless=False)
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.scan()
    drive_id = engine.drives[0].id
    queued = await engine.enqueue(drive_id, "quick")
    with pytest.raises(ValueError, match="Wait for all testing"):
        await engine.release(drive_id, "eject")
    run = store.get(queued["id"])
    run.update(status="cancelled", workflow_status="complete")
    store.save(run)
    engine.queue.get_nowait()
    engine.queue.task_done()
    result = await engine.release(drive_id, "eject")
    assert result["status"] == "ejected"
    assert hardware.eject_calls == 1
    assert engine.state()["system"]["release_in_progress"] is False
    store.close()


def test_recovery_marks_finishing_release_interrupted_without_eject_retry(tmp_path):
    store = Store(tmp_path / "runs.db")
    store.save(
        {
            "id": "finishing",
            "created_at": "2026-10-03",
            "status": "passed",
            "workflow_status": "finishing",
            "lifecycle": {
                "notification_status": "pending",
                "eject_status": "pending",
                "eject_detail": "",
            },
        }
    )
    store.enqueue_notice("finishing:finished", "Safe persisted completion message")
    store.recover()
    run = store.get("finishing")
    assert run["status"] == "passed"
    assert run["workflow_status"] == "interrupted"
    assert run["lifecycle"]["notification_status"] == "pending"
    assert run["lifecycle"]["eject_status"] == "failed"
    assert store.notice_state("finishing:finished")["delivered"] is False
    store.close()
