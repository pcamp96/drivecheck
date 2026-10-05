from __future__ import annotations

import asyncio
import json
import stat
import sys
from collections import deque
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from drivecheck.erase import RecoveryJournal, parse_hdparm_security
from drivecheck.hardware import CommandResult, CommandRunner, Drive, Hardware, SafetyError

READY = """
Security:
        Master password revision code = 65534
                supported
                not enabled
                not locked
                not frozen
                12min for SECURITY ERASE UNIT.
Checksum: correct
"""

UNSUPPORTED = """
Security:
                not supported
Checksum: correct
"""


def test_hdparm_security_parser_is_strict_and_scoped() -> None:
    ready = parse_hdparm_security(READY)
    assert ready.ready is True
    assert ready.erase_minutes == 12
    assert parse_hdparm_security(UNSUPPORTED).supported is False
    assert parse_hdparm_security("supported\nnot enabled\nnot locked\nnot frozen").ready is False


def test_recovery_journal_is_private_and_rejects_existing(tmp_path: Path) -> None:
    directory = tmp_path / "recovery"
    journal = RecoveryJournal(directory, "abc123")
    journal.create({"password": "secret", "stage": "prepared"})
    assert journal.path.name == "abc123.json"
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    with pytest.raises(RuntimeError, match="already exists"):
        journal.create({"password": "replacement"})
    journal.update({"password": "secret", "stage": "armed"})
    assert json.loads(journal.path.read_text())["stage"] == "armed"


@pytest.mark.asyncio
async def test_command_runner_redacts_sensitive_result_args() -> None:
    runner = CommandRunner()
    secret = "temporary-ata-password"
    result = await runner.run(
        sys.executable,
        "-c",
        "pass",
        secret,
        timeout=5,
        sensitive_args={3},
    )
    assert secret not in result.args
    assert result.args[3] == "[REDACTED]"


def drive() -> Drive:
    return Drive(
        id="test",
        path="/dev/sda",
        model="Test Disk",
        serial="SERIAL123",
        size_bytes=1_000_000_000,
        transport="usb",
        eligible=True,
        reasons=[],
        identity="a" * 64,
        mounted=False,
    )


class Runner:
    def __init__(self, responses: list[CommandResult]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def run(self, *args: str, **kwargs: object) -> CommandResult:
        self.calls.append((args, kwargs))
        return self.responses.popleft()

    async def cancel(self) -> None:
        return None


class CancellableEraseRunner(Runner):
    def __init__(self) -> None:
        super().__init__([response()])
        self.erase_started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, *args: str, **kwargs: object) -> CommandResult:
        if "--security-erase" not in args:
            return await super().run(*args, **kwargs)
        self.calls.append((args, kwargs))
        self.erase_started.set()
        await self.release.wait()
        return response(returncode=-15)

    async def cancel(self) -> None:
        self.release.set()


def response(stdout="", returncode=0) -> CommandResult:
    return CommandResult(("mock",), returncode, stdout, "")


def safe_hardware(monkeypatch: pytest.MonkeyPatch) -> Hardware:
    hardware = Hardware(demo=False)
    selected = drive()

    async def validate(candidate: Drive, destructive: bool = False) -> Drive:
        assert candidate.identity == selected.identity
        assert destructive is True
        return selected

    monkeypatch.setattr(hardware, "validate", validate)
    monkeypatch.setattr(hardware, "_pin_device", lambda _path: 2049)
    monkeypatch.setattr(hardware, "_exclusive_claim", lambda _path: nullcontext())
    monkeypatch.setattr("drivecheck.hardware.os.geteuid", lambda: 0)
    return hardware


@pytest.mark.asyncio
async def test_plan_prefers_ready_firmware_and_falls_back_only_if_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    hardware._runner = Runner([response(READY)])
    hardware._probe_runner = hardware._runner
    plan = await hardware.erase_plan(drive())
    assert plan["quick"] == {
        "available": True,
        "method": "ata_secure_erase",
        "secure": True,
        "detail": "Drive firmware reports ATA Secure Erase ready.",
        "estimated_minutes": 12,
    }

    hardware._runner = Runner([response(UNSUPPORTED)])
    hardware._probe_runner = hardware._runner
    assert (await hardware.erase_plan(drive()))["quick"]["method"] == "quick_format_exfat"

    frozen = READY.replace("not frozen", "frozen")
    hardware._runner = Runner([response(frozen)])
    hardware._probe_runner = hardware._runner
    blocked = (await hardware.erase_plan(drive()))["quick"]
    assert blocked["available"] is False
    assert blocked["method"] == "ata_secure_erase"


@pytest.mark.asyncio
async def test_expected_method_drift_stops_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    hardware._runner = Runner([response(UNSUPPORTED)])
    hardware._probe_runner = hardware._runner

    async def progress(_percent: int, _detail: str) -> None:
        return None

    with pytest.raises(SafetyError, match="method changed"):
        await hardware.erase(
            drive(),
            "quick_erase",
            progress,
            recovery_dir=tmp_path,
            expected_method="ata_secure_erase",
        )
    assert len(hardware._runner.calls) == 1


@pytest.mark.asyncio
async def test_secure_erase_redacts_password_and_removes_verified_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    hardware._runner = Runner([response(), response()])
    hardware._probe_runner = Runner([response(READY), response(READY), response(READY)])
    updates = []

    async def progress(percent: int, detail: str) -> None:
        updates.append((percent, detail))

    result = await hardware.erase(
        drive(),
        "quick_erase",
        progress,
        recovery_dir=tmp_path / "recovery",
        expected_method="ata_secure_erase",
    )
    assert result["status"] == "passed"
    assert hardware.firmware_erase_active is False
    assert not list((tmp_path / "recovery").iterdir())
    sensitive = [
        kwargs for args, kwargs in hardware._runner.calls if "--security-" in " ".join(args)
    ]
    assert sensitive and all(item["sensitive_args"] == {4} for item in sensitive)
    assert updates[-1][0] == 100
    assert all(percent is None for percent, _detail in updates[:-1])
    assert any("progress unavailable" in detail for _percent, detail in updates[:-1])


