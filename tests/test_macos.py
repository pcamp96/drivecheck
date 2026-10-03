from __future__ import annotations

import json
import plistlib
from collections import deque
from contextlib import nullcontext

import pytest

from drivecheck.hardware import CommandResult, Drive
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
    assert "--readonly" in command


@pytest.mark.asyncio
async def test_destructive_surface_is_always_unsupported() -> None:
    hardware = MacHardware(demo=False)

    async def progress(_: int, __: str) -> None:
        pass

    drive = Drive(
        id="fixture",
        path="/dev/rdisk4",
        model="Fixture",
        serial="SERIAL",
        size_bytes=512,
        transport="usb",
        eligible=True,
        reasons=[],
        identity="fixture",
        mounted=False,
    )
    report = await hardware.surface(drive, progress, destructive=True)
    assert report["status"] == "unsupported"


def test_capabilities_never_offer_destructive_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("drivecheck.macos.shutil.which", lambda _: "/tool")
    monkeypatch.setattr("drivecheck.macos.os.geteuid", lambda: 0)
    report = MacHardware(demo=False).capabilities()
    assert report["platform"] == "macos"
    assert report["can_test"] is True
    assert report["can_verify"] is False
