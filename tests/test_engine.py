import asyncio

import pytest

from drivecheck.config import Config, Settings
from drivecheck.engine import Engine, verdict
from drivecheck.hardware import Hardware, SafetyError
from drivecheck.storage import Store


class SlowHardware(Hardware):
    def __init__(self):
        super().__init__(demo=True)
        self.entered = asyncio.Event()
        self.cancelled = False
        self.unsafe = False

    async def self_test(self, drive, progress):
        self.entered.set()
        await asyncio.sleep(100)
        return {"status": "passed"}

    async def validate(self, drive, destructive=False):
        if self.unsafe:
            raise SafetyError("Drive became mounted")
        return await super().validate(drive, destructive)

    async def cancel(self):
        self.cancelled = True


@pytest.mark.parametrize(
    ("parts", "status"),
    [
        ({"smart": {"health": "passed"}, "scan": {"status": "passed"}}, "passed"),
        ({"smart": {"health": "unsupported"}, "scan": {"status": "passed"}}, "incomplete"),
        ({"scan": {"status": "failed"}}, "failed"),
        ({"smart": {"health": "warning"}}, "warning"),
        ({}, "incomplete"),
    ],
)
def test_verdict_never_claims_pass_without_complete_checks(parts, status):
    assert verdict(parts)[0] == status


async def setup(tmp_path, hardware):
    config = Config(tmp_path, api_key="test-token-long-enough")
    config.prepare()
    store = Store(tmp_path / "runs.db")
    instance = Engine(config, Settings(config), store, hardware)
    await instance.start()
    return instance, store


async def test_cancel_stops_active_job_and_queue_keeps_working(tmp_path):
    hardware = SlowHardware()
    instance, store = await setup(tmp_path, hardware)
    try:
        run = await instance.enqueue(instance.drives[0].id, "extended")
        await asyncio.wait_for(hardware.entered.wait(), 1)
        with pytest.raises(ValueError, match="already"):
            await instance.enqueue(instance.drives[0].id, "quick")
        await instance.cancel(run["id"])
        assert hardware.cancelled
        assert store.get(run["id"])["status"] == "cancelled"
        await asyncio.sleep(0)
        retry = await instance.enqueue(instance.drives[0].id, "quick")
        await asyncio.wait_for(instance.queue.join(), 1)
        assert store.get(retry["id"])["status"] == "passed"
    finally:
        await instance.stop()
        store.close()


async def test_queued_job_revalidates_and_stops_on_mount(tmp_path):
    hardware = SlowHardware()
    instance, store = await setup(tmp_path, hardware)
    try:
        run = await instance.enqueue(instance.drives[0].id, "quick")
        hardware.unsafe = True
        await asyncio.wait_for(instance.queue.join(), 1)
        assert store.get(run["id"])["status"] == "incomplete"
        assert store.get(run["id"])["results"] == {}
    finally:
        await instance.stop()
        store.close()


def test_restart_marks_running_and_queued_incomplete(tmp_path):
    store = Store(tmp_path / "runs.db")
    for status in ("running", "queued", "passed"):
        store.save({"id": status, "created_at": "2026-10-03", "status": status})
    store.recover()
    assert store.get("running")["status"] == "incomplete"
    assert store.get("queued")["status"] == "incomplete"
    assert store.get("passed")["status"] == "passed"
    store.close()


async def test_unexpected_tool_response_does_not_kill_worker(tmp_path):
    class BadOnce(Hardware):
        bad = True

        async def smart(self, drive):
            if self.bad:
                self.bad = False
                raise AttributeError("Malformed tool JSON")
            return await super().smart(drive)

    instance, store = await setup(tmp_path, BadOnce(demo=True))
    try:
        run = await instance.enqueue(instance.drives[0].id, "quick")
        await asyncio.wait_for(instance.queue.join(), 1)
        assert store.get(run["id"])["status"] == "incomplete"
        retry = await instance.enqueue(instance.drives[0].id, "quick")
        await asyncio.wait_for(instance.queue.join(), 1)
        assert store.get(retry["id"])["status"] == "passed"
        assert instance.station_error is None
    finally:
        await instance.stop()
        store.close()
