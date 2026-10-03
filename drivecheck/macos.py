"""Fail-closed macOS drive discovery and read-only testing."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from drivecheck.hardware import CommandError, Drive, Hardware, Progress, SafetyError, _identity


def _string(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace").replace("\x00", "").strip()
    return str(value or "").replace("\x00", "").strip()


def _whole(identifier: str) -> str:
    match = re.match(r"^(disk\d+)", identifier.removeprefix("/dev/"))
    return match.group(1) if match else identifier.removeprefix("/dev/")


def _children(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _children(child)
    elif isinstance(value, list):
        for child in value:
            yield from _children(child)


class MacHardware(Hardware):
    """Darwin adapter; destructive verification is deliberately unavailable."""

    def capabilities(self) -> dict[str, Any]:
        tools = {
            name: bool(shutil.which(name)) for name in ("diskutil", "ioreg", "smartctl", "fio")
        }
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        can_test = root and all(tools[name] for name in ("diskutil", "ioreg", "fio"))
        limitations = [
            "Destructive verification is disabled on macOS.",
            "Disk Arbitration can remount media; unmount immediately before a read scan.",
        ]
        if not root:
            limitations.append("Raw drive tests require root privileges.")
        if not tools["fio"]:
            limitations.append("fio is required for read benchmarks and surface scans.")
        if not tools["smartctl"]:
            limitations.append("smartctl is unavailable; health coverage will be incomplete.")
        return {
            "platform": "macos",
            "can_test": can_test,
            "can_verify": False,
            "can_unmount": tools["diskutil"] and tools["ioreg"],
            "can_eject": tools["diskutil"] and tools["ioreg"],
            "can_erase": False,
            "can_take_control": False,
            "tools": tools,
            "limitations": limitations,
        }

    async def erase_plan(self, drive: Drive) -> dict[str, Any]:
        detail = "Drive erasure is disabled on macOS because exclusive destructive access cannot be proved."
        return {
            "quick": self._unavailable_erase("quick_format_exfat", False, detail),
            "full": self._unavailable_erase("full_overwrite", True, detail),
        }

    async def erase(
        self,
        drive: Drive,
        profile: str,
        progress: Progress,
        *,
        recovery_dir: Path,
        expected_method: str | None = None,
    ) -> dict[str, Any]:
        del drive, profile, progress, recovery_dir, expected_method
        raise SafetyError(
            "Drive erasure is disabled on macOS because exclusive destructive access cannot be proved."
        )

    async def discover(self) -> list[Drive]:
        if self.demo:
            return [Drive(**self._DEMO_DRIVE.to_dict())]
        listing = await self._plist_command(
            "diskutil", "list", "-plist", "external", "physical", required=True
        )
        ioreg = await self._plist_command(
            "ioreg", "-a", "-r", "-c", "IOUSBHostDevice", required=True
        )
        apfs, apfs_known = await self._optional_plist("diskutil", "apfs", "list", "-plist")
        root_info, root_known = await self._optional_plist("diskutil", "info", "-plist", "/")
        if not isinstance(listing, dict):
            raise CommandError("diskutil list returned an unexpected plist")
        if not isinstance(ioreg, (dict, list)):
            raise CommandError("ioreg returned an unexpected plist")
        registry = self._registry_devices(ioreg)
        apfs_state = self._apfs_state(apfs)
        root_whole = _whole(
            _string(root_info.get("ParentWholeDisk") or root_info.get("DeviceIdentifier"))
        )
        records: list[Drive] = []
        logical_sectors: dict[str, int] = {}
        entries = listing.get("AllDisksAndPartitions", [])
        if not isinstance(entries, list):
            raise CommandError("diskutil list plist has no disk inventory")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = _string(entry.get("DeviceIdentifier"))
            if not re.fullmatch(r"disk\d+", identifier):
                continue
            info = await self._plist_command(
                "diskutil", "info", "-plist", identifier, required=True
            )
            if not isinstance(info, dict):
                raise CommandError("diskutil info returned an unexpected plist")
            bsd_names = self._entry_bsd_names(entry) | {identifier}
            media_entry_name = _string(info.get("IORegistryEntryName"))
            device_tree_location = self._device_tree_location(_string(info.get("DeviceTreePath")))
            matches = [
                device
                for device in registry
                if (
                    device["bsd_names"].intersection(bsd_names)
                    or (media_entry_name and media_entry_name in device["media_names"])
                )
                and (
                    not device_tree_location
                    or not device["location"]
                    or device_tree_location == device["location"]
                )
            ]
            serials = {device["serial"] for device in matches if device["serial"]}
            models = {device["model"] for device in matches if device["model"]}
            serial = next(iter(serials)) if len(serials) == 1 else ""
            model = (
                media_entry_name.removesuffix(" Media")
                or _string(info.get("MediaName") or info.get("DeviceName"))
                or (next(iter(models)) if len(models) == 1 else "")
                or "Unknown drive"
            )
            try:
                size = int(info.get("TotalSize") or entry.get("Size") or 0)
                logical_sector = int(info.get("DeviceBlockSize") or 0)
            except (TypeError, ValueError):
                size = logical_sector = 0
            mounts = self._entry_mounts(entry)
            store_state = apfs_state.get(identifier, {"mounted": False, "system": False})
            mounted = bool(mounts) or bool(store_state["mounted"])
            protocol = _string(info.get("BusProtocol")).lower()
            reasons: list[str] = []
            if bool(info.get("Internal")) or protocol != "usb":
                reasons.append("not_external_usb")
            if not bool(info.get("Whole", True)) or bool(
                info.get("VirtualOrPhysical") == "Virtual"
            ):
                reasons.append("not_physical_whole_disk")
            if mounted:
                reasons.append("mounted")
            system = (
                "/" in mounts
                or "/System/Volumes/Data" in mounts
                or identifier == root_whole
                or bool(store_state["system"])
            )
            if system:
                reasons.append("system_drive")
            if not root_known:
                reasons.append("system_state_unknown")
            if not apfs_known:
                reasons.append("apfs_state_unknown")
            if not serial:
                reasons.append("ambiguous_serial" if len(serials) > 1 else "missing_serial")
            if size <= 0:
                reasons.append("invalid_size")
            if logical_sector <= 0 or size % logical_sector:
                reasons.append("invalid_logical_sector")
            ident = _identity(model, serial, size)
            if logical_sector > 0:
                logical_sectors[ident] = logical_sector
            records.append(
                Drive(
                    id=ident[:16],
                    path=f"/dev/r{identifier}",
                    model=model,
                    serial=serial,
                    size_bytes=size,
                    transport="usb" if protocol == "usb" else protocol or "unknown",
                    eligible=not reasons,
                    reasons=reasons,
                    identity=ident,
                    mounted=mounted,
                )
            )
        identity_counts: dict[str, int] = {}
        serial_counts: dict[str, int] = {}
        for drive in records:
            identity_counts[drive.identity] = identity_counts.get(drive.identity, 0) + 1
            if drive.serial:
                serial_counts[drive.serial] = serial_counts.get(drive.serial, 0) + 1
        for drive in records:
            if identity_counts[drive.identity] > 1 or (
                drive.serial and serial_counts[drive.serial] > 1
            ):
                drive.reasons.append("duplicate_identity")
                drive.eligible = False
        self._logical_sector_bytes = logical_sectors
        return records

    async def validate(self, drive: Drive, destructive: bool = False) -> Drive:
        if destructive:
            raise SafetyError("destructive verification is disabled on macOS")
        return await super().validate(drive, destructive=False)

    async def surface(
        self, drive: Drive, progress: Progress, destructive: bool = False
    ) -> dict[str, Any]:
        if destructive:
            return {
                "status": "unsupported",
                "detail": "Destructive verification is disabled on macOS",
                "raw": {},
            }
        return await super().surface(drive, progress, destructive=False)

    async def unmount(self, drive: Drive) -> dict[str, str]:
        if self.demo:
            return {"status": "unmounted", "detail": "Demo drive unmounted"}
        try:
            current = await self._fresh_for_management(drive, allow_mounted=True)
        except (SafetyError, CommandError) as exc:
            return {"status": "failed", "detail": str(exc)}
        if not current.mounted:
            return {"status": "unmounted", "detail": "Drive is already unmounted"}
        result = await self._runner.run(
            "diskutil", "unmountDisk", self._block_path(current), timeout=90
        )
        if result.returncode:
            return {
                "status": "failed",
                "detail": result.stderr.strip() or "diskutil could not unmount the drive",
            }
        try:
            refreshed = await self._fresh_for_management(current, allow_mounted=True)
        except (SafetyError, CommandError) as exc:
            return {"status": "failed", "detail": f"Unmount verification failed: {exc}"}
        if refreshed.mounted:
            return {"status": "failed", "detail": "Drive remains mounted"}
        return {"status": "unmounted", "detail": "All drive volumes were unmounted"}

    async def eject(self, drive: Drive) -> dict[str, str]:
        if self.demo:
            return {"status": "ejected", "detail": "Demo drive ejected"}
        try:
            current = await self._fresh_for_management(drive, allow_mounted=False)
        except (SafetyError, CommandError) as exc:
            return {"status": "failed", "detail": str(exc)}
        result = await self._runner.run("diskutil", "eject", self._block_path(current), timeout=90)
        if result.returncode:
            return {
                "status": "failed",
                "detail": result.stderr.strip() or "diskutil could not eject the drive",
            }
        try:
            remaining = await self.discover()
        except (SafetyError, CommandError):
            return {"status": "failed", "detail": "Eject succeeded but could not be verified"}
        if any(candidate.identity == current.identity for candidate in remaining):
            return {"status": "failed", "detail": "Drive remains visible after eject"}
        return {"status": "ejected", "detail": "Drive ejected and is safe to remove"}

    async def _fresh_for_management(self, drive: Drive, *, allow_mounted: bool) -> Drive:
        matches = [
            candidate for candidate in await self.discover() if candidate.identity == drive.identity
        ]
        if len(matches) != 1:
            raise SafetyError("drive identity is missing or no longer unique")
        current = matches[0]
        if current.path != drive.path or current.serial != drive.serial:
            raise SafetyError("drive identity or path changed")
        allowed = {"mounted"} if allow_mounted else set()
        unsafe = [reason for reason in current.reasons if reason not in allowed]
        if unsafe or (current.mounted and not allow_mounted):
            raise SafetyError("drive is unsafe: " + ", ".join(unsafe or ["mounted"]))
        return current

    async def _plist_command(self, *args: str, required: bool) -> Any:
        result = await self._discovery_runner.run(*args, timeout=30)
        if result.returncode:
            if required:
                raise CommandError(f"{args[0]} inventory command failed")
            return {}
        try:
            parsed = plistlib.loads(result.stdout.encode())
        except (plistlib.InvalidFileException, ValueError) as exc:
            raise CommandError(f"{args[0]} returned an invalid plist") from exc
        if not isinstance(parsed, (dict, list)):
            raise CommandError(f"{args[0]} returned an unexpected plist")
        return parsed

    async def _optional_plist(self, *args: str) -> tuple[dict[str, Any], bool]:
        try:
            result = await self._discovery_runner.run(*args, timeout=30)
            if result.returncode:
                return {}, False
            parsed = plistlib.loads(result.stdout.encode())
            return (parsed, True) if isinstance(parsed, dict) else ({}, False)
        except (CommandError, plistlib.InvalidFileException, ValueError):
            return {}, False

    @staticmethod
    def _registry_devices(payload: Any) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        roots = payload if isinstance(payload, list) else [payload]
        for node in roots:
            if not isinstance(node, dict):
                continue
            characteristics = node.get("Device Characteristics", {})
            if not isinstance(characteristics, dict):
                characteristics = {}
            bsd_names = {
                _string(descendant.get("BSD Name"))
                for descendant in _children(node)
                if _string(descendant.get("BSD Name"))
            }
            media_names = {
                _string(descendant.get("IORegistryEntryName"))
                for descendant in _children(node)
                if _string(descendant.get("IOObjectClass")) == "IOMedia"
                and _string(descendant.get("IORegistryEntryName"))
            }
            if not bsd_names and not media_names:
                continue
            serial = _string(
                node.get("USB Serial Number")
                or node.get("kUSBSerialNumberString")
                or characteristics.get("Serial Number")
                or characteristics.get("USB Serial Number")
                or node.get("Serial Number")
            )
            product = _string(
                node.get("USB Product Name")
                or node.get("kUSBProductString")
                or characteristics.get("Product Name")
                or characteristics.get("Product")
                or node.get("Product Name")
            )
            vendor = _string(
                node.get("USB Vendor Name")
                or node.get("kUSBVendorString")
                or characteristics.get("Vendor Name")
                or characteristics.get("Vendor")
            )
            model = " ".join(part for part in (vendor, product) if part).strip()
            devices.append(
                {
                    "bsd_names": bsd_names,
                    "media_names": media_names,
                    "serial": serial,
                    "model": model,
                    "location": _string(node.get("IORegistryEntryLocation")),
                }
            )
        return devices

    @staticmethod
    def _device_tree_location(path: str) -> str:
        match = re.search(r"@([0-9a-fA-F]+)$", path)
        return match.group(1) if match else ""

    @staticmethod
    def _entry_bsd_names(entry: dict[str, Any]) -> set[str]:
        return {
            _string(node.get("DeviceIdentifier"))
            for node in _children(entry)
            if _string(node.get("DeviceIdentifier"))
        }

    @staticmethod
    def _entry_mounts(entry: dict[str, Any]) -> set[str]:
        return {
            _string(node.get("MountPoint"))
            for node in _children(entry)
            if _string(node.get("MountPoint"))
        }

    @staticmethod
    def _apfs_state(payload: dict[str, Any]) -> dict[str, dict[str, bool]]:
        state: dict[str, dict[str, bool]] = {}
        containers = payload.get("Containers", []) if isinstance(payload, dict) else []
        for container in containers if isinstance(containers, list) else []:
            if not isinstance(container, dict):
                continue
            mounts = {
                _string(node.get("MountPoint"))
                for node in _children(container.get("Volumes", []))
                if _string(node.get("MountPoint"))
            }
            system = bool(mounts.intersection({"/", "/System/Volumes/Data"}))
            stores = container.get("PhysicalStores", [])
            for store in stores if isinstance(stores, list) else []:
                if not isinstance(store, dict):
                    continue
                identifier = _whole(_string(store.get("DeviceIdentifier")))
                if identifier:
                    current = state.setdefault(identifier, {"mounted": False, "system": False})
                    current["mounted"] = current["mounted"] or bool(mounts)
                    current["system"] = current["system"] or system
        return state

    @staticmethod
    def _block_path(drive: Drive) -> str:
        return drive.path.replace("/dev/rdisk", "/dev/disk", 1)

    @staticmethod
    def _smart_path(drive: Drive) -> str:
        return MacHardware._block_path(drive)

    @staticmethod
    def _fio_engine() -> str:
        return "posixaio"

    @staticmethod
    def _pin_device(path: str) -> int:
        try:
            device_stat = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise SafetyError("raw drive path is unavailable") from exc
        if not stat.S_ISCHR(device_stat.st_mode):
            raise SafetyError("macOS tests require a raw character device")
        return device_stat.st_rdev

    @staticmethod
    @contextmanager
    def _exclusive_claim(path: str):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise SafetyError("raw drive path could not be opened read-only") from exc
        try:
            claimed_stat = os.fstat(descriptor)
            if not stat.S_ISCHR(claimed_stat.st_mode):
                raise SafetyError("claimed path is not a raw character device")
            yield descriptor
        finally:
            os.close(descriptor)


__all__ = ["MacHardware"]
