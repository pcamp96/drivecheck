import asyncio
from dataclasses import replace

import pytest

from drivecheck.config import Config, Settings
from drivecheck.engine import Engine
from drivecheck.hardware import Hardware, SafetyError
from drivecheck.storage import Store


class StaleRaidHardware(Hardware):
    def __init__(self):
        super().__init__(demo=True)
        self.claimed = True
        self.takeovers = 0
        self.stop_error = False

    def capabilities(self):
        return {**super().capabilities(), "can_take_control": True}

    async def discover(self):
        drives = await super().discover()
        if self.claimed:
            return [replace(d, eligible=False, reasons=["device_in_use"]) for d in drives]
        return drives

    async def take_control(self, drive, confirmation):
        self.takeovers += 1
        if self.stop_error:
            raise SafetyError("Array became active")
        self.claimed = False
        return {"status": "released", "detail": "Inactive RAID claim released; metadata preserved."}


async def setup(tmp_path):
    config = Config(tmp_path, api_key="test-token-long-enough")
    config.prepare()
    settings = Settings(config)
    store = Store(tmp_path / "runs.db")
    hardware = StaleRaidHardware()
    engine = Engine(config, settings, store, hardware)
    await engine.start()
    return engine, store, hardware


async def test_explicit_takeover_queues_one_quick_intake_and_respects_busy(tmp_path):
    engine, store, hardware = await setup(tmp_path)
    try:
        drive = engine.drives[0]
        with pytest.raises(ValueError, match="exact serial"):
            await engine.take_control(drive.id, "TAKE CONTROL WRONG")
        assert hardware.takeovers == 0
        outcome = await engine.take_control(drive.id, f"TAKE CONTROL {drive.serial}")
        assert outcome["run"]["profile"] == "quick"
        assert outcome["run"]["automatic"] is True
        assert outcome["run"]["intake_source"] == "take_control"
        assert engine.state()["system"]["release_in_progress"] is False
        with pytest.raises(ValueError, match="finish first"):
            await engine.take_control(drive.id, f"TAKE CONTROL {drive.serial}")
        assert hardware.takeovers == 1
        await asyncio.wait_for(engine.queue.join(), 1)
        assert set(store.runs()[0]["results"]) == {"smart_before", "benchmark", "smart_after"}
        assert not any(r["profile"] == "verify" for r in store.runs())
    finally:
        await engine.stop()
        store.close()


async def test_takeover_failure_does_not_queue_or_drop_manual_confirmation(tmp_path):
    engine, store, hardware = await setup(tmp_path)
    try:
        hardware.stop_error = True
        drive = engine.drives[0]
        with pytest.raises(SafetyError, match="active"):
            await engine.take_control(drive.id, f"TAKE CONTROL {drive.serial}")
        assert engine.queue.empty() and not store.runs()
        assert hardware.claimed
        assert not engine.release_in_progress
    finally:
        await engine.stop()
        store.close()


async def test_automatic_intake_never_takes_over_kernel_claim(tmp_path):
    engine, store, hardware = await setup(tmp_path)
    try:
        engine.settings.value["auto_test"] = True
        await engine.scan()
        assert hardware.takeovers == 0
        assert not store.runs() and engine.queue.empty()
    finally:
        await engine.stop()
        store.close()
