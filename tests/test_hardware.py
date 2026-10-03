from __future__ import annotations

import asyncio
import builtins
import json
import sys
from collections import deque
from contextlib import nullcontext

import pytest

from drivecheck.hardware import (
    CommandError,
    CommandResult,
    CommandRunner,
    Hardware,
    SafetyError,
    get_hardware,
)


def result(args: tuple[str, ...], payload: object, returncode: int = 0) -> CommandResult:
    return CommandResult(args, returncode, json.dumps(payload), "")


class FakeRunner:
    def __init__(self, responses: list[CommandResult]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []
        self.cancelled = False

    async def run(self, *args: str, **kwargs: object) -> CommandResult:
        self.calls.append(args)
        if not self.responses:
            raise AssertionError(f"unexpected command: {args}")
        response = self.responses.popleft()
        callback = kwargs.get("stdout_chunk")
        if callback:
            await callback(response.stdout)
        return response

    async def cancel(self) -> None:
        self.cancelled = True


class BlockingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.cancelled = False

    async def run(self, *args: str, **_: object) -> CommandResult:
        self.calls.append(args)
        self.started.set()
        await self.released.wait()
        return CommandResult(args, -15, "", "cancelled")

    async def cancel(self) -> None:
        self.cancelled = True
        self.released.set()


class StreamingRunner(FakeRunner):
    def __init__(self, retained: str, streamed: str) -> None:
        super().__init__([CommandResult(("fio",), 0, retained, "", truncated=True)])
        self.streamed = streamed

    async def run(self, *args: str, **kwargs: object) -> CommandResult:
        callback = kwargs.pop("stdout_chunk", None)
        response = await super().run(*args, **kwargs)
        if callback:
            await callback(self.streamed)
        return response


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
        "log-sec": 512,
        "mountpoints": mounts or [None],
        "children": children or [],
    }


def discovery(hardware: Hardware, *payloads: dict[str, object]) -> FakeRunner:
    runner = FakeRunner([result(("lsblk",), payload) for payload in payloads])
    hardware._discovery_runner = runner
    return runner


@pytest.fixture(autouse=True)
def fake_block_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Hardware, "_pin_device", staticmethod(lambda _: 2049))
    monkeypatch.setattr(Hardware, "_exclusive_claim", staticmethod(lambda _: nullcontext()))


@pytest.mark.asyncio
async def test_demo_never_runs_host_commands() -> None:
    hardware = Hardware(demo=True)
    hardware._runner = FakeRunner([])
    hardware._discovery_runner = FakeRunner([])
    drives = await hardware.discover()
    assert len(drives) == 1
    assert drives[0].eligible
    assert (await hardware.smart(drives[0]))["health"] == "passed"
    assert (await hardware.unmount(drives[0]))["status"] == "unmounted"
    assert (await hardware.eject(drives[0]))["status"] == "ejected"


def test_platform_factory_keeps_demo_platform_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("drivecheck.hardware.platform.system", lambda: "Darwin")
    assert type(get_hardware(demo=True)) is Hardware
    assert get_hardware(demo=False).__class__.__name__ == "MacHardware"


def test_linux_capabilities_require_root_and_fio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("drivecheck.hardware.os.geteuid", lambda: 1000)
    monkeypatch.setattr(
        "drivecheck.hardware.shutil.which",
        lambda name: f"/usr/bin/{name}" if name != "smartctl" else None,
    )
    report = Hardware(demo=False).capabilities()
    assert report["platform"] == "linux"
    assert report["can_test"] is False
    assert report["can_verify"] is False
    assert any("smartctl" in limitation for limitation in report["limitations"])


@pytest.mark.asyncio
async def test_discovery_allows_only_unique_unmounted_serial_usb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    discovery(hardware, payload)
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
    discovery(hardware, payload)
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drives = await hardware.discover()
    assert all(not drive.eligible for drive in drives)
    assert all("duplicate_identity" in drive.reasons for drive in drives)


@pytest.mark.asyncio
async def test_validate_rejects_new_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, lsblk(disk()), lsblk(disk(mounts=["/media/test"])))
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    with pytest.raises(SafetyError, match="mounted"):
        await hardware.validate(drive, destructive=True)


