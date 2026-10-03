import asyncio
import json
from collections import deque
from pathlib import Path

import pytest

from drivecheck.hardware import CommandResult, Hardware, SafetyError
from drivecheck.raid import RaidInspector, RaidOwnership


def node(root: Path, name: str) -> Path:
    path = root / name
    (path / "holders").mkdir(parents=True)
    (path / "slaves").mkdir()
    return path


def link(directory: Path, target: Path) -> None:
    (directory / target.name).symlink_to(target, target_is_directory=True)


def sysfs(tmp_path: Path, *, state="inactive", member="sda2", upper=False, md=True):
    root = tmp_path / "block"
    disk = node(root, "sda")
    part = node(root, "sda2")
    other = node(root, "sdb2")
    array = node(root, "md127")
    if md:
        (array / "md").mkdir()
        (array / "md/array_state").write_text(state)
    link(part / "holders", array)
    link(array / "slaves", part if member == "sda2" else other)
    if upper:
        upper_node = node(root, "dm-0")
        link(array / "holders", upper_node)
    return root, disk, part, array


def test_inspector_allows_only_inactive_partition_members_on_selected_drive(tmp_path):
    root, _, _, _ = sysfs(tmp_path)
    claim = RaidInspector(root).inspect("/dev/sda", {"/dev/sda2"}, {"/dev/md127"})
    assert claim.blocked and claim.releasable
    assert claim.arrays == [{"path": "/dev/md127", "state": "inactive", "members": ["/dev/sda2"]}]


@pytest.mark.parametrize("state", ["active", "read-auto", "readonly", "clean"])
def test_inspector_rejects_any_noninactive_array(tmp_path, state):
    root, _, _, _ = sysfs(tmp_path, state=state)
    claim = RaidInspector(root).inspect("/dev/sda", {"/dev/sda2"}, {"/dev/md127"})
    assert claim.blocked and not claim.releasable
    assert state in claim.detail


def test_inspector_rejects_shared_array_other_holders_and_non_md(tmp_path):
    root, _, _, _ = sysfs(tmp_path / "shared", member="sdb2")
    assert not RaidInspector(root).inspect("/dev/sda", {"/dev/sda2"}, {"/dev/md127"}).releasable

    root, _, _, _ = sysfs(tmp_path / "upper", upper=True)
    assert (
        "another block layer"
        in RaidInspector(root).inspect("/dev/sda", {"/dev/sda2"}, {"/dev/md127"}).detail
    )

    root, _, _, _ = sysfs(tmp_path / "lvm", md=False)
    assert (
        "not an MD array"
        in RaidInspector(root).inspect("/dev/sda", {"/dev/sda2"}, {"/dev/md127"}).detail
    )


def test_inspector_fails_closed_when_sysfs_nodes_are_missing(tmp_path):
    claim = RaidInspector(tmp_path / "missing").inspect("/dev/sda", {"/dev/sda2"}, {"/dev/md127"})
    assert claim.blocked and not claim.releasable
    assert "could not be read" in claim.detail


def test_inspector_fails_closed_when_lsblk_and_sysfs_disagree(tmp_path):
    root, _, _, _ = sysfs(tmp_path)
    claim = RaidInspector(root).inspect("/dev/sda", {"/dev/sda2"}, set())
    assert claim.blocked and not claim.releasable
    assert "do not agree" in claim.detail


class Runner:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    async def run(self, *args, **_kwargs):
        self.calls.append(args)
        return self.responses.popleft()


class Claims:
    def __init__(self, claims):
        self.claims = deque(claims)

    def inspect(self, *_args):
        return self.claims.popleft()


def claim(*, releasable=True):
    return RaidOwnership(
        True,
        releasable,
        "Inactive Linux MD metadata currently holds this drive.",
        [{"path": "/dev/md127", "state": "inactive", "members": ["/dev/sda2"]}],
    )


def clear():
    return RaidOwnership.clear()


def payload(path="/dev/sda", *, mounted=False, serial="82LN36WZ"):
    part = {
        "name": path.rsplit("/", 1)[-1] + "2",
        "path": path + "2",
        "type": "part",
        "mountpoints": ["/media/disk"] if mounted else [None],
    }
    return {
        "blockdevices": [
            {
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "type": "disk",
                "size": 1_000_000_000,
                "model": "RAID disk",
                "serial": serial,
                "tran": "usb",
                "log-sec": 512,
                "mountpoints": [None],
                "children": [part],
            }
        ]
    }


