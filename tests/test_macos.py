from __future__ import annotations

import asyncio
import json
import plistlib
from collections import deque
from contextlib import nullcontext

import pytest

from drivecheck.hardware import CommandResult, Drive, SafetyError
from drivecheck.macos import MacHardware


class PlistRunner:
    def __init__(self, responses: list[CommandResult]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []

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
        pass


class DelayedRunner:
    def __init__(self, response: CommandResult, delay: float = 0.02) -> None:
        self.response = response
        self.delay = delay
        self.calls: list[tuple[str, ...]] = []
        self.cancelled = False

    async def run(self, *args: str, **kwargs: object) -> CommandResult:
        self.calls.append(args)
        await asyncio.sleep(self.delay)
        callback = kwargs.get("stdout_chunk")
        if callback:
            await callback(self.response.stdout)
        return self.response

    async def cancel(self) -> None:
        self.cancelled = True


def plist_result(payload: object, returncode: int = 0) -> CommandResult:
    output = plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode()
    return CommandResult(("fixture",), returncode, output, "")


def external_list(*, mounted: bool = False, identifier: str = "disk4") -> dict:
    partition = {"DeviceIdentifier": f"{identifier}s1", "Size": 999_999_488}
    if mounted:
        partition["MountPoint"] = "/Volumes/Intake"
    return {
        "AllDisksAndPartitions": [
            {
                "DeviceIdentifier": identifier,
                "Size": 1_000_000_000,
                "Partitions": [partition],
            }
        ]
    }


def info(identifier: str = "disk4") -> dict:
    return {
        "DeviceIdentifier": identifier,
        "TotalSize": 1_000_000_000,
        "DeviceBlockSize": 512,
        "MediaName": "Fixture Drive",
        "IORegistryEntryName": "Fixture Drive Media",
        "DeviceTreePath": "IODeviceTree:/fixture/usb-port@02200000",
        "BusProtocol": "USB",
        "Internal": False,
        "Whole": True,
        "VirtualOrPhysical": "Physical",
    }


def registry(identifier: str = "disk4", serial: str = "MAC-SERIAL-1") -> list[dict]:
    return [
        {
            "USB Serial Number": f"   {serial}\x00".encode(),
            "USB Vendor Name": b"Fixture Bridge",
            "USB Product Name": b"USB SATA",
            "IORegistryEntryLocation": "02200000",
            "IORegistryEntryChildren": [
                {
                    "IOObjectClass": "IOMedia",
                    "IORegistryEntryName": "Fixture Drive Media",
                    "IORegistryEntryChildren": [
                        {"BSD Name": identifier.encode()},
                        {"BSD Name": f"{identifier}s1".encode()},
                    ],
                }
            ],
        }
    ]


def inventory(
    *,
    mounted: bool = False,
    identifier: str = "disk4",
    serial: str = "MAC-SERIAL-1",
    apfs: dict | None = None,
    root: dict | None = None,
) -> list[CommandResult]:
    return [
        plist_result(external_list(mounted=mounted, identifier=identifier)),
        plist_result(registry(identifier, serial)),
        plist_result(apfs if apfs is not None else {"Containers": []}),
        plist_result(root if root is not None else {"ParentWholeDisk": "disk0"}),
        plist_result(info(identifier)),
    ]


@pytest.fixture(autouse=True)
def fake_raw_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MacHardware, "_pin_device", staticmethod(lambda _: 1234))
    monkeypatch.setattr(MacHardware, "_exclusive_claim", staticmethod(lambda _: nullcontext()))


@pytest.mark.asyncio
async def test_discovers_unique_external_physical_drive_from_plists() -> None:
    hardware = MacHardware(demo=False)
    hardware._discovery_runner = PlistRunner(inventory())
    drives = await hardware.discover()
    assert len(drives) == 1
    assert drives[0].path == "/dev/rdisk4"
    assert drives[0].serial == "MAC-SERIAL-1"
    assert drives[0].model == "Fixture Drive"
    assert drives[0].eligible is True