@pytest.mark.asyncio
async def test_validate_rejects_same_identity_at_new_path(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, lsblk(disk()), lsblk(disk("/dev/sdb")))
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    with pytest.raises(SafetyError, match="path changed"):
        await hardware.validate(drive)


@pytest.mark.asyncio
async def test_linux_eject_refuses_multidisk_usb_enclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, lsblk(disk()), lsblk(disk()))
    operation = FakeRunner([])
    hardware._runner = operation
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda _: "/usr/bin/udisksctl")
    monkeypatch.setattr(hardware, "_usb_enclosure_siblings", lambda _: {"/dev/sdb"})
    drive = (await hardware.discover())[0]
    report = await hardware.eject(drive)
    assert report["status"] == "failed"
    assert "/dev/sdb" in report["detail"]
    assert operation.calls == []


@pytest.mark.asyncio
async def test_linux_eject_powers_off_only_verified_single_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, lsblk(disk()), lsblk(disk()), lsblk())
    operation = FakeRunner([CommandResult(("udisksctl",), 0, "Powered off", "")])
    hardware._runner = operation
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda _: "/usr/bin/udisksctl")
    monkeypatch.setattr(hardware, "_usb_enclosure_siblings", lambda _: set())
    drive = (await hardware.discover())[0]
    report = await hardware.eject(drive)
    assert report["status"] == "ejected"
    assert operation.calls == [
        ("udisksctl", "power-off", "--no-user-interaction", "-b", "/dev/sda")
    ]


@pytest.mark.asyncio
async def test_unreadable_swap_state_fails_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, lsblk(disk()))
    real_open = builtins.open

    def denied(path: object, *args: object, **kwargs: object):
        if path == "/proc/swaps":
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(SafetyError, match="swap"):
        await hardware.discover()