def prepare(monkeypatch, claims, payloads):
    hardware = Hardware(demo=False)
    hardware._raid_inspector = Claims(claims)
    hardware._discovery_runner = Runner(
        [CommandResult(("lsblk",), 0, json.dumps(item), "") for item in payloads]
    )
    monkeypatch.setattr(hardware, "_swap_paths", lambda: asyncio.sleep(0, result=set()))
    monkeypatch.setattr("drivecheck.hardware.os.geteuid", lambda: 0)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    return hardware


async def test_take_control_validates_stops_and_preserves_metadata(monkeypatch):
    hardware = prepare(monkeypatch, [claim(), claim(), claim(), clear()], [payload()] * 4)
    hardware._runner = Runner([CommandResult(("mdadm",), 0, "stopped", "")])
    drive = (await hardware.discover())[0]
    assert drive.reasons == ["device_in_use"]
    assert drive.ownership["take_control_available"] is True

    result = await hardware.take_control(drive, "TAKE CONTROL 82LN36WZ")
    assert result == {
        "status": "released",
        "detail": "Inactive RAID claim released; metadata preserved.",
    }
    assert hardware._runner.calls == [("mdadm", "--stop", "/dev/md127")]


async def test_take_control_rejects_confirmation_mount_and_path_change(monkeypatch):
    hardware = prepare(monkeypatch, [claim()], [payload()])
    hardware._runner = Runner([])
    drive = (await hardware.discover())[0]
    with pytest.raises(SafetyError, match="exact drive serial confirmation"):
        await hardware.take_control(drive, "TAKE CONTROL WRONG")
    assert hardware._runner.calls == []

    hardware = prepare(monkeypatch, [claim(), claim()], [payload(mounted=True)] * 2)
    hardware._runner = Runner([])
    drive = (await hardware.discover())[0]
    assert drive.ownership["take_control_available"] is False
    with pytest.raises(SafetyError, match="safety issues"):
        await hardware.take_control(drive, "TAKE CONTROL 82LN36WZ")

    hardware = prepare(monkeypatch, [claim(), claim()], [payload(), payload("/dev/sdb")])
    hardware._runner = Runner([])
    drive = (await hardware.discover())[0]
    with pytest.raises(SafetyError, match="path changed"):
        await hardware.take_control(drive, "TAKE CONTROL 82LN36WZ")


async def test_take_control_command_failure_leaves_claim_blocked(monkeypatch):
    hardware = prepare(monkeypatch, [claim(), claim(), claim()], [payload()] * 3)
    hardware._runner = Runner([CommandResult(("mdadm",), 1, "", "device busy")])
    drive = (await hardware.discover())[0]
    with pytest.raises(SafetyError, match="mdadm could not stop /dev/md127 safely"):
        await hardware.take_control(drive, "TAKE CONTROL 82LN36WZ")
    assert hardware._runner.calls == [("mdadm", "--stop", "/dev/md127")]


async def test_take_control_is_unavailable_without_a_serial(monkeypatch):
    hardware = prepare(monkeypatch, [claim()], [payload(serial="")])
    hardware._runner = Runner([])
    drive = (await hardware.discover())[0]
    assert "missing_serial" in drive.reasons
    assert drive.ownership["take_control_available"] is False
    with pytest.raises(SafetyError, match="exact drive serial confirmation"):
        await hardware.take_control(drive, "TAKE CONTROL ")
    assert hardware._runner.calls == []


def test_take_control_capability_requires_root_lsblk_and_mdadm(monkeypatch):
    hardware = Hardware(demo=False)
    monkeypatch.setattr("drivecheck.hardware.os.geteuid", lambda: 0)
    monkeypatch.setattr("drivecheck.hardware.shutil.which", lambda name: f"/usr/bin/{name}")
    assert hardware.capabilities()["can_take_control"] is True

    monkeypatch.setattr(
        "drivecheck.hardware.shutil.which",
        lambda name: None if name == "mdadm" else f"/usr/bin/{name}",
    )
    capabilities = hardware.capabilities()
    assert capabilities["can_take_control"] is False
    assert any("mdadm is unavailable" in item for item in capabilities["limitations"])