@pytest.mark.asyncio
async def test_apfs_synthesized_system_volume_marks_physical_store_unsafe() -> None:
    apfs = {
        "Containers": [
            {
                "ContainerReference": "disk5",
                "PhysicalStores": [{"DeviceIdentifier": "disk4s2"}],
                "Volumes": [{"DeviceIdentifier": "disk5s1", "MountPoint": "/"}],
            }
        ]
    }
    hardware = MacHardware(demo=False)
    hardware._discovery_runner = PlistRunner(
        inventory(apfs=apfs, root={"ParentWholeDisk": "disk5"})
    )
    drive = (await hardware.discover())[0]
    assert drive.mounted is True
    assert drive.eligible is False
    assert {"mounted", "system_drive"} <= set(drive.reasons)


@pytest.mark.asyncio
async def test_missing_or_ambiguous_registry_serial_fails_closed() -> None:
    hardware = MacHardware(demo=False)
    responses = inventory(serial="")
    hardware._discovery_runner = PlistRunner(responses)
    drive = (await hardware.discover())[0]
    assert drive.eligible is False
    assert "missing_serial" in drive.reasons


@pytest.mark.asyncio
async def test_usb_location_disambiguates_identical_media_names() -> None:
    hardware = MacHardware(demo=False)
    other = registry(serial="OTHER-SERIAL")[0]
    other["IORegistryEntryLocation"] = "03300000"
    responses = inventory()
    responses[1] = plist_result([other, registry()[0]])
    hardware._discovery_runner = PlistRunner(responses)
    drive = (await hardware.discover())[0]
    assert drive.serial == "MAC-SERIAL-1"
    assert drive.eligible is True


@pytest.mark.asyncio
async def test_unmount_is_nonforce_and_revalidates() -> None:
    hardware = MacHardware(demo=False)
    hardware._discovery_runner = PlistRunner(
        inventory(mounted=True) + inventory(mounted=True) + inventory(mounted=False)
    )
    operation = PlistRunner([CommandResult(("diskutil",), 0, "Unmount successful", "")])
    hardware._runner = operation
    drive = (await hardware.discover())[0]
    report = await hardware.unmount(drive)
    assert report["status"] == "unmounted"
    assert operation.calls == [("diskutil", "unmountDisk", "/dev/disk4")]


@pytest.mark.asyncio
async def test_eject_requires_unmounted_drive_and_verifies_disappearance() -> None:
    hardware = MacHardware(demo=False)
    absent = {
        "AllDisksAndPartitions": [],
    }
    disappearance = [
        plist_result(absent),
        plist_result([]),
        plist_result({"Containers": []}),
        plist_result({"ParentWholeDisk": "disk0"}),
    ]
    hardware._discovery_runner = PlistRunner(inventory() + inventory() + disappearance)
    operation = PlistRunner([CommandResult(("diskutil",), 0, "Ejected", "")])
    hardware._runner = operation
    drive = (await hardware.discover())[0]
    report = await hardware.eject(drive)
    assert report["status"] == "ejected"
    assert operation.calls == [("diskutil", "eject", "/dev/disk4")]


@pytest.mark.asyncio
async def test_macos_smart_uses_buffered_device_path() -> None:
    hardware = MacHardware(demo=False)
    hardware._discovery_runner = PlistRunner(inventory() + inventory())
    operation = PlistRunner(
        [CommandResult(("smartctl",), 0, '{"smart_status":{"passed":true}}', "")]
    )
    hardware._runner = operation
    drive = (await hardware.discover())[0]
    assert (await hardware.smart(drive))["health"] == "passed"
    assert operation.calls[0][-1] == "/dev/disk4"