@pytest.mark.asyncio
async def test_smartctl_exit_is_decoded_as_bitmask(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    smart = {"smart_status": {"passed": True}}
    discovery(hardware, lsblk(disk()), lsblk(disk()))
    hardware._runner = FakeRunner([result(("smartctl",), smart, returncode=64)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    report = await hardware.smart(drive)
    assert report["health"] == "warning"
    assert report["warnings"] == ["the error log contains records"]


@pytest.mark.asyncio
async def test_smart_command_failure_is_unsupported_even_with_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, lsblk(disk()), lsblk(disk()))
    hardware._runner = FakeRunner(
        [result(("smartctl",), {"smart_status": {"passed": True}}, returncode=65)]
    )
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    report = await hardware.smart(drive)
    assert report["health"] == "unsupported"
    assert "command line" in report["warnings"][0]
    assert any("error log" in warning for warning in report["warnings"])


@pytest.mark.asyncio
async def test_known_smart_failure_wins_over_command_failure_bits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, lsblk(disk()), lsblk(disk()))
    hardware._runner = FakeRunner(
        [result(("smartctl",), {"smart_status": {"passed": False}}, returncode=9)]
    )
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    assert (await hardware.smart(drive))["health"] == "failed"


@pytest.mark.asyncio
async def test_smart_warns_for_ata_and_scsi_media_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    raw = {
        "smart_status": {"passed": True},
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "raw": {"value": 2}},
                {"id": 197, "raw": {"value": 1}},
                {"id": 198, "raw": {"value": 3}},
            ]
        },
        "scsi_grown_defect_list": 4,
        "scsi_error_counter_log": {"read": {"total_uncorrected_errors": 5}},
    }
    discovery(hardware, lsblk(disk()), lsblk(disk()))
    hardware._runner = FakeRunner([result(("smartctl",), raw)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    report = await hardware.smart(drive)
    assert report["health"] == "warning"
    assert len(report["warnings"]) == 5


@pytest.mark.asyncio
async def test_self_test_polls_to_terminal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    failed = {
        "smart_status": {"passed": True},
        "ata_smart_self_test_log": {
            "standard": {"table": [{"status": {"string": "Completed: read failure"}}]}
        },
    }
    previous = {
        "smart_status": {"passed": True},
        "ata_smart_self_test_log": {
            "standard": {
                "table": [{"status": {"string": "Completed without error"}, "lifetime_hours": 10}]
            }
        },
    }
    discovery(hardware, *(lsblk(disk()) for _ in range(7)))
    hardware._runner = FakeRunner(
        [
            result(("smartctl",), previous),
            CommandResult(("smartctl",), 0, "test started", ""),
            result(("smartctl",), previous),
            result(("smartctl",), failed),
        ]
    )
    hardware.self_test_poll_seconds = 0
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    updates: list[tuple[int, str]] = []

    async def progress(percent: int, detail: str) -> None:
        updates.append((percent, detail))

    report = await hardware.self_test(drive, progress)
    assert report["status"] == "failed"
    assert any("Waiting for a new" in detail for _, detail in updates)
    assert updates[-1][0] == 100


@pytest.mark.asyncio
async def test_self_test_does_not_adopt_existing_test(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    running = {
        "smart_status": {"passed": True},
        "ata_smart_data": {"self_test": {"status": {"remaining_percent": 90}}},
    }
    discovery(hardware, *(lsblk(disk()) for _ in range(3)))
    operation = FakeRunner([result(("smartctl",), running)])
    hardware._runner = operation
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.self_test(drive, progress)
    assert report["status"] == "incomplete"
    assert "already running" in report["detail"]
    assert all("-t" not in call for call in operation.calls)


@pytest.mark.asyncio
async def test_observed_running_test_can_complete_with_identical_log_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    completed = {
        "smart_status": {"passed": True},
        "ata_smart_self_test_log": {
            "standard": {"table": [{"status": {"string": "Completed without error"}}]}
        },
    }
    running = {
        "smart_status": {"passed": True},
        "ata_smart_data": {"self_test": {"status": {"remaining_percent": 50}}},
        **{key: value for key, value in completed.items() if key != "smart_status"},
    }
    discovery(hardware, *(lsblk(disk()) for _ in range(7)))
    hardware._runner = FakeRunner(
        [
            result(("smartctl",), completed),
            CommandResult(("smartctl",), 0, "started", ""),
            result(("smartctl",), running),
            result(("smartctl",), completed),
        ]
    )
    hardware.self_test_poll_seconds = 0
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    assert (await hardware.self_test(drive, progress))["status"] == "passed"


@pytest.mark.asyncio
async def test_self_test_timeout_aborts_only_after_identity_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    initial = {"smart_status": {"passed": True}}
    discovery(hardware, *(lsblk(disk()) for _ in range(4)))
    operation = FakeRunner(
        [
            result(("smartctl",), initial),
            CommandResult(("smartctl",), 0, "started", ""),
            CommandResult(("smartctl",), 0, "", ""),
        ]
    )
    hardware._runner = operation
    hardware.self_test_poll_seconds = 1
    hardware.self_test_timeout_seconds = 0.001
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.self_test(drive, progress)
    assert report["status"] == "incomplete"
    assert any(call[1:2] == ("-X",) for call in operation.calls)


@pytest.mark.asyncio
async def test_fio_revalidates_after_io(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    fio = {"jobs": [{"error": 0, "read": {"bw_bytes": 150_000_000, "io_bytes": 1_000_000_000}}]}
    discovery(
        hardware,
        lsblk(disk()),
        lsblk(disk()),
        lsblk(disk()),
        lsblk(disk()),
        lsblk(disk(mounts=["/mnt/new"])),
    )
    hardware._runner = FakeRunner([result(("fio",), fio)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.benchmark(drive, progress)
    assert report["status"] == "incomplete"
    assert "mounted" in report["detail"]


@pytest.mark.asyncio
async def test_fio_safety_poll_cancels_when_drive_becomes_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    discovery(
        hardware,
        lsblk(disk()),
        lsblk(disk()),
        lsblk(disk()),
        lsblk(disk()),
        lsblk(disk(mounts=["/mnt/surprise"])),
    )
    operation = BlockingRunner()
    hardware._runner = operation
    hardware.io_safety_poll_seconds = 0.001
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.surface(drive, progress)
    assert report["status"] == "incomplete"
    assert "mounted" in report["detail"]
    assert operation.cancelled is True


@pytest.mark.asyncio
async def test_fio_task_cancellation_does_not_orphan_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    discovery(hardware, *(lsblk(disk()) for _ in range(4)))
    operation = BlockingRunner()
    hardware._runner = operation
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    task = asyncio.create_task(hardware.surface(drive, progress))
    await operation.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert operation.cancelled is True


@pytest.mark.asyncio
async def test_destructive_surface_uses_checksum_and_read_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    fio = {
        "jobs": [
            {
                "error": 0,
                "write": {"io_bytes": 1_000_000_000},
                "read": {"io_bytes": 1_000_000_000},
            }
        ]
    }
    discovery(hardware, *(lsblk(disk()) for _ in range(5)))
    hardware._runner = FakeRunner([result(("fio",), fio)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.surface(drive, progress, destructive=True)
    assert report["status"] == "passed"
    fio_call = next(call for call in hardware._runner.calls if call[0] == "fio")
    assert "--verify=sha256" in fio_call
    assert "--do_verify=1" in fio_call
    assert "--readonly" not in fio_call
    assert "--allow_file_create=0" in fio_call
    assert "--verify_backlog=1024" in fio_call


@pytest.mark.asyncio
async def test_full_surface_selects_aligned_block_size_for_non_mib_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    odd_size = 4_000_787_030_016
    odd = disk()
    odd["size"] = odd_size
    raw = {"jobs": [{"error": 0, "read": {"io_bytes": odd_size}}]}
    discovery(hardware, *(lsblk(odd) for _ in range(5)))
    hardware._runner = FakeRunner([result(("fio",), raw)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.surface(drive, progress)
    assert report["status"] == "passed"
    fio_call = next(call for call in hardware._runner.calls if call[0] == "fio")
    assert "--bs=8192" in fio_call
    assert f"--size={odd_size}" in fio_call


@pytest.mark.asyncio
async def test_fio_concatenated_json_reports_progress_and_requires_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    halfway = {"jobs": [{"error": 0, "read": {"runtime": 15_000, "io_bytes": 10}}]}
    final = {
        "jobs": [
            {
                "error": 0,
                "read": {"runtime": 30_000, "io_bytes": 1_000_000_000, "bw_bytes": 100_000_000},
            }
        ]
    }
    joined = json.dumps(halfway) + "\n" + json.dumps(final)
    discovery(hardware, *(lsblk(disk()) for _ in range(5)))
    hardware._runner = FakeRunner([CommandResult(("fio",), 0, joined, "")])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]
    updates: list[int] = []

    async def progress(percent: int, _: str) -> None:
        updates.append(percent)

    report = await hardware.benchmark(drive, progress)
    assert report["status"] == "passed"
    assert 50 in updates
    assert report["read_mbps"] == 100.0


@pytest.mark.asyncio
async def test_fio_uses_latest_streamed_json_when_retained_output_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = Hardware(demo=False)
    early = {"jobs": [{"error": 0, "read": {"runtime": 1000, "io_bytes": 1}}]}
    final = {
        "jobs": [
            {
                "error": 0,
                "read": {"runtime": 30_000, "io_bytes": 1_000_000_000, "bw_bytes": 90_000_000},
            }
        ]
    }
    discovery(hardware, *(lsblk(disk()) for _ in range(5)))
    hardware._runner = StreamingRunner(json.dumps(early), json.dumps(final))
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.benchmark(drive, progress)
    assert report["status"] == "passed"
    assert report["read_mbps"] == 90.0


@pytest.mark.asyncio
async def test_fio_zero_io_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = Hardware(demo=False)
    raw = {"jobs": [{"error": 0, "read": {"io_bytes": 0, "bw_bytes": 0}}]}
    discovery(hardware, *(lsblk(disk()) for _ in range(5)))
    hardware._runner = FakeRunner([result(("fio",), raw)])
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.benchmark(drive, progress)
    assert report["status"] == "incomplete"
    assert "expected number" in report["detail"]


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


@pytest.mark.asyncio
async def test_command_runner_callback_failure_stops_child_immediately() -> None:
    runner = CommandRunner()

    async def fail(_: str) -> None:
        raise RuntimeError("persistence failed")

    command = "import time; print('status', flush=True); time.sleep(30)"
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match="persistence failed"):
        await runner.run(sys.executable, "-c", command, timeout=60, stdout_chunk=fail)
    assert asyncio.get_running_loop().time() - started < 2
    assert runner._process is None
