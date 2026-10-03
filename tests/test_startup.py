import asyncio

from drivecheck import notifications
from drivecheck.config import Config, Settings
from drivecheck.engine import Engine
from drivecheck.hardware import Hardware
from drivecheck.storage import Store


class StartupHardware(Hardware):
    def __init__(self, *, fail=False):
        super().__init__(demo=True)
        self.fail = fail

    async def discover(self):
        if self.fail:
            raise RuntimeError("synthetic discovery failure")
        return await super().discover()

    def capabilities(self):
        return {
            "platform": "test-platform",
            "can_test": True,
            "can_verify": False,
            "can_unmount": True,
            "can_eject": True,
            "tools": {},
            "limitations": [],
        }


def station(tmp_path, *, notify_ready=True, fail=False, auto_test=False):
    config = Config(tmp_path, api_key="test-token-long-enough")
    config.prepare()
    settings = Settings(config)
    settings.value["auto_test"] = auto_test
    settings.value["notifications"].update(
        provider="discord",
        enabled=True,
        discord_webhook="https://discord.com/api/webhooks/123/test_token",
        notify_ready=notify_ready,
    )
    store = Store(tmp_path / "runs.db")
    engine = Engine(config, settings, store, StartupHardware(fail=fail))
    return engine, store


async def wait_until(predicate, timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_successful_startup_sends_one_ready_notice_with_queue_state(tmp_path, monkeypatch):
    delivered = []

    async def send(settings, message, transport=None):
        delivered.append(message)

    monkeypatch.setattr(notifications, "send", send)
    engine, store = station(tmp_path, auto_test=True)
    await engine.start()
    try:
        await wait_until(lambda: any("station ready" in item for item in delivered))
        ready = [item for item in delivered if "station ready" in item]
        assert len(ready) == 1
        assert "Platform: test-platform" in ready[0]
        assert "Software: ready." in ready[0]
        assert "Testing: simulation ready" in ready[0]
        assert "Queue: 1 intake job queued." in ready[0]
        assert engine.booted_at in ready[0]
        engine._queue_startup_ready_notice()
        await asyncio.sleep(0)
        assert len([item for item in delivered if "station ready" in item]) == 1
    finally:
        await engine.stop()
        store.close()


async def test_ready_toggle_disables_startup_notice(tmp_path, monkeypatch):
    delivered = []

    async def send(settings, message, transport=None):
        delivered.append(message)

    monkeypatch.setattr(notifications, "send", send)
    engine, store = station(tmp_path, notify_ready=False)
    await engine.start()
    try:
        await asyncio.sleep(0.05)
        assert delivered == []
        assert store.notice_state(engine.startup_notice_id) is None
    finally:
        await engine.stop()
        store.close()


async def test_discovery_failure_never_queues_ready_notice(tmp_path, monkeypatch):
    delivered = []

    async def send(settings, message, transport=None):
        delivered.append(message)

    monkeypatch.setattr(notifications, "send", send)
    engine, store = station(tmp_path, fail=True)
    await engine.start()
    try:
        await asyncio.sleep(0.05)
        assert engine.discovery_error is not None
        assert delivered == []
        assert store.notice_state(engine.startup_notice_id) is None
    finally:
        await engine.stop()
        store.close()


async def test_startup_outage_keeps_notice_for_retry(tmp_path, monkeypatch):
    async def fail(settings, message, transport=None):
        raise notifications.NotificationError("offline")

    monkeypatch.setattr(notifications, "send", fail)
    engine, store = station(tmp_path)
    await engine.start()
    try:
        await wait_until(
            lambda: bool(
                (state := store.notice_state(engine.startup_notice_id)) and state["attempts"] >= 1
            )
        )
        state = store.notice_state(engine.startup_notice_id)
        assert state["delivered"] is False
        assert state["attempts"] == 1
    finally:
        await engine.stop()
        store.close()


async def test_restart_discards_stale_pending_startup_notice(tmp_path, monkeypatch):
    delivered = []

    async def send(settings, message, transport=None):
        delivered.append(message)

    monkeypatch.setattr(notifications, "send", send)
    engine, store = station(tmp_path)
    stale_id = "startup:ready:previous-process"
    store.enqueue_notice(stale_id, "Old process is ready")
    await engine.start()
    try:
        await wait_until(lambda: bool(delivered))
        assert store.notice_state(stale_id) is None
        assert "Old process is ready" not in delivered
        assert store.notice_state(engine.startup_notice_id)["delivered"] is True
    finally:
        await engine.stop()
        store.close()
