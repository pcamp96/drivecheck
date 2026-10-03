from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from drivecheck import timing
from drivecheck.config import Config, Settings
from drivecheck.engine import Engine
from drivecheck.hardware import Hardware
from drivecheck.storage import Store

CLOCK = datetime(2026, 10, 3, 21, 0, tzinfo=UTC)
DRIVE = SimpleNamespace(size_bytes=4_000_000_000_000)
INFO = {"self_test_seconds": {"short": 60, "long": 447 * 60}, "notes": []}


def test_preflight_includes_firmware_and_surface_and_marks_assumptions():
    measured = timing.build_estimate(DRIVE, "extended", INFO, 200, clock=CLOCK)
    assert measured["total_seconds"] == 447 * 60 + 25000 + 40
    assert measured["complete"]
    assert "measured 200.0 MB/s" in " ".join(measured["notes"])
    provisional = timing.build_estimate(DRIVE, "extended", INFO, clock=CLOCK)
    assert "assumes 100 MB/s" in " ".join(provisional["notes"])
    missing = timing.build_estimate(DRIVE, "extended", {"self_test_seconds": {}}, clock=CLOCK)
    assert missing["total_seconds"] is None and missing["estimated_finish_at"] is None
    assert missing["minimum_seconds"] > 0 and not missing["complete"]


def test_countdown_does_not_fabricate_firmware_progress_and_overdue_has_no_eta():
    run = {
        "status": "running",
        "phase": "self_test",
        "estimate": timing.build_estimate(DRIVE, "extended", INFO, 200, clock=CLOCK),
        "task": {"phase": "self_test", "started_at": CLOCK.isoformat(), "progress_percent": 10},
    }
    before = timing.live_timing(run, clock=CLOCK + timedelta(minutes=5))
    after = timing.live_timing(run, clock=CLOCK + timedelta(minutes=6))
    assert before["remaining_seconds"] - after["remaining_seconds"] == 60
    assert before["estimated_finish_at"] == after["estimated_finish_at"]
    assert run["task"]["progress_percent"] == 10
    overdue = timing.live_timing(run, clock=CLOCK + timedelta(hours=8))
    assert overdue["overdue"] and overdue["remaining_seconds"] is None
    assert overdue["estimated_finish_at"] is None


def test_surface_eta_uses_observed_rate_and_terminal_jobs_have_no_countdown():
    run = {
        "status": "running",
        "phase": "surface",
        "estimate": timing.build_estimate(DRIVE, "extended", INFO, 200, clock=CLOCK),
        "task": {"phase": "surface", "started_at": CLOCK.isoformat(), "progress_percent": 25},
    }
    value = timing.live_timing(run, clock=CLOCK + timedelta(hours=1))
    assert value["remaining_seconds"] == 3 * 3600 + 5
    assert any("observed rate" in note for note in value["notes"])
    run["status"] = "cancelled"
    assert timing.live_timing(run, clock=CLOCK)["remaining_seconds"] is None


async def test_estimate_probe_and_quick_short_test_are_read_only_and_reported(tmp_path):
    class ShortHardware(Hardware):
        def __init__(self):
            super().__init__(demo=True)
            self.short_calls = 0
            self.long_calls = 0
            self.surface_calls = 0

        async def short_self_test(self, drive, progress):
            self.short_calls += 1
            await progress(10, "Short SMART self-test running; firmware reports 90% remaining")
            return {"status": "unsupported", "detail": "Bridge cannot report short self-test"}

        async def self_test(self, drive, progress):
            self.long_calls += 1
            raise AssertionError("Quick cannot run extended self-test")

        async def surface(self, drive, progress, destructive=False):
            self.surface_calls += 1
            raise AssertionError("Quick cannot run a full scan or write")

    config = Config(tmp_path, api_key="test-token-long-enough")
    config.prepare()
    hardware = ShortHardware()
    store = Store(tmp_path / "runs.db")
    engine = Engine(config, Settings(config), store, hardware)
    try:
        engine.drives = await hardware.discover()
        estimate = await engine.test_estimate(engine.drives[0].id, "extended")
        assert estimate["total_seconds"] > 0 and not store.runs()
        assert hardware.short_calls == hardware.long_calls == hardware.surface_calls == 0
        run = await engine.enqueue(engine.drives[0].id, "quick", automatic=True)
        await engine.execute(run)
        result = store.get(run["id"])
        assert result["status"] == "incomplete"
        assert set(result["results"]) == {"smart_before", "self_test", "benchmark", "smart_after"}
        assert result["steps"] == timing.steps_for("quick")
        assert hardware.short_calls == 1 and hardware.long_calls == hardware.surface_calls == 0
        assert result["task"]["phase"] == "smart_after"
        assert result["timing"]["remaining_seconds"] is None
    finally:
        store.close()


@pytest.mark.parametrize("percent", [None, 0, 10])
def test_unknown_task_duration_never_claims_finished(percent):
    run = {
        "status": "running",
        "phase": "self_test",
        "estimate": timing.build_estimate(DRIVE, "quick", {"self_test_seconds": {}}, clock=CLOCK),
        "task": {
            "phase": "self_test",
            "started_at": CLOCK.isoformat(),
            "progress_percent": percent,
        },
    }
    value = timing.live_timing(run, clock=CLOCK + timedelta(hours=2))
    assert value["remaining_seconds"] is None and value["estimated_finish_at"] is None
    assert not value["overdue"]


def test_surface_countdown_is_pinned_to_actual_sample_and_preserves_fractional_progress():
    run = {
        "status": "running",
        "phase": "surface",
        "estimate": timing.build_estimate(DRIVE, "extended", INFO, 100, clock=CLOCK),
        "task": {
            "phase": "surface",
            "started_at": CLOCK.isoformat(),
            "last_update_at": (CLOCK + timedelta(minutes=1)).isoformat(),
            "progress_percent": 0.15,
        },
    }
    first = timing.live_timing(run, clock=CLOCK + timedelta(minutes=1))
    next_value = timing.live_timing(run, clock=CLOCK + timedelta(minutes=1, seconds=5))
    assert first["estimated_finish_at"] == next_value["estimated_finish_at"]
    assert first["remaining_seconds"] - next_value["remaining_seconds"] == 5
    assert run["task"]["progress_percent"] == 0.15
    run.update(status="passed", finished_at=(CLOCK + timedelta(hours=1)).isoformat())
    finished = timing.live_timing(run, clock=CLOCK + timedelta(hours=2))
    later = timing.live_timing(run, clock=CLOCK + timedelta(hours=3))
    assert finished["phase_elapsed_seconds"] == later["phase_elapsed_seconds"] == 3600
    assert finished["phase_estimated_finish_at"] is None
