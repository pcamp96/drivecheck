"""Erase orchestration acceptance checks with synthetic devices only."""

import asyncio
import time

import pytest

from drivecheck.config import Config, Settings
from drivecheck.engine import Engine
from drivecheck.hardware import Hardware, SafetyError
from drivecheck.reports import human_report
from drivecheck.storage import Store


class EraseHardware(Hardware):
    def __init__(self):
        super().__init__(demo=True)
        self.method = "quick_format_exfat"
        self.calls = []
        self.entered = asyncio.Event()
        self.firmware_erase_active = False
        self.hold = False
        self.validations = 0
        self.on_validate = None

    async def validate(self, drive, destructive=False):
        self.validations += 1
        if self.on_validate:
            self.on_validate(self.validations)
        return await super().validate(drive, destructive)

    async def erase_plan(self, drive):
        return {
            "quick": {
                "available": True,
                "method": self.method,
                "detail": "Quick format",
                "targets": [
                    {
                        "id": "volume-1",
                        "path": "/dev/demo1",
                        "size_bytes": drive.size_bytes,
                    }
                ],
            },
            "initialize": {
                "available": True,
                "method": "initialize_exfat",
                "detail": "Initialize disk",
            },
            "secure": {
                "available": True,
                "method": "ata_secure_erase",
                "detail": "Firmware erase",
                "estimated_minutes": 240,
            },
            "full": {"available": True, "method": "full_overwrite", "detail": "Complete overwrite"},
        }

    async def erase(
        self, drive, profile, progress, *, recovery_dir, expected_method, target_id=None
    ):
        self.calls.append((profile, expected_method))
        self.entered.set()
        if self.hold:
            if expected_method == "ata_secure_erase":
                recovery_dir.mkdir()
                (recovery_dir / f"{drive.identity}.json").write_text("{}")
                self.firmware_erase_active = True
            await asyncio.sleep(100)
        return {"status": "passed", "method": expected_method, "detail": "Erase completed."}

    async def eject(self, drive):
        self.calls.append("eject")
        return {"status": "ejected", "detail": "Released"}


async def make_engine(tmp_path, *, allow=True):
    config = Config(tmp_path, allow_destructive=allow, api_key="test-token-long-enough")
    config.prepare()
    store = Store(tmp_path / "runs.db")
    engine = Engine(config, Settings(config), store, EraseHardware())
    engine.drives = await engine.hardware.discover()
    return engine, store


def authorize(engine):
    engine.settings.value["notifications"].update(
        enabled=True,
        provider="telegram",
        telegram_token="synthetic-secret",
        telegram_chat_id="123",
        telegram_user_id="123",
    )


@pytest.mark.parametrize(
    "profile,method",
    [
        ("quick_erase", "quick_format_exfat"),
        ("initialize_disk", "initialize_exfat"),
        ("secure_erase", "ata_secure_erase"),
        ("full_erase", "full_overwrite"),
    ],
)
async def test_erase_is_separate_manual_job_and_report_records_method(tmp_path, profile, method):
    engine, store = await make_engine(tmp_path)
    try:
        drive = engine.drives[0]
        target = (
            {"id": "volume-1", "path": "/dev/demo1", "size_bytes": drive.size_bytes}
            if profile == "quick_erase"
            else None
        )
        target_id = target["id"] if target else None
        phrase = engine._erase_phrase(profile, drive.serial, target)
        with pytest.raises(ValueError, match="confirmation endpoint"):
            await engine.enqueue(drive.id, profile)
        with pytest.raises(ValueError, match="exact|confirmation"):
            await engine.request_erase(drive.id, profile, "WRONG", method, target_id=target_id)
        with pytest.raises(ValueError, match="method changed"):
            await engine.request_erase(
                drive.id, profile, phrase, "wrong_method", target_id=target_id
            )
        assert not store.runs() and not engine.hardware.calls
        response = await engine.request_erase(
            drive.id, profile, phrase, method, target_id=target_id
        )
        run = response["run"]
        assert not run["automatic"]
        await engine.execute(run)
        assert engine.hardware.calls == [(profile, method)]
        assert set(store.get(run["id"])["results"]) == {"erase"}
        report = human_report(store.get(run["id"]))
        assert "VERDICT: PASSED" in report and "Drive erasure" in report
    finally:
        store.close()


async def test_disabled_station_and_automatic_intake_cannot_write(tmp_path):
    engine, store = await make_engine(tmp_path, allow=False)
    try:
        drive = engine.drives[0]
        with pytest.raises(ValueError, match="disabled"):
            await engine.request_erase(
                drive.id, "secure_erase", f"SECURE ERASE {drive.serial}", "ata_secure_erase"
            )
        with pytest.raises(ValueError, match="read-only"):
            await engine.enqueue(drive.id, "full_erase", automatic=True)
        engine.settings.value["auto_test"] = True
        await engine.scan()
        assert [r["profile"] for r in store.runs()] == ["quick"]
        assert not engine.hardware.calls
    finally:
        store.close()


