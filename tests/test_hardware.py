from __future__ import annotations

import asyncio
import json
import sys
from collections import deque

import pytest

from drivecheck.hardware import CommandError, CommandResult, CommandRunner, Hardware, SafetyError


def result(args: tuple[str, ...], payload: object, returncode: int = 0) -> CommandResult:
    return CommandResult(args, returncode, json.dumps(payload), "")


class FakeRunner:
    def __init__(self, responses: list[CommandResult]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []
        self.cancelled = False

    async def run(self, *args: str, **_: object) -> CommandResult:
        self.calls.append(args)
        if not self.responses:
            raise AssertionError(f"unexpected command: {args}")
        return self.responses.popleft()

    async def cancel(self) -> None:
        self.cancelled = True


def lsblk(*devices: dict[str, object]) -> dict[str, object]:
    return {"blockdevices": list(devices)}


def disk(
    path: str = "/dev/sda",
    *,
    serial: str = "ABC123",
    tran: str = "usb",
    mounts: list[str | None] | None = None,
    children: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "type": "disk",
        "size": 1_000_000_000,
        "model": "Test Disk",
        "serial": serial,
        "tran": tran,
        "mountpoints": mounts or [None],
        "children": children or [],
    }


@pytest.mark.asyncio
async def test_demo_never_runs_host_commands() -> None:
    hardware = Hardware(demo=True)
    hardware._runner = FakeRunner([])
    drives = await hardware.discover()
    assert len(drives) == 1
    assert drives[0].eligible
    assert (await hardware.smart(drives[0]))["health"] == "passed"


@pytest.mark.asyncio
async def test_discovery_allows_only_unique_unmounted_serial_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    root_child = {
        "name": "mmcblk0p2",
        "path": "/dev/mmcblk0p2",
        "type": "part",
        "mountpoints": ["/"],
    }
    payload = lsblk(
        disk(),
        disk("/dev/sdb", serial="", tran="usb"),
        disk("/dev/nvme0n1", serial="SYS", tran="nvme", children=[root_child]),
    )
    hardware._runner = FakeRunner([result(("lsblk",), payload)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drives = await hardware.discover()
    assert drives[0].eligible is True
    assert drives[1].eligible is False and "missing_serial" in drives[1].reasons
    assert drives[2].eligible is False
    assert {"not_external_usb", "mounted", "system_drive"} <= set(drives[2].reasons)


@pytest.mark.asyncio
async def test_duplicate_serials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    payload = lsblk(disk("/dev/sda"), disk("/dev/sdb"))
    hardware._runner = FakeRunner([result(("lsblk",), payload)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drives = await hardware.discover()
    assert all(not drive.eligible for drive in drives)
    assert all("duplicate_identity" in drive.reasons for drive in drives)


@pytest.mark.asyncio
async def test_validate_rejects_new_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    hardware._runner = FakeRunner(
        [
            result(("lsblk",), lsblk(disk())),
            result(("lsblk",), lsblk(disk(mounts=["/media/test"]))),
        ]
    )
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    with pytest.raises(SafetyError, match="mounted"):
        await hardware.validate(drive, destructive=True)


@pytest.mark.asyncio
async def test_smartctl_exit_is_decoded_as_bitmask(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    smart = {"smart_status": {"passed": True}}
    hardware._runner = FakeRunner(
        [
            result(("lsblk",), lsblk(disk())),
            result(("lsblk",), lsblk(disk())),
            result(("smartctl",), smart, returncode=64),
        ]
    )
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    report = await hardware.smart(drive)
    assert report["health"] == "warning"
    assert report["warnings"] == ["the error log contains records"]


@pytest.mark.asyncio
async def test_self_test_polls_to_terminal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    running = {
        "smart_status": {"passed": True},
        "ata_smart_data": {"self_test": {"status": {"remaining_percent": 40}}},
    }
    failed = {
        "smart_status": {"passed": True},
        "ata_smart_self_test_log": {
            "standard": {"table": [{"status": {"string": "Completed: read failure"}}]}
        },
    }
    responses = [
        result(("lsblk",), lsblk(disk())),  # initial discover
        result(("lsblk",), lsblk(disk())),  # self_test validate
        CommandResult(("smartctl",), 0, "test started", ""),
        result(("lsblk",), lsblk(disk())),  # loop validate
        result(("lsblk",), lsblk(disk())),  # smart validate
        result(("smartctl",), running),
        result(("lsblk",), lsblk(disk())),
        result(("lsblk",), lsblk(disk())),
        result(("smartctl",), failed),
    ]
    hardware._runner = FakeRunner(responses)
    hardware.self_test_poll_seconds = 0
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    updates: list[tuple[int, str]] = []

    async def progress(percent: int, detail: str) -> None:
        updates.append((percent, detail))

    report = await hardware.self_test(drive, progress)
    assert report["status"] == "failed"
    assert any(percent == 60 for percent, _ in updates)
    assert updates[-1][0] == 100


@pytest.mark.asyncio
async def test_fio_revalidates_after_io(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    fio = {"jobs": [{"error": 0, "read": {"bw_bytes": 150_000_000}}]}
    hardware._runner = FakeRunner(
        [
            result(("lsblk",), lsblk(disk())),
            result(("lsblk",), lsblk(disk())),
            result(("lsblk",), lsblk(disk())),
            result(("fio",), fio),
            result(("lsblk",), lsblk(disk(mounts=["/mnt/new"]))),
        ]
    )
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.benchmark(drive, progress)
    assert report["status"] == "incomplete"
    assert "mounted" in report["detail"]


@pytest.mark.asyncio
async def test_destructive_surface_uses_checksum_and_read_write(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    fio = {"jobs": [{"error": 0, "write": {"io_bytes": 1_000_000_000}}]}
    hardware._runner = FakeRunner(
        [
            result(("lsblk",), lsblk(disk())),
            result(("lsblk",), lsblk(disk())),
            result(("lsblk",), lsblk(disk())),
            result(("fio",), fio),
            result(("lsblk",), lsblk(disk())),
        ]
    )
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.surface(drive, progress, destructive=True)
    assert report["status"] == "passed"
    fio_call = next(call for call in hardware._runner.calls if call[0] == "fio")
    assert "--verify=sha256" in fio_call
    assert "--do_verify=1" in fio_call
    assert "--readonly=0" in fio_call


@pytest.mark.asyncio
async def test_command_runner_bounds_output() -> None:
    runner = CommandRunner(output_limit=128)
    command = "import sys; sys.stdout.write('x' * 200000)"
    response = await runner.run(sys.executable, "-c", command, timeout=5)
    assert len(response.stdout.encode()) == 128
    assert response.truncated is True


@pytest.mark.asyncio
async def test_command_runner_timeout_kills_child() -> None:
    runner = CommandRunner()
    with pytest.raises(CommandError, match="timed out"):
        await runner.run(sys.executable, "-c", "import time; time.sleep(30)", timeout=0.05)
    assert runner._process is None


@pytest.mark.asyncio
async def test_command_runner_cancellation_kills_child() -> None:
    runner = CommandRunner()
    task = asyncio.create_task(
        runner.run(sys.executable, "-c", "import time; time.sleep(30)", timeout=60)
    )
    for _ in range(100):
        if runner._process is not None:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner._process is None