@pytest.mark.asyncio
async def test_macos_fio_is_readonly_posixaio_on_raw_device() -> None:
    hardware = MacHardware(demo=False)
    hardware._discovery_runner = PlistRunner(inventory() * 5)
    raw = {
        "jobs": [
            {
                "error": 0,
                "read": {
                    "runtime": 30_000,
                    "io_bytes": 1_000_000_000,
                    "bw_bytes": 100_000_000,
                },
            }
        ]
    }
    operation = PlistRunner([CommandResult(("fio",), 0, json.dumps(raw), "")])
    hardware._runner = operation
    drive = (await hardware.discover())[0]

    async def progress(_: int, __: str) -> None:
        pass

    report = await hardware.benchmark(drive, progress)
    assert report["status"] == "passed"
    command = operation.calls[0]
    assert "--filename=/dev/rdisk4" in command
    assert "--ioengine=posixaio" in command
    assert "--direct=1" not in command
    assert "--readonly" in command


@pytest.mark.asyncio
async def test_macos_full_erase_writes_and_verifies_entire_raw_device() -> None:
    hardware = MacHardware(demo=False)
    hardware._discovery_runner = PlistRunner(inventory() * 5)
    raw = {
        "jobs": [
            {
                "error": 0,
                "read": {"io_bytes": 1_000_000_000},
                "write": {"io_bytes": 1_000_000_000},
            }
        ]
    }
    operation = PlistRunner([CommandResult(("fio",), 0, json.dumps(raw), "")])
    hardware._runner = operation
    drive = (await hardware.discover())[0]

    async def progress(_: float | None, __: str) -> None:
        pass

    report = await hardware.surface(drive, progress, destructive=True)

    assert report["status"] == "passed"
    command = operation.calls[0]
    assert "--filename=/dev/rdisk4" in command
    assert "--ioengine=posixaio" in command
    assert "--direct=1" not in command
    assert "--rw=write" in command
    assert "--verify=sha256" in command
    assert "--do_verify=1" in command
    assert "--readonly" not in command


@pytest.mark.asyncio
async def test_macos_full_erase_stops_when_disk_arbitration_remounts_drive() -> None:
    hardware = MacHardware(demo=False)
    hardware.io_safety_poll_seconds = 0.001
    hardware._discovery_runner = PlistRunner(inventory() * 4 + inventory(mounted=True))
    raw = {
        "jobs": [
            {
                "error": 0,
                "read": {"io_bytes": 1_000_000_000},
                "write": {"io_bytes": 1_000_000_000},
            }
        ]
    }
    operation = DelayedRunner(CommandResult(("fio",), 0, json.dumps(raw), ""))
    hardware._runner = operation
    drive = (await hardware.discover())[0]

    async def progress(_: float | None, __: str) -> None:
        pass

    report = await hardware.surface(drive, progress, destructive=True)

    assert report["status"] == "incomplete"
    assert "mounted" in report["detail"]
    assert operation.cancelled is True