async def test_firmware_cancellation_survives_restart_and_blocks_release(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        drive = engine.drives[0]
        engine.settings.value["auto_eject"] = True
        engine.hardware.hold = True
        response = await engine.request_erase(
            drive.id, "secure_erase", f"SECURE ERASE {drive.serial}", "ata_secure_erase"
        )
        task = asyncio.create_task(engine.execute(response["run"]))
        await engine.hardware.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        result = store.get(response["run"]["id"])
        assert result["status"] == "incomplete"
        assert result["results"]["erase"]["recovery_required"]
        assert result["lifecycle"]["eject_status"] == "not_requested"
        assert "eject" not in engine.hardware.calls
        # A new hardware instance cannot bypass the durable journal.
        engine.hardware = EraseHardware()
        engine.capabilities["can_eject"] = True
        await engine.scan()
        assert not engine.drives[0].eligible
        with pytest.raises(SafetyError, match="recovery"):
            await engine.release(drive.id, "eject")
        with pytest.raises(SafetyError, match="recovery"):
            await engine.enqueue(drive.id, "quick")
        with pytest.raises(ValueError, match="recovery"):
            await engine.erase_plan(drive.id)
    finally:
        store.close()


async def test_telegram_button_intent_requires_serial_and_is_one_use(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        authorize(engine)
        drive = engine.drives[0]
        original = await engine.enqueue(drive.id, "quick")
        original.update(status="passed", workflow_status="awaiting_action")
        store.save(original)
        wait = {"deadline": time.monotonic() + 180, "event": asyncio.Event()}
        engine.action_waits[original["id"]] = wait
        intent = await engine.begin_erase(original["id"], "quick_erase", chat_id=123, user_id=123)
        assert not engine.hardware.calls and not wait["event"].is_set()
        with pytest.raises(ValueError, match="exactly"):
            await engine.confirm_erase(intent["intent_id"], "WRONG", chat_id=123, user_id=123)
        outcome = await engine.confirm_erase(
            intent["intent_id"],
            f"QUICK FORMAT {drive.serial} /dev/demo1",
            chat_id=123,
            user_id=123,
        )
        assert outcome["status"] == "accepted"
        assert wait["event"].is_set() and wait["choice"] == "quick_erase"
        with pytest.raises(ValueError, match="expired"):
            await engine.confirm_erase(
                intent["intent_id"],
                f"QUICK FORMAT {drive.serial} /dev/demo1",
                chat_id=123,
                user_id=123,
            )
        assert not engine.hardware.calls
    finally:
        store.close()


async def test_firmware_erase_confirmation_names_duration_and_cancellation_limit(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        authorize(engine)
        drive = engine.drives[0]
        original = await engine.enqueue(drive.id, "quick")
        original.update(status="passed", workflow_status="complete")
        store.save(original)
        intent = await engine.begin_erase(original["id"], "secure_erase", chat_id=123, user_id=123)
        assert "Estimated duration: about 4h 0m." in intent["message"]
        assert "Cannot safely cancel once firmware erase starts." in intent["message"]
        assert f"SECURE ERASE {drive.serial}" in intent["message"]
    finally:
        store.close()


async def test_telegram_quick_format_requires_one_volume_and_rejects_target_drift(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        authorize(engine)
        drive = engine.drives[0]
        original = await engine.enqueue(drive.id, "quick")
        original.update(status="passed", workflow_status="complete")
        store.save(original)

        async def plan_with(targets):
            return {
                "quick": {
                    "available": True,
                    "method": "quick_format_exfat",
                    "detail": "Selected-volume quick format",
                    "targets": targets,
                }
            }

        first = {"id": "volume-1", "path": "/dev/demo1", "size_bytes": 750_000_000}
        second = {"id": "volume-2", "path": "/dev/demo2", "size_bytes": 250_000_000}
        engine.hardware.erase_plan = lambda _drive: plan_with([first, second])
        with pytest.raises(ValueError, match="dashboard"):
            await engine.begin_erase(original["id"], "quick_erase", chat_id=123, user_id=123)

        engine.hardware.erase_plan = lambda _drive: plan_with([first])
        intent = await engine.begin_erase(original["id"], "quick_erase", chat_id=123, user_id=123)
        engine.hardware.erase_plan = lambda _drive: plan_with([second])
        with pytest.raises(ValueError, match="current quick-format volume"):
            await engine.confirm_erase(
                intent["intent_id"],
                f"QUICK FORMAT {drive.serial} /dev/demo1",
                chat_id=123,
                user_id=123,
            )
        assert [run for run in store.runs() if run["profile"] == "quick_erase"] == []
    finally:
        store.close()


async def test_telegram_authority_rechecked_after_final_hardware_await(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        authorize(engine)
        drive = engine.drives[0]
        original = await engine.enqueue(drive.id, "quick")
        original.update(status="passed", workflow_status="complete")
        store.save(original)
        intent = await engine.begin_erase(original["id"], "quick_erase", chat_id=123, user_id=123)
        engine.hardware.validations = 0

        def revoke_on_last_validate(count):
            if count == 2:
                engine.settings.value["notifications"]["telegram_token"] = "replacement-secret"

        engine.hardware.on_validate = revoke_on_last_validate
        with pytest.raises(ValueError, match="authorization changed"):
            await engine.confirm_erase(
                intent["intent_id"],
                f"QUICK FORMAT {drive.serial} /dev/demo1",
                chat_id=123,
                user_id=123,
            )
        assert len(store.runs()) == 1 and not engine.hardware.calls
    finally:
        store.close()


@pytest.mark.parametrize("revoke", [False, True])
async def test_quick_action_window_reserves_erase_and_rechecks_telegram_authority(tmp_path, revoke):
    engine, store = await make_engine(tmp_path)
    try:
        authorize(engine)
        engine.config.notification_wait_seconds = 0
        engine.settings.value["auto_eject"] = True
        drive = engine.drives[0]
        run = await engine.enqueue(drive.id, "quick", automatic=True)
        task = asyncio.create_task(engine.execute(run))
        for _ in range(100):
            if run["id"] in engine.action_waits:
                break
            await asyncio.sleep(0.001)
        assert run["id"] in engine.action_waits
        intent = await engine.begin_erase(run["id"], "full_erase", chat_id=123, user_id=123)
        await engine.confirm_erase(
            intent["intent_id"], f"FULL ERASE {drive.serial}", chat_id=123, user_id=123
        )
        engine.hardware.validations = 0
        if revoke:

            def revoke_at_final_enqueue(count):
                if count == 2:
                    engine.settings.value["notifications"]["telegram_user_id"] = "456"

            engine.hardware.on_validate = revoke_at_final_enqueue
        await asyncio.wait_for(task, 1)
        erased = [r for r in store.runs() if r["profile"] == "full_erase"]
        if revoke:
            assert not erased and engine.hardware.calls == ["eject"]
        else:
            assert len(erased) == 1 and not erased[0]["automatic"]
            assert erased[0]["erase_method"] == "full_overwrite"
            assert not engine.hardware.calls
    finally:
        store.close()


async def test_service_shutdown_does_not_hang_or_eject_armed_firmware(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        engine.hardware.hold = True
        engine.settings.value["auto_eject"] = True
        await engine.start()
        drive = engine.drives[0]
        response = await engine.request_erase(
            drive.id, "secure_erase", f"SECURE ERASE {drive.serial}", "ata_secure_erase"
        )
        await asyncio.wait_for(engine.hardware.entered.wait(), 1)
        await asyncio.wait_for(engine.stop(), 1)
        assert store.get(response["run"]["id"])["status"] == "incomplete"
        assert "eject" not in engine.hardware.calls
        assert engine._recovery_pending(drive)
    finally:
        store.close()


async def test_queued_firmware_erase_captures_duration_and_null_progress(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        hardware = engine.hardware
        plans = 0

        async def plan(drive):
            nonlocal plans
            plans += 1
            return {
                "secure": {
                    "available": True,
                    "method": "ata_secure_erase",
                    "estimated_minutes": 120 if plans < 3 else 125,
                }
            }

        async def erase(drive, profile, progress, *, recovery_dir, expected_method, target_id=None):
            await progress(None, "Firmware erase running; progress unavailable")
            current = store.runs()[0]
            assert current["progress"] is None and current["task"]["progress_percent"] is None
            assert current["estimate"]["total_seconds"] == 125 * 60
            assert current["timing"]["remaining_seconds"] > 0
            return {
                "status": "passed",
                "method": expected_method,
                "detail": "Firmware erase completed",
            }

        hardware.erase_plan = plan
        hardware.erase = erase
        drive = engine.drives[0]
        response = await engine.request_erase(
            drive.id, "secure_erase", "SECURE ERASE " + drive.serial, "ata_secure_erase"
        )
        run = response["run"]
        assert run["estimate"]["total_seconds"] == 120 * 60
        assert not hardware.calls
        await engine.execute(run)
        finished = store.get(run["id"])
        assert finished["status"] == "passed"
        assert finished["progress"] == 100
        assert finished["timing"]["remaining_seconds"] is None
    finally:
        store.close()


async def test_uncertain_firmware_erase_never_reports_completed_percentage(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:

        async def erase(drive, profile, progress, **kwargs):
            await progress(None, "Firmware erase running; progress unavailable")
            return {
                "status": "incomplete",
                "method": "ata_secure_erase",
                "recovery_required": True,
                "detail": "Firmware erase state uncertain",
            }

        engine.hardware.erase = erase
        engine.settings.value["auto_eject"] = True
        drive = engine.drives[0]
        result = await engine.request_erase(
            drive.id, "secure_erase", "SECURE ERASE " + drive.serial, "ata_secure_erase"
        )
        await engine.execute(result["run"])
        final = store.get(result["run"]["id"])
        assert final["status"] == "incomplete"
        assert final["progress"] is None and final["task"]["progress_percent"] is None
        assert final["lifecycle"]["eject_status"] == "not_requested"
        assert not engine.hardware.calls
    finally:
        store.close()


async def test_firmware_cancel_is_rejected_without_stopping_active_task(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        drive = engine.drives[0]
        response = await engine.request_erase(
            drive.id, "secure_erase", f"SECURE ERASE {drive.serial}", "ata_secure_erase"
        )
        run = response["run"]
        run["status"] = "running"
        store.save(run)
        engine.active_run_id = run["id"]
        engine.hardware.firmware_erase_active = True
        with pytest.raises(ValueError, match="Firmware erase"):
            await engine.cancel(run["id"], testing_only=True)
        assert store.get(run["id"])["status"] == "running"
        assert not engine.hardware.calls
    finally:
        store.close()


async def test_stale_cancel_preserves_release_window_and_queued_erase_can_cancel(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        drive = engine.drives[0]
        response = await engine.request_erase(
            drive.id, "secure_erase", f"SECURE ERASE {drive.serial}", "ata_secure_erase"
        )
        run = response["run"]
        assert (await engine.cancel(run["id"], testing_only=True))["status"] == "cancelled"
        assert store.get(run["id"])["status"] == "cancelled"
        run.update(status="passed", workflow_status="awaiting_action")
        store.save(run)
        engine.action_waits[run["id"]] = {"event": asyncio.Event()}
        assert (await engine.cancel(run["id"], testing_only=True))["status"] == "already_finished"
        assert store.get(run["id"])["workflow_status"] == "awaiting_action"
        assert not engine.action_waits[run["id"]]["event"].is_set()
        assert not engine.hardware.calls
    finally:
        store.close()


@pytest.mark.parametrize(
    "profile,method,status,label,action",
    [
        ("quick", None, "running", "Cancel test", "cancel"),
        ("full_erase", "full_overwrite", "running", "Cancel erase", "cancel"),
        ("quick_erase", "quick_format", "running", "Cancel erase", "cancel"),
        ("secure_erase", "ata_secure_erase", "queued", "Cancel erase", "cancel"),
        ("secure_erase", "ata_secure_erase", "running", "Why can't I cancel?", "cancel_info"),
    ],
)
async def test_started_telegram_controls_match_actual_cancellation_support(
    tmp_path, profile, method, status, label, action
):
    engine, store = await make_engine(tmp_path)
    try:
        authorize(engine)
        run = await engine.enqueue(engine.drives[0].id, "quick")
        run.update(profile=profile, erase_method=method, status=status)
        store.save(run)
        button = engine.telegram_markup(f"{run['id']}:started")["inline_keyboard"][0][0]
        assert button == {"text": label, "callback_data": f"dc:{run['id']}:{action}"}
        run["status"] = "passed"
        store.save(run)
        markup = engine.telegram_markup(f"{run['id']}:started")
        assert not markup or all(
            not button.get("callback_data", "").endswith((":cancel", ":cancel_info"))
            for row in markup["inline_keyboard"]
            for button in row
        )
    finally:
        store.close()


async def test_full_erase_cancel_stops_work_without_ejecting_partial_drive(tmp_path):
    engine, store = await make_engine(tmp_path)
    try:
        drive = engine.drives[0]
        engine.settings.value["auto_eject"] = True
        engine.hardware.hold = True
        response = await engine.request_erase(
            drive.id, "full_erase", f"FULL ERASE {drive.serial}", "full_overwrite"
        )
        run = response["run"]
        engine.active_run_id = run["id"]
        engine.active_task = asyncio.create_task(engine.execute(run))
        await asyncio.wait_for(engine.hardware.entered.wait(), 1)
        result = await engine.cancel(run["id"], testing_only=True)
        assert result["status"] == "cancelled"
        assert engine.active_task.done()
        stored = store.get(run["id"])
        assert stored["status"] == "cancelled"
        assert stored["lifecycle"]["eject_status"] == "not_requested"
        assert "eject" not in engine.hardware.calls
        assert not engine.hardware.firmware_erase_active
    finally:
        store.close()