@pytest.mark.asyncio
async def test_secure_erase_failure_keeps_recovery_journal_and_blocks_eject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    hardware._runner = Runner([response(), response(returncode=1)])
    hardware._probe_runner = Runner([response(READY), response(READY)])

    async def progress(_percent: int, _detail: str) -> None:
        return None

    recovery = tmp_path / "recovery"
    result = await hardware.erase(
        drive(),
        "quick_erase",
        progress,
        recovery_dir=recovery,
        expected_method="ata_secure_erase",
    )
    assert result["status"] == "incomplete"
    assert result["recovery_required"] is True
    assert hardware.firmware_erase_active is True
    journals = list(recovery.iterdir())
    assert [item.name for item in journals] == [f"{drive().identity}.json"]
    password = json.loads(journals[0].read_text())["password"]
    assert password not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_status",
    [READY.replace("not enabled", "enabled"), READY.replace("not locked", "locked")],
)
async def test_secure_erase_requires_disabled_and_unlocked_terminal_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, post_status: str
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    hardware._runner = Runner([response(), response()])
    hardware._probe_runner = Runner([response(READY), response(READY), response(post_status)])

    async def progress(_percent: int, _detail: str) -> None:
        return None

    result = await hardware.erase(
        drive(),
        "quick_erase",
        progress,
        recovery_dir=tmp_path / "recovery",
        expected_method="ata_secure_erase",
    )
    assert result["status"] == "incomplete"
    assert result["recovery_required"] is True
    assert hardware.firmware_erase_active is True


@pytest.mark.asyncio
async def test_secure_erase_task_cancellation_propagates_with_recovery_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    runner = CancellableEraseRunner()
    hardware._runner = runner
    hardware._probe_runner = Runner([response(READY), response(READY)])

    async def progress(_percent: int, _detail: str) -> None:
        return None

    task = asyncio.create_task(
        hardware.erase(
            drive(),
            "quick_erase",
            progress,
            recovery_dir=tmp_path / "recovery",
            expected_method="ata_secure_erase",
        )
    )
    await runner.erase_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hardware.firmware_erase_active is True
    assert (tmp_path / "recovery" / f"{drive().identity}.json").is_file()


@pytest.mark.asyncio
async def test_quick_format_command_contract_and_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr(
        "drivecheck.hardware.shutil.which",
        lambda name: None if name == "hdparm" else f"/usr/bin/{name}",
    )
    operation = Runner(
        [
            response(),
            response(),
            response(),
            response(),
            response(),
            response(),
            response("TYPE=exfat\n"),
        ]
    )
    hardware._runner = operation
    old = json.dumps(
        {
            "blockdevices": [
                {
                    "path": "/dev/sda",
                    "type": "disk",
                    "children": [{"path": "/dev/sda1", "type": "part", "pkname": "/dev/sda"}],
                }
            ]
        }
    )
    new = old.replace("/dev/sda1", "/dev/sda1")
    hardware._discovery_runner = Runner([response(old), *(response(new) for _ in range(6))])
    claim_state = {"active": False}

    @contextmanager
    def claim(_path: str):
        assert claim_state["active"] is False
        claim_state["active"] = True
        try:
            yield
        finally:
            claim_state["active"] = False

    original_run = operation.run

    async def reject_parent_claim_conflict(*args: str, **kwargs: object) -> CommandResult:
        if args[0] == "mkfs.exfat":
            assert claim_state["active"] is False, "parent O_EXCL would make mkfs return EBUSY"
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(hardware, "_exclusive_claim", claim)
    monkeypatch.setattr(operation, "run", reject_parent_claim_conflict)

    async def progress(_percent: int, _detail: str) -> None:
        return None

    result = await hardware.erase(
        drive(),
        "quick_erase",
        progress,
        recovery_dir=tmp_path,
        expected_method="quick_format_exfat",
    )
    assert result["status"] == "passed"
    calls = [args for args, _kwargs in hardware._runner.calls]
    assert calls[:2] == [
        ("wipefs", "--all", "--force", "/dev/sda1"),
        ("wipefs", "--all", "--force", "/dev/sda"),
    ]
    mkfs = next(call for call in calls if call[0] == "mkfs.exfat")
    assert "-K" in mkfs and "-L" in mkfs and "-f" not in mkfs
    assert calls[-1] == ("blkid", "-o", "export", "/dev/sda1")


@pytest.mark.asyncio
async def test_full_erase_reuses_verified_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hardware = safe_hardware(monkeypatch)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    hardware._runner = Runner([response(UNSUPPORTED)])
    hardware._probe_runner = hardware._runner
    called = []

    async def surface(selected: Drive, progress, destructive=False):
        called.append((selected, progress, destructive))
        return {"status": "passed", "detail": "verified", "raw": {}}

    monkeypatch.setattr(hardware, "surface", surface)

    async def progress(_percent: int, _detail: str) -> None:
        return None

    result = await hardware.erase(
        drive(),
        "full_erase",
        progress,
        recovery_dir=tmp_path,
        expected_method="full_overwrite",
    )
    assert result["method"] == "full_overwrite"
    assert called[0][2] is True