def test_capabilities_offer_manual_destructive_features(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("drivecheck.macos.shutil.which", lambda _: "/tool")
    monkeypatch.setattr("drivecheck.macos.os.geteuid", lambda: 0)
    report = MacHardware(demo=False).capabilities()
    assert report["platform"] == "macos"
    assert report["can_test"] is True
    assert report["can_verify"] is True
    assert report["can_erase"] is True


@pytest.mark.asyncio
async def test_erase_plan_lists_data_partition_and_keeps_firmware_erase_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = MacHardware(demo=False)
    drive = (await _drive_from_inventory(hardware))[0]
    hardware._discovery_runner = PlistRunner(
        inventory()
        + [
            plist_result(
                {
                    "AllDisksAndPartitions": [
                        {
                            "DeviceIdentifier": "disk4",
                            "Partitions": [
                                {
                                    "DeviceIdentifier": "disk4s1",
                                    "Size": 209_715_200,
                                    "Content": "EFI",
                                    "PartitionMapPartitionOffset": 20_480,
                                },
                                {
                                    "DeviceIdentifier": "disk4s2",
                                    "Size": 790_000_000,
                                    "Content": "Microsoft Basic Data",
                                    "VolumeName": "Archive",
                                    "PartitionMapPartitionOffset": 209_735_680,
                                },
                            ],
                        }
                    ]
                }
            ),
            plist_result(
                {
                    "DeviceIdentifier": "disk4s1",
                    "ParentWholeDisk": "disk4",
                    "PartitionMapPartitionOffset": 20_480,
                    "DiskUUID": "EFI-UUID",
                }
            ),
            plist_result(
                {
                    "DeviceIdentifier": "disk4s2",
                    "ParentWholeDisk": "disk4",
                    "PartitionMapPartitionOffset": 209_735_680,
                    "DiskUUID": "DATA-UUID",
                }
            ),
        ]
    )
    monkeypatch.setattr("drivecheck.macos.os.geteuid", lambda: 0)
    monkeypatch.setattr("drivecheck.macos.shutil.which", lambda _: "/tool")

    plan = await hardware.erase_plan(drive)

    assert [target["path"] for target in plan["quick"]["targets"]] == ["/dev/disk4s2"]
    assert plan["initialize"]["available"] is True
    assert plan["full"]["available"] is True
    assert plan["secure"]["available"] is False
    assert "unavailable" in plan["secure"]["detail"].lower()


@pytest.mark.asyncio
async def test_quick_format_excludes_apfs_containers_and_apple_raid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = MacHardware(demo=False)
    drive = _fixture_drive()

    async def records(_: Drive) -> list[dict]:
        return [
            {
                "id": "apfs",
                "path": "/dev/disk4s1",
                "size_bytes": 300_000_000,
                "start_offset": 20_480,
                "partuuid": "APFS-STORE",
                "content": "Apple_APFS",
                "label": "Container disk5 with multiple volumes",
            },
            {
                "id": "raid",
                "path": "/dev/disk4s2",
                "size_bytes": 300_000_000,
                "start_offset": 300_020_480,
                "partuuid": "RAID-MEMBER",
                "content": "Apple_RAID",
                "label": "RAID member",
            },
            {
                "id": "ordinary",
                "path": "/dev/disk4s3",
                "size_bytes": 300_000_000,
                "start_offset": 600_020_480,
                "partuuid": "DATA-PARTITION",
                "content": "Microsoft Basic Data",
                "label": "Ordinary volume",
            },
        ]

    monkeypatch.setattr(hardware, "_partition_records", records)

    targets = await hardware._format_targets(drive)

    assert [target["id"] for target in targets] == ["ordinary"]


@pytest.mark.asyncio
async def test_quick_format_target_id_changes_when_partition_moves() -> None:
    hardware = MacHardware(demo=False)

    def partition_map(offset: int) -> dict:
        return {
            "AllDisksAndPartitions": [
                {
                    "DeviceIdentifier": "disk4",
                    "Partitions": [
                        {
                            "DeviceIdentifier": "disk4s1",
                            "Size": 900_000_000,
                            "Content": "Microsoft Basic Data",
                            "PartitionMapPartitionOffset": offset,
                        }
                    ],
                }
            ]
        }

    def partition_info(offset: int) -> dict:
        return {
            "DeviceIdentifier": "disk4s1",
            "ParentWholeDisk": "disk4",
            "PartitionMapPartitionOffset": offset,
            "DiskUUID": "PARTITION-UUID",
        }

    hardware._discovery_runner = PlistRunner(
        [
            plist_result(partition_map(100_000)),
            plist_result(partition_info(100_000)),
            plist_result(partition_map(200_000)),
            plist_result(partition_info(200_000)),
        ]
    )
    drive = _fixture_drive()

    before = await hardware._format_targets(drive)
    after = await hardware._format_targets(drive)

    assert before[0]["id"] != after[0]["id"]


@pytest.mark.asyncio
async def test_quick_format_targets_only_selected_partition_and_unmounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = MacHardware(demo=False)
    drive = _fixture_drive()
    target = {
        "id": "selected",
        "path": "/dev/disk4s2",
        "size_bytes": 900_000_000,
        "start_offset": 100_000_000,
        "partuuid": "DATA-UUID",
        "filesystem": "Apple_APFS",
        "label": "Archive",
    }

    async def validate(current: Drive, destructive: bool = False) -> Drive:
        assert destructive is True
        return current

    async def targets(_: Drive) -> list[dict]:
        return [target]

    async def topology(_: Drive) -> tuple[tuple[object, ...], ...]:
        return (("/dev/disk4s2", 900_000_000, 100_000_000, "DATA-UUID"),)

    async def plist(*args: str, required: bool) -> dict:
        assert args == ("diskutil", "info", "-plist", "/dev/disk4s2")
        assert required is True
        return {
            "DeviceIdentifier": "disk4s2",
            "ParentWholeDisk": "disk4",
            "FilesystemType": "exfat",
        }

    monkeypatch.setattr(hardware, "validate", validate)
    monkeypatch.setattr(hardware, "_format_targets", targets)
    monkeypatch.setattr(hardware, "_partition_topology", topology)
    monkeypatch.setattr(hardware, "_plist_command", plist)
    operation = PlistRunner(
        [
            CommandResult(("diskutil",), 0, "Finished erase", ""),
            CommandResult(("diskutil",), 0, "Unmounted", ""),
        ]
    )
    hardware._runner = operation

    async def progress(_: float | None, __: str) -> None:
        pass

    report = await hardware._quick_format_exfat(drive, progress, "selected")

    assert report["status"] == "passed"
    assert operation.calls == [
        ("diskutil", "eraseVolume", "ExFAT", "DRIVECHECK", "/dev/disk4s2"),
        ("diskutil", "unmountDisk", "/dev/disk4"),
    ]


@pytest.mark.asyncio
async def test_quick_format_rejects_partition_map_change_before_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = MacHardware(demo=False)
    drive = _fixture_drive()
    target = {
        "id": "selected",
        "path": "/dev/disk4s2",
        "size_bytes": 900_000_000,
        "start_offset": 100_000_000,
        "partuuid": "DATA-UUID",
    }

    async def validate(current: Drive, destructive: bool = False) -> Drive:
        assert destructive is True
        return current

    async def targets(_: Drive) -> list[dict]:
        return [target]

    topologies = deque(
        [
            (("/dev/disk4s2", 900_000_000, 100_000_000, "DATA-UUID"),),
            (("/dev/disk4s2", 900_000_000, 200_000_000, "DATA-UUID"),),
        ]
    )

    async def topology(_: Drive) -> tuple[tuple[object, ...], ...]:
        return topologies.popleft()

    monkeypatch.setattr(hardware, "validate", validate)
    monkeypatch.setattr(hardware, "_format_targets", targets)
    monkeypatch.setattr(hardware, "_partition_topology", topology)
    operation = PlistRunner([])
    hardware._runner = operation

    async def progress(_: float | None, __: str) -> None:
        pass

    with pytest.raises(SafetyError, match="partition map changed before quick format"):
        await hardware._quick_format_exfat(drive, progress, "selected")
    assert operation.calls == []


@pytest.mark.asyncio
async def test_quick_format_rejects_partition_map_change_during_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = MacHardware(demo=False)
    drive = _fixture_drive()
    target = {
        "id": "selected",
        "path": "/dev/disk4s2",
        "size_bytes": 900_000_000,
        "start_offset": 100_000_000,
        "partuuid": "DATA-UUID",
    }

    async def validate(current: Drive, destructive: bool = False) -> Drive:
        assert destructive is True
        return current

    async def targets(_: Drive) -> list[dict]:
        return [target]

    topologies = deque(
        [
            (("/dev/disk4s2", 900_000_000, 100_000_000, "DATA-UUID"),),
            (("/dev/disk4s2", 900_000_000, 100_000_000, "DATA-UUID"),),
            (("/dev/disk4s2", 900_000_000, 200_000_000, "DATA-UUID"),),
        ]
    )

    async def topology(_: Drive) -> tuple[tuple[object, ...], ...]:
        return topologies.popleft()

    async def plist(*args: str, required: bool) -> dict:
        assert args == ("diskutil", "info", "-plist", "/dev/disk4s2")
        assert required is True
        return {
            "DeviceIdentifier": "disk4s2",
            "ParentWholeDisk": "disk4",
            "FilesystemType": "exfat",
        }

    monkeypatch.setattr(hardware, "validate", validate)
    monkeypatch.setattr(hardware, "_format_targets", targets)
    monkeypatch.setattr(hardware, "_partition_topology", topology)
    monkeypatch.setattr(hardware, "_plist_command", plist)
    operation = PlistRunner([CommandResult(("diskutil",), 0, "Finished erase", "")])
    hardware._runner = operation

    async def progress(_: float | None, __: str) -> None:
        pass

    with pytest.raises(SafetyError, match="partition map changed during quick format"):
        await hardware._quick_format_exfat(drive, progress, "selected")
    assert operation.calls == [
        ("diskutil", "eraseVolume", "ExFAT", "DRIVECHECK", "/dev/disk4s2")
    ]


@pytest.mark.asyncio
async def test_initialize_replaces_whole_disk_with_gpt_exfat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = MacHardware(demo=False)
    drive = _fixture_drive()
    target = {
        "id": "new-volume",
        "path": "/dev/disk4s2",
        "size_bytes": 900_000_000,
    }

    async def validate(current: Drive, destructive: bool = False) -> Drive:
        assert destructive is True
        return current

    async def targets(_: Drive) -> list[dict]:
        return [target]

    async def plist(*args: str, required: bool) -> dict:
        assert args == ("diskutil", "info", "-plist", "/dev/disk4s2")
        assert required is True
        return {"FilesystemType": "exfat"}

    monkeypatch.setattr(hardware, "validate", validate)
    monkeypatch.setattr(hardware, "_format_targets", targets)
    monkeypatch.setattr(hardware, "_plist_command", plist)
    operation = PlistRunner(
        [
            CommandResult(("diskutil",), 0, "Finished erase", ""),
            CommandResult(("diskutil",), 0, "Unmounted", ""),
        ]
    )
    hardware._runner = operation

    async def progress(_: float | None, __: str) -> None:
        pass

    report = await hardware._initialize_exfat(drive, progress)

    assert report["status"] == "passed"
    assert operation.calls[0] == (
        "diskutil",
        "eraseDisk",
        "ExFAT",
        "DRIVECHECK",
        "GPT",
        "/dev/disk4",
    )
    assert operation.calls[1] == ("diskutil", "unmountDisk", "/dev/disk4")


@pytest.mark.asyncio
async def test_initialize_revalidates_after_progress_callback_before_erase_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = MacHardware(demo=False)
    drive = _fixture_drive()
    drifted = False

    async def validate(current: Drive, destructive: bool = False) -> Drive:
        assert destructive is True
        if drifted:
            raise SafetyError("drive identity changed during progress callback")
        return current

    monkeypatch.setattr(hardware, "validate", validate)
    operation = PlistRunner([])
    hardware._runner = operation

    async def progress(_: float | None, __: str) -> None:
        nonlocal drifted
        drifted = True

    with pytest.raises(SafetyError, match="identity changed"):
        await hardware._initialize_exfat(drive, progress)
    assert operation.calls == []


async def _drive_from_inventory(hardware: MacHardware) -> list[Drive]:
    hardware._discovery_runner = PlistRunner(inventory())
    return await hardware.discover()


def _fixture_drive() -> Drive:
    return Drive(
        id="fixture",
        path="/dev/rdisk4",
        model="Fixture Drive",
        serial="MAC-SERIAL-1",
        size_bytes=1_000_000_000,
        transport="usb",
        eligible=True,
        reasons=[],
        identity="fixture",
        mounted=False,
    )
