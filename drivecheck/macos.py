"""Fail-closed macOS drive discovery, testing, and manual erasure."""

from __future__ import annotations

import hashlib
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
    """Darwin adapter using Disk Arbitration-aware system tools."""

    def capabilities(self) -> dict[str, Any]:
        tools = {
            name: bool(shutil.which(name)) for name in ("diskutil", "ioreg", "smartctl", "fio")
        }
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        can_test = root and all(tools[name] for name in ("diskutil", "ioreg", "fio"))
        can_erase = root and tools["diskutil"] and tools["ioreg"]
        limitations = [
            "Disk Arbitration can remount media; DriveCheck monitors mount state during raw I/O.",
            "ATA firmware secure erase is unavailable through the supported macOS tools.",
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
            "can_verify": can_test,
            "can_unmount": tools["diskutil"] and tools["ioreg"],
            "can_eject": tools["diskutil"] and tools["ioreg"],
            "can_erase": can_erase,
            "can_take_control": False,
            "tools": tools,
            "limitations": limitations,
        }

    async def erase_plan(self, drive: Drive) -> dict[str, Any]:
        if self.demo:
            return await super().erase_plan(drive)
        try:
            current = await self.validate(drive, destructive=True)
        except (SafetyError, CommandError) as exc:
            detail = f"Drive is not safely erasable: {exc}"
            return self._unavailable_erase_plan(detail)
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return self._unavailable_erase_plan("Root privileges are required to erase a drive.")
        if not shutil.which("diskutil"):
            return self._unavailable_erase_plan("diskutil is required to erase a drive on macOS.")

        try:
            targets = await self._format_targets(current)
        except (SafetyError, CommandError) as exc:
            quick = self._unavailable_erase(
                "quick_format_exfat", False, f"Quick-format targets are unavailable: {exc}"
            )
            quick["targets"] = []
        else:
            if targets:
                quick = {
                    "available": True,
                    "method": "quick_format_exfat",
                    "secure": False,
                    "detail": "Quick-formats one selected existing partition as exFAT without replacing the partition map.",
                    "estimated_minutes": 2,
                    "targets": targets,
                }
            else:
                quick = self._unavailable_erase(
                    "quick_format_exfat",
                    False,
                    "Quick format requires an existing ordinary, non-APFS, non-RAID partition. Use Initialize/reset disk to create a new layout.",
                )
                quick["targets"] = []
        full_available = bool(shutil.which("fio"))
        return {
            "quick": quick,
            "initialize": {
                "available": True,
                "method": "initialize_exfat",
                "secure": False,
                "detail": "Replaces the partition map with a GUID layout and a new exFAT volume; old data is not securely overwritten.",
                "estimated_minutes": 2,
            },
            "secure": self._unavailable_erase(
                "ata_secure_erase",
                True,
                "ATA firmware secure erase is unavailable through the supported macOS tools.",
            ),
            "full": {
                "available": full_available,
                "method": "full_overwrite",
                "secure": True,
                "detail": (
                    "Writes and reads back the entire drive with SHA-256 verification."
                    if full_available
                    else "fio is required for a full overwrite."
                ),
            },
        }

    async def erase(
        self,
        drive: Drive,
        profile: str,
        progress: Progress,
        *,
        recovery_dir: Path,
        expected_method: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        if self.demo:
            return await super().erase(
                drive,
                profile,
                progress,
                recovery_dir=recovery_dir,
                expected_method=expected_method,
                target_id=target_id,
            )
        del recovery_dir
        choices = {
            "quick_erase": "quick",
            "initialize_disk": "initialize",
            "secure_erase": "secure",
            "full_erase": "full",
        }
        if profile not in choices:
            raise ValueError("unknown erase profile")
        current = await self.validate(drive, destructive=True)
        choice = (await self.erase_plan(current))[choices[profile]]
        if not choice["available"]:
            raise SafetyError(choice["detail"])
        if expected_method is not None and expected_method != choice["method"]:
            raise SafetyError("erase method changed; review and confirm the new plan")
        if profile == "quick_erase":
            return await self._quick_format_exfat(current, progress, target_id)
        if profile == "initialize_disk":
            return await self._initialize_exfat(current, progress)
        result = await super().surface(current, progress, destructive=True)
        return {**result, "method": "full_overwrite"}

    @staticmethod
    def _unavailable_erase_plan(detail: str) -> dict[str, Any]:
        return {
            "quick": Hardware._unavailable_erase("quick_format_exfat", False, detail),
            "initialize": Hardware._unavailable_erase("initialize_exfat", False, detail),
            "secure": Hardware._unavailable_erase("ata_secure_erase", True, detail),
            "full": Hardware._unavailable_erase("full_overwrite", True, detail),
        }

    async def _format_targets(self, drive: Drive) -> list[dict[str, Any]]:
        records = await self._partition_records(drive)
        protected_content = {
            "efi",
            "apple_boot",
            "apple_apfs",
            "apple_apfs_isc",
            "apple_apfs_recovery",
            "apple_apfs_vm",
            "apple_raid",
            "apple_raid_offline",
        }
        return [
            {
                "id": record["id"],
                "path": record["path"],
                "size_bytes": record["size_bytes"],
                "start_offset": record["start_offset"],
                "partuuid": record["partuuid"],
                "filesystem": record["content"] or None,
                "label": record["label"] or None,
            }
            for record in records
            if record["content"].lower() not in protected_content
        ]

    async def _partition_topology(self, drive: Drive) -> tuple[tuple[Any, ...], ...]:
        records = await self._partition_records(drive)
        return tuple(
            (
                record["path"],
                record["size_bytes"],
                record["start_offset"],
                record["partuuid"],
            )
            for record in records
        )

    async def _partition_records(self, drive: Drive) -> list[dict[str, Any]]:
        payload = await self._plist_command(
            "diskutil", "list", "-plist", self._block_path(drive), required=True
        )
        if not isinstance(payload, dict):
            raise CommandError("diskutil returned an unexpected partition map")
        entries = payload.get("AllDisksAndPartitions", [])
        if not isinstance(entries, list) or len(entries) != 1:
            raise SafetyError("partition map did not uniquely match the selected drive")
        entry = entries[0]
        if not isinstance(entry, dict) or _string(entry.get("DeviceIdentifier")) != _whole(
            self._block_path(drive)
        ):
            raise SafetyError("partition map did not match the selected drive")
        partitions = entry.get("Partitions", [])
        if not isinstance(partitions, list):
            raise CommandError("diskutil returned an invalid partition map")
        records: list[dict[str, Any]] = []
        whole = _whole(self._block_path(drive))
        for partition in partitions:
            if not isinstance(partition, dict):
                raise CommandError("diskutil returned an invalid partition entry")
            identifier = _string(partition.get("DeviceIdentifier"))
            content = _string(partition.get("Content"))
            if not re.fullmatch(rf"{re.escape(whole)}s\d+", identifier):
                raise SafetyError("partition identifier did not belong to the selected drive")
            path = f"/dev/{identifier}"
            info = await self._plist_command("diskutil", "info", "-plist", path, required=True)
            if not isinstance(info, dict) or _whole(
                _string(info.get("ParentWholeDisk") or info.get("DeviceIdentifier"))
            ) != whole:
                raise SafetyError("partition information did not match the selected drive")
            try:
                size = int(partition.get("Size") or 0)
                start_offset = int(
                    info.get("PartitionMapPartitionOffset")
                    or partition.get("PartitionMapPartitionOffset")
                )
            except (TypeError, ValueError) as exc:
                raise SafetyError("partition boundaries were invalid") from exc
            if size <= 0 or start_offset < 0:
                raise SafetyError("partition boundaries were invalid")
            partuuid = _string(info.get("DiskUUID") or info.get("PartitionUUID"))
            target_id = hashlib.sha256(
                f"{identifier}\0{size}\0{start_offset}\0{partuuid}".encode()
            ).hexdigest()[:24]
            records.append(
                {
                    "id": target_id,
                    "path": path,
                    "size_bytes": size,
                    "start_offset": start_offset,
                    "partuuid": partuuid or None,
                    "content": content,
                    "label": _string(partition.get("VolumeName")),
                }
            )
        return sorted(records, key=lambda item: item["path"])

    async def _quick_format_exfat(
        self, drive: Drive, progress: Progress, target_id: str | None
    ) -> dict[str, Any]:
        current = await self.validate(drive, destructive=True)
        device_number = self._pin_device(current.path)
        targets = await self._format_targets(current)
        target = next((item for item in targets if item["id"] == target_id), None)
        if target is None:
            raise SafetyError("the selected quick-format volume is missing or changed")
        topology = await self._partition_topology(current)
        target_device_number = self._pin_device(target["path"])
        await self._mac_destructive_revalidate(current, device_number)
        if self._pin_device(target["path"]) != target_device_number:
            raise SafetyError("quick-format volume device changed")
        await progress(5, f"Quick-formatting {target['path']} as exFAT")
        refreshed_targets = await self._format_targets(current)
        refreshed = next((item for item in refreshed_targets if item["id"] == target_id), None)
        if refreshed != target or await self._partition_topology(current) != topology:
            raise SafetyError("partition map changed before quick format")
        await self._mac_destructive_revalidate(current, device_number)
        result = await self._runner.run(
            "diskutil", "eraseVolume", "ExFAT", "DRIVECHECK", target["path"], timeout=60 * 60
        )
        if result.returncode:
            raise CommandError(
                result.stderr.strip() or "diskutil could not quick-format the selected volume"
            )
        info = await self._plist_command("diskutil", "info", "-plist", target["path"], required=True)
        if not isinstance(info, dict) or _whole(
            _string(info.get("ParentWholeDisk") or info.get("DeviceIdentifier"))
        ) != _whole(self._block_path(current)):
            raise SafetyError("formatted volume no longer belongs to the selected drive")
        filesystem = _string(
            info.get("FilesystemType") or info.get("FileSystemPersonality")
        ).lower()
        if "exfat" not in filesystem:
            raise CommandError("the selected exFAT volume could not be verified")
        if await self._partition_topology(current) != topology:
            raise SafetyError("partition map changed during quick format")
        await self._unmount_after_format(current)
        await self._mac_destructive_revalidate(current, device_number)
        await progress(100, f"Quick exFAT format completed on {target['path']}")
        return {
            "status": "passed",
            "method": "quick_format_exfat",
            "target": target,
            "detail": f"{target['path']} was quick-formatted as exFAT; the partition map and other volumes were preserved. Old file contents may remain recoverable.",
        }

    async def _initialize_exfat(self, drive: Drive, progress: Progress) -> dict[str, Any]:
        current = await self.validate(drive, destructive=True)
        device_number = self._pin_device(current.path)
        await self._mac_destructive_revalidate(current, device_number)
        await progress(5, "Creating a new GUID partition map and exFAT volume")
        await self._mac_destructive_revalidate(current, device_number)
        result = await self._runner.run(
            "diskutil",
            "eraseDisk",
            "ExFAT",
            "DRIVECHECK",
            "GPT",
            self._block_path(current),
            timeout=60 * 60,
        )
        if result.returncode:
            raise CommandError(result.stderr.strip() or "diskutil could not initialize the drive")
        await self._unmount_after_format(current)
        await self._mac_destructive_revalidate(current, device_number)
        targets = await self._format_targets(current)
        if len(targets) != 1:
            raise SafetyError("the new exFAT volume could not be identified uniquely")
        info = await self._plist_command(
            "diskutil", "info", "-plist", targets[0]["path"], required=True
        )
        filesystem = _string(
            info.get("FilesystemType") or info.get("FileSystemPersonality")
        ).lower() if isinstance(info, dict) else ""
        if "exfat" not in filesystem:
            raise CommandError("the new exFAT filesystem could not be verified")
        await progress(100, "Disk initialization completed")
        return {
            "status": "passed",
            "method": "initialize_exfat",
            "detail": "The partition map was replaced with a GUID layout and one quick-formatted exFAT data volume; old data was not securely overwritten.",
        }

    async def _unmount_after_format(self, drive: Drive) -> None:
        result = await self._runner.run(
            "diskutil", "unmountDisk", self._block_path(drive), timeout=90
        )
        if result.returncode:
            raise SafetyError(
                result.stderr.strip() or "the newly formatted drive could not be unmounted"
            )

    async def _mac_destructive_revalidate(self, drive: Drive, device_number: int) -> Drive:
        current = await self.validate(drive, destructive=True)
        if self._pin_device(current.path) != device_number:
            raise SafetyError("raw drive device number changed")
        return current

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
        return await super().validate(drive, destructive=destructive)

    async def surface(
        self, drive: Drive, progress: Progress, destructive: bool = False
    ) -> dict[str, Any]:
        return await super().surface(drive, progress, destructive=destructive)

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
    def _fio_direct_args() -> tuple[str, ...]:
        # Darwin raw character devices are already unbuffered. fio's direct
        # option maps to Linux-style direct-I/O behavior that is not portable
        # across the macOS engines supported by DriveCheck.
        return ()

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
