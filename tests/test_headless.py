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
        assert any("Safe eject pending" in message for message in delivered)
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


async def test_failed_self_test_still_ejects_and_sends_failed_ready_notice(tmp_path, monkeypatch):
    delivered = []

    async def send(settings, message, transport=None):
        delivered.append(message)

    class FailedHardware(HeadlessHardware):
        async def self_test(self, drive, progress):
            return {"status": "failed", "detail": "Completed: read failure"}

    monkeypatch.setattr(notifications, "send", send)
    config, settings, store = configured(tmp_path)
    hardware = FailedHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    try:
        await asyncio.wait_for(engine.queue.join(), 2)
        run = store.runs()[0]
        assert run["status"] == "failed"
        assert "benchmark" not in run["results"]
        assert run["lifecycle"]["eject_status"] == "ejected"
        assert hardware.eject_calls == 1
        for _ in range(30):
            if store.notice_state(f"{run['id']}:ready")["delivered"]:
                break
            await asyncio.sleep(0.01)
        assert store.notice_state(f"{run['id']}:ready")["delivered"]
        assert any(
            "DriveCheck: failed" in message and "ready to remove" in message
            for message in delivered
        )
    finally:
        await engine.stop()
        store.close()


async def finished_run(engine):
    await engine.scan()
    run = await engine.enqueue(engine.drives[0].id, "extended")
    run.update(status="failed", workflow_status="complete", profile="verify")
    engine.store.save(run)
    engine.queue.get_nowait()
    engine.queue.task_done()
    return run


async def test_retest_requires_reconnection_and_never_repeats_destructive_profile(tmp_path):
    config, settings, store = configured(tmp_path, headless=False)
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)
    original = await finished_run(engine)
    hardware.present = False
    absent = await engine.retest(original["id"])
    assert absent["status"] == "reconnect_required"
    assert engine.queue.empty()
    hardware.present = True
    retry = await engine.retest(original["id"])
    assert retry["run"]["profile"] == "extended"
    assert retry["run"]["id"] != original["id"]
    assert len(store.runs()) == 2
    # Repeated clicks return the same queued run, not an additional job.
    assert (await engine.retest(original["id"]))["run"]["id"] == retry["run"]["id"]
    assert engine.queue.qsize() == 1
    store.close()


async def test_retest_refuses_mounted_drive_and_deduplicates_automatic_reconnect(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    config, settings, store = configured(tmp_path, headless=False)
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)
    original = await finished_run(engine)
    original_drive = engine.drives[0]

    async def mounted():
        return [replace(original_drive, mounted=True, eligible=False, reasons=["mounted"])]

    monkeypatch.setattr(hardware, "discover", mounted)
    with pytest.raises(ValueError, match="safe for an unmounted"):
        await engine.retest(original["id"])
    assert engine.queue.empty()
    monkeypatch.undo()
    settings.value["auto_test"] = True
    hardware.present = False
    for _ in range(AUTO_DETACH_SCANS):
        await engine.scan()
    hardware.present = True
    response = await engine.retest(original["id"])
    assert response["status"] == "queued"
    assert len(store.runs()) == 2
    assert engine.queue.qsize() == 1
    store.close()


async def test_late_manual_eject_updates_failed_report_and_queues_release_notice(tmp_path):
    config, settings, store = configured(tmp_path, headless=False)
    engine = Engine(config, settings, store, HeadlessHardware())
    original = await finished_run(engine)
    result = await engine.release(original["drive_id"], "eject")
    assert result["status"] == "ejected"
    saved = store.get(original["id"])
    assert saved["status"] == "failed"
    assert saved["lifecycle"]["eject_status"] == "ejected"
    assert store.notice_state(f"{original['id']}:ready") is not None
    store.close()


async def test_retest_resolves_identity_after_device_path_changes(tmp_path, monkeypatch):
    from dataclasses import replace

    config, settings, store = configured(tmp_path, headless=False)
    hardware = HeadlessHardware()
    engine = Engine(config, settings, store, hardware)
    original = await finished_run(engine)
    original_drive = engine.drives[0]

    async def reconnected():
        return [replace(original_drive, path="/dev/demo-reconnected")]

    monkeypatch.setattr(hardware, "discover", reconnected)
    response = await engine.retest(original["id"])
    assert response["run"]["drive"]["path"] == "/dev/demo-reconnected"
    assert response["run"]["drive"]["identity"] == original["drive"]["identity"]
    assert original["drive"]["path"] != "/dev/demo-reconnected"
    store.close()


async def test_failed_late_eject_never_queues_ready_to_remove(tmp_path):
    class FailedEjectHardware(HeadlessHardware):
        async def eject(self, drive):
            return {"status": "failed", "detail": "Power-off was not confirmed."}

    config, settings, store = configured(tmp_path, headless=False)
    engine = Engine(config, settings, store, FailedEjectHardware())
    original = await finished_run(engine)
    response = await engine.release(original["drive_id"], "eject")
    assert response["status"] == "failed"
    assert store.get(original["id"])["lifecycle"]["eject_status"] == "failed"
    assert store.notice_state(f"{original['id']}:ready") is None
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


async def test_notifier_external_cancellation_wins_notice_event_race(tmp_path):
    config, settings, store = configured(tmp_path, headless=False)
    engine = Engine(config, settings, store, HeadlessHardware())
    task = asyncio.create_task(engine.notifier())
    await asyncio.sleep(0)
    engine.notice_event.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(0.5):
            await task
    assert task.cancelled()
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
