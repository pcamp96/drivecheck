"""Linux drive discovery and test execution for DriveCheck.

The public :class:`Hardware` API intentionally treats the operating system as
hostile state.  A drive is rediscovered before each operation and destructive
work is allowed only when a unique, serial-backed identity is still present.
Demo mode is completely synthetic and never invokes a host command.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drivecheck.erase import RecoveryJournal, parse_hdparm_security
from drivecheck.raid import RaidInspector, RaidOwnership

Progress = Callable[[float | None, str], Awaitable[None]]


class SafetyError(RuntimeError):
    """The requested drive operation cannot be proved safe."""


class CommandError(RuntimeError):
    """A child command could not be executed or completed."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(slots=True)
class Drive:
    id: str
    path: str
    model: str
    serial: str
    size_bytes: int
    transport: str
    eligible: bool
    reasons: list[str]
    identity: str
    mounted: bool
    ownership: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False


class CommandRunner:
    """Run one bounded child process and terminate its whole process group."""

    def __init__(self, output_limit: int = 2_000_000) -> None:
        self.output_limit = output_limit
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def run(
        self,
        *args: str,
        timeout: float,
        stdout_chunk: Callable[[str], Awaitable[None]] | None = None,
        stderr_line: Callable[[str], Awaitable[None]] | None = None,
        sensitive_args: set[int] | None = None,
    ) -> CommandResult:
        async with self._lock:
            if self._process is not None:
                raise CommandError("another hardware command is already running")
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise CommandError(f"could not start {args[0]}: {exc}") from exc
            self._process = process
            callback_failure: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            stdout_task = asyncio.create_task(
                self._read(process.stdout, stdout_chunk, callback_failure)
            )
            stderr_task = asyncio.create_task(
                self._read(process.stderr, stderr_line, callback_failure)
            )
            process_task = asyncio.create_task(process.wait())
            try:
                done, _ = await asyncio.wait(
                    {process_task, callback_failure},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError
                if callback_failure in done:
                    error = callback_failure.exception()
                    await self._terminate(process)
                    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                    await process_task
                    if error is not None:
                        raise error
                await process_task
            except TimeoutError as exc:
                await self._terminate(process)
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                process_task.cancel()
                raise CommandError(f"command timed out after {timeout:g}s") from exc
            except asyncio.CancelledError:
                await self._terminate(process)
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                process_task.cancel()
                raise
            finally:
                self._process = None
                if not callback_failure.done():
                    callback_failure.cancel()
                elif not callback_failure.cancelled():
                    callback_failure.exception()
            (stdout, out_cut), (stderr, err_cut) = await asyncio.gather(stdout_task, stderr_task)
            reported_args = tuple(
                "[REDACTED]" if sensitive_args and index in sensitive_args else value
                for index, value in enumerate(args)
            )
            return CommandResult(
                reported_args, process.returncode or 0, stdout, stderr, out_cut or err_cut
            )

    async def cancel(self) -> None:
        process = self._process
        if process is not None:
            await self._terminate(process)

    async def _read(
        self,
        stream: asyncio.StreamReader | None,
        callback: Callable[[str], Awaitable[None]] | None = None,
        callback_failure: asyncio.Future[None] | None = None,
    ) -> tuple[str, bool]:
        if stream is None:
            return "", False
        chunks: list[bytes] = []
        length = 0
        truncated = False
        callback_error: Exception | None = None
        while True:
            # Fixed-size reads keep a command that emits one enormous line from
            # defeating the output cap (or StreamReader's line limit).
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            if callback and callback_error is None:
                try:
                    await callback(chunk.decode(errors="replace"))
                except Exception as exc:  # drain the pipe before surfacing callback failure
                    callback_error = exc
                    if callback_failure is not None and not callback_failure.done():
                        callback_failure.set_exception(exc)
            remaining = self.output_limit - length
            if remaining > 0:
                kept = chunk[:remaining]
                chunks.append(kept)
                length += len(kept)
            if len(chunk) > remaining:
                truncated = True
        if callback_error is not None:
            raise callback_error
        return b"".join(chunks).decode(errors="replace"), truncated

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mountpoints(node: dict[str, Any]) -> set[str]:
    points: set[str] = set()
    raw = node.get("mountpoints")
    if isinstance(raw, list):
        points.update(_text(value) for value in raw if _text(value))
    elif _text(raw):
        points.add(_text(raw))
    if _text(node.get("mountpoint")):
        points.add(_text(node["mountpoint"]))
    for child in node.get("children") or []:
        points.update(_mountpoints(child))
    return points


def _identity(model: str, serial: str, size_bytes: int) -> str:
    material = f"{serial}\0{model}\0{size_bytes}".encode()
    return hashlib.sha256(material).hexdigest()


class Hardware:
    """Discover and exercise one externally attached drive at a time."""

    _DEMO_DRIVE = Drive(
        id="demo-wd40efzx",
        path="/dev/drivecheck-demo0",
        model="WDC WD40EFZX-68AWUN0",
        serial="DEMO-WD40-2026",
        size_bytes=4_000_787_030_016,
        transport="usb",
        eligible=True,
        reasons=[],
        identity=_identity("WDC WD40EFZX-68AWUN0", "DEMO-WD40-2026", 4_000_787_030_016),
        mounted=False,
    )

    def __init__(self, demo: bool) -> None:
        self.demo = demo
        self._runner = CommandRunner()
        # Discovery must remain available while the operation runner is occupied
        # by a multi-hour fio or SMART command.
        self._discovery_runner = CommandRunner(output_limit=1_000_000)
        self._probe_runner = CommandRunner(output_limit=1_000_000)
        self._cancel_requested = False
        self._self_test_drive: Drive | None = None
        self.self_test_poll_seconds = 30.0
        self.self_test_timeout_seconds = 48 * 60 * 60.0
        self.short_self_test_timeout_cap_seconds = 30 * 60.0
        self.io_safety_poll_seconds = 3.0
        self.demo_step_seconds = 0.0
        self.firmware_erase_active = False
        self._logical_sector_bytes: dict[str, int] = {}
        self._raid_inspector = RaidInspector()

    def capabilities(self) -> dict[str, Any]:
        tools = {
            name: bool(shutil.which(name))
            for name in (
                "lsblk",
                "smartctl",
                "fio",
                "udisksctl",
                "mdadm",
                "hdparm",
                "wipefs",
                "sgdisk",
                "mkfs.exfat",
                "partprobe",
                "blkid",
            )
        }
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        can_test = root and tools["lsblk"] and tools["fio"]
        can_take_control = not self.demo and root and tools["lsblk"] and tools["mdadm"]
        format_tools = all(
            tools[name] for name in ("wipefs", "sgdisk", "mkfs.exfat", "partprobe", "blkid")
        )
        can_erase = (
            not self.demo
            and root
            and tools["lsblk"]
            and (tools["fio"] or tools["hdparm"] or format_tools)
        )
        limitations: list[str] = []
        if not root:
            limitations.append("Raw drive tests require root privileges.")
        if not tools["fio"]:
            limitations.append("fio is required for read benchmarks and surface scans.")
        if not tools["smartctl"]:
            limitations.append("smartctl is unavailable; health coverage will be incomplete.")
        if not tools["udisksctl"]:
            limitations.append("udisksctl is unavailable; USB power-off is disabled.")
        if not tools["mdadm"]:
            limitations.append("mdadm is unavailable; inactive MD claims cannot be released.")
        return {
            "platform": "linux",
            "can_test": can_test,
            "can_verify": can_test,
            "can_unmount": False,
            "can_eject": tools["udisksctl"],
            "can_take_control": can_take_control,
            "can_erase": can_erase,
            "tools": tools,
            "limitations": limitations,
        }

    async def discover(self) -> list[Drive]:
        if self.demo:
            return [Drive(**self._DEMO_DRIVE.to_dict())]
        result = await self._discovery_runner.run(
            "lsblk",
            "--json",
            "--bytes",
            "--output",
            "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINTS,PKNAME,RM,HOTPLUG,LOG-SEC",
            timeout=15,
        )
        if result.returncode:
            raise CommandError(
                f"lsblk failed: {result.stderr.strip()}", returncode=result.returncode
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CommandError("lsblk returned invalid JSON") from exc
        nodes = payload.get("blockdevices")
        if not isinstance(nodes, list):
            raise CommandError("lsblk JSON has no block device list")

        swaps = await self._swap_paths()
        records: list[tuple[dict[str, Any], Drive, RaidOwnership]] = []
        logical_sectors: dict[str, int] = {}
        for node in nodes:
            if _text(node.get("type")) != "disk":
                continue
            path = _text(node.get("path")) or f"/dev/{_text(node.get('name'))}"
            model = _text(node.get("model")) or "Unknown drive"
            serial = _text(node.get("serial"))
            transport = _text(node.get("tran")).lower() or "unknown"
            try:
                size = int(node.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            mounts = _mountpoints(node)
            child_paths = self._node_paths(node)
            partition_paths = self._partition_paths(node)
            stacked_paths = self._stacked_paths(node)
            has_swap = bool(swaps.intersection(child_paths)) or "[SWAP]" in mounts
            mounted = bool(mounts) or has_swap
            reasons: list[str] = []
            if transport != "usb":
                reasons.append("not_external_usb")
            if mounts:
                reasons.append("mounted")
            if has_swap:
                reasons.append("swap_in_use")
            if mounts.intersection({"/", "/boot", "/boot/firmware"}):
                reasons.append("system_drive")
            if not serial:
                reasons.append("missing_serial")
            if size <= 0:
                reasons.append("invalid_size")
            try:
                logical_sector = int(node.get("log-sec") or 0)
            except (TypeError, ValueError):
                logical_sector = 0
            if logical_sector <= 0 or size % logical_sector:
                reasons.append("invalid_logical_sector")
            ownership = self._raid_inspector.inspect(path, partition_paths, stacked_paths)
            if ownership.blocked:
                reasons.append("device_in_use")
            ident = _identity(model, serial, size)
            if logical_sector > 0:
                logical_sectors[ident] = logical_sector
            records.append(
                (
                    node,
                    Drive(
                        id=ident[:16],
                        path=path,
                        model=model,
                        serial=serial,
                        size_bytes=size,
                        transport=transport,
                        eligible=not reasons,
                        reasons=reasons,
                        identity=ident,
                        mounted=mounted,
                    ),
                    ownership,
                )
            )
        identity_counts: dict[str, int] = {}
        serial_counts: dict[str, int] = {}
        for _, drive, _ in records:
            identity_counts[drive.identity] = identity_counts.get(drive.identity, 0) + 1
            if drive.serial:
                serial_counts[drive.serial] = serial_counts.get(drive.serial, 0) + 1
        drives: list[Drive] = []
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        mdadm = bool(shutil.which("mdadm"))
        for _, drive, ownership in records:
            if identity_counts[drive.identity] > 1 or (
                drive.serial and serial_counts[drive.serial] > 1
            ):
                drive.reasons.append("duplicate_identity")
                drive.eligible = False
            if ownership.blocked:
                other_reasons = [reason for reason in drive.reasons if reason != "device_in_use"]
                available = ownership.releasable and not other_reasons and root and mdadm
                detail = ownership.detail
                if ownership.releasable and other_reasons:
                    detail = (
                        "Take control is unavailable until other drive safety issues are resolved."
                    )
                elif ownership.releasable and not root:
                    detail = "Root privileges are required to release the inactive MD claim."
                elif ownership.releasable and not mdadm:
                    detail = "mdadm is required to release the inactive MD claim."
                drive.ownership = {
                    "take_control_available": available,
                    "detail": detail,
                    "arrays": ownership.arrays,
                }
            drives.append(drive)
        self._logical_sector_bytes = logical_sectors
        return drives

    async def _swap_paths(self) -> set[str]:
        try:
            with open("/proc/swaps", encoding="utf-8") as swaps_file:
                lines = swaps_file.read().splitlines()
        except OSError as exc:
            raise SafetyError("cannot verify active swap devices") from exc
        return {line.split()[0] for line in lines[1:] if line.split()}

    @classmethod
    def _node_paths(cls, node: dict[str, Any]) -> set[str]:
        path = _text(node.get("path")) or f"/dev/{_text(node.get('name'))}"
        paths = {path}
        for child in node.get("children") or []:
            paths.update(cls._node_paths(child))
        return paths

    @classmethod
    def _partition_paths(cls, node: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for child in node.get("children") or []:
            if _text(child.get("type")) == "part":
                paths.add(_text(child.get("path")) or f"/dev/{_text(child.get('name'))}")
            paths.update(cls._partition_paths(child))
        return paths

    @classmethod
    def _stacked_paths(cls, node: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for child in node.get("children") or []:
            if _text(child.get("type")) not in {"", "part"}:
                paths.add(_text(child.get("path")) or f"/dev/{_text(child.get('name'))}")
            paths.update(cls._stacked_paths(child))
        return paths

    async def validate(self, drive: Drive, destructive: bool = False) -> Drive:
        matches = [
            current for current in await self.discover() if current.identity == drive.identity
        ]
        if len(matches) != 1:
            raise SafetyError("drive identity is missing or no longer unique")
        current = matches[0]
        if current.path != drive.path:
            raise SafetyError("drive path changed")
        if current.serial != drive.serial or current.size_bytes != drive.size_bytes:
            raise SafetyError("drive identity changed")
        if not current.eligible:
            raise SafetyError("drive is unsafe: " + ", ".join(current.reasons))
        if destructive and (not current.serial or current.mounted):
            raise SafetyError("destructive verification requires a unique unmounted serial")
        return current

    async def take_control(self, drive: Drive, confirmation: str) -> dict[str, str]:
        if self.demo:
            raise SafetyError("demo drives have no ownership claims")
        if confirmation != f"TAKE CONTROL {drive.serial}" or not drive.serial:
            raise SafetyError("exact drive serial confirmation is required")
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise SafetyError("root privileges are required")
        if not shutil.which("mdadm"):
            raise SafetyError("mdadm is unavailable")
        try:
            current = await self._takeover_candidate(drive)
            arrays = [item["path"] for item in current.ownership["arrays"]]
            for index, array_path in enumerate(arrays):
                current = await self._takeover_candidate(drive)
                current_arrays = {item["path"] for item in current.ownership["arrays"]}
                if current_arrays != set(arrays[index:]):
                    raise SafetyError("inactive MD ownership changed before release")
                result = await self._runner.run("mdadm", "--stop", array_path, timeout=60)
                if result.returncode:
                    raise SafetyError(f"mdadm could not stop {array_path} safely")
            matches = await self._takeover_matches(drive)
            if len(matches) != 1:
                raise SafetyError("drive identity is missing or no longer unique after release")
            released = matches[0]
            if (
                released.path != drive.path
                or released.serial != drive.serial
                or released.size_bytes != drive.size_bytes
                or not released.eligible
                or released.ownership is not None
            ):
                raise SafetyError("drive did not become safely eligible after release")
        except CommandError as exc:
            raise SafetyError("drive ownership could not be revalidated safely") from exc
        return {
            "status": "released",
            "detail": "Inactive RAID claim released; metadata preserved.",
        }

    async def _takeover_candidate(self, drive: Drive) -> Drive:
        matches = await self._takeover_matches(drive)
        if len(matches) != 1:
            raise SafetyError("drive identity is missing or no longer unique")
        current = matches[0]
        if current.path != drive.path:
            raise SafetyError("drive path changed")
        if current.serial != drive.serial or current.size_bytes != drive.size_bytes:
            raise SafetyError("drive identity changed")
        if set(current.reasons) != {"device_in_use"}:
            raise SafetyError("drive has safety issues beyond an inactive MD ownership claim")
        ownership = current.ownership or {}
        if not ownership.get("take_control_available") or not ownership.get("arrays"):
            raise SafetyError(
                _text(ownership.get("detail")) or "ownership cannot be released safely"
            )
        return current

    async def _takeover_matches(self, drive: Drive) -> list[Drive]:
        try:
            discovered = await self.discover()
        except CommandError as exc:
            raise SafetyError("drive ownership could not be revalidated safely") from exc
        return [candidate for candidate in discovered if candidate.identity == drive.identity]

    async def unmount(self, drive: Drive) -> dict[str, str]:
        if self.demo:
            return {"status": "unmounted", "detail": "Demo drive unmounted"}
        return {
            "status": "unsupported",
            "detail": "Linux unmount is disabled; the dedicated station must not automount drives.",
        }

    async def eject(self, drive: Drive) -> dict[str, str]:
        if self.demo:
            return {"status": "ejected", "detail": "Demo drive ejected"}
        if not shutil.which("udisksctl"):
            return {"status": "unsupported", "detail": "udisksctl is unavailable"}
        try:
            current = await self.validate(drive)
            siblings = self._usb_enclosure_siblings(current.path)
            if siblings:
                raise SafetyError(
                    "USB enclosure contains other disks: " + ", ".join(sorted(siblings))
                )
        except (SafetyError, CommandError) as exc:
            return {"status": "failed", "detail": str(exc)}
        result = await self._runner.run(
            "udisksctl",
            "power-off",
            "--no-user-interaction",
            "-b",
            current.path,
            timeout=60,
        )
        if result.returncode:
            return {
                "status": "failed",
                "detail": result.stderr.strip() or "udisksctl could not power off the drive",
            }
        try:
            remaining = await self.discover()
        except (SafetyError, CommandError):
            return {
                "status": "failed",
                "detail": "Power-off returned success, but disappearance could not be verified",
            }
        if any(candidate.identity == current.identity for candidate in remaining):
            return {"status": "failed", "detail": "Drive remains visible after power-off"}
        return {"status": "ejected", "detail": "USB drive powered off and is safe to remove"}

    @staticmethod
    def _usb_enclosure_siblings(path: str) -> set[str]:
        target_name = Path(path).name
        class_root = Path("/sys/class/block")
        target_link = class_root / target_name / "device"
        try:
            target_parts = target_link.resolve(strict=True).parts
        except OSError as exc:
            raise SafetyError("USB enclosure scope could not be identified") from exc

        def enclosure(parts: tuple[str, ...]) -> tuple[str, ...] | None:
            for index in range(len(parts) - 1, -1, -1):
                if re.fullmatch(r"\d+-\d+(?:\.\d+)*", parts[index]):
                    return parts[: index + 1]
            return None

        target_enclosure = enclosure(target_parts)
        if target_enclosure is None:
            raise SafetyError("USB enclosure scope could not be identified")
        siblings: set[str] = set()
        try:
            entries = list(class_root.iterdir())
        except OSError as exc:
            raise SafetyError("USB enclosure siblings could not be enumerated") from exc
        for entry in entries:
            if entry.name == target_name or (entry / "partition").exists():
                continue
            try:
                candidate = (entry / "device").resolve(strict=True)
            except OSError:
                continue
            if enclosure(candidate.parts) == target_enclosure:
                siblings.add(f"/dev/{entry.name}")
        return siblings

    async def smart(self, drive: Drive) -> dict[str, Any]:
        current = await self.validate(drive)
        if self.demo:
            raw = self._demo_smart()
            return {"health": "passed", "warnings": [], "raw": raw}
        result = await self._runner.run(
            "smartctl", "-a", "-j", self._smart_path(current), timeout=45
        )
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError:
            raw = {}
        status = result.returncode & 0xFF
        warnings = self._smart_warnings(status)
        warnings.extend(message for message in self._media_warnings(raw) if message not in warnings)
        passed = raw.get("smart_status", {}).get("passed")
        if passed is False or status & 0b00011000:
            health = "failed"
        elif status & 0b00000111:
            health = "unsupported"
        elif status & 0b11100000 or warnings:
            health = "warning"
        elif passed is True:
            health = "passed"
        else:
            health = "unsupported"
        return {"health": health, "warnings": warnings, "raw": raw}

    @staticmethod
    def _media_warnings(raw: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        attributes = raw.get("ata_smart_attributes", {}).get("table", [])
        labels = {
            5: "SMART reports reallocated sectors",
            197: "SMART reports pending sectors",
            198: "SMART reports offline uncorrectable sectors",
        }
        for attribute in attributes if isinstance(attributes, list) else []:
            try:
                attribute_id = int(attribute.get("id"))
                value = int(attribute.get("raw", {}).get("value", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if attribute_id in labels and value > 0:
                warnings.append(f"{labels[attribute_id]} ({value})")
        try:
            grown = int(raw.get("scsi_grown_defect_list", 0))
        except (TypeError, ValueError):
            grown = 0
        if grown > 0:
            warnings.append(f"SCSI grown defect list contains {grown} entries")
        scsi = raw.get("scsi_error_counter_log", {})
        if isinstance(scsi, dict):
            for operation, counters in scsi.items():
                if not isinstance(counters, dict):
                    continue
                for key, value in counters.items():
                    if "uncorrect" not in str(key).lower():
                        continue
                    try:
                        count = int(value)
                    except (TypeError, ValueError):
                        continue
                    if count > 0:
                        warnings.append(f"SCSI {operation} reports {count} uncorrected errors")
        return warnings

    @staticmethod
    def _smart_warnings(status: int) -> list[str]:
        messages = (
            "smartctl command line could not be parsed",
            "device could not be opened or identified",
            "a SMART command failed",
            "SMART reports the drive is failing",
            "a prefail attribute is at or below threshold",
            "an attribute was at or below threshold in the past",
            "the error log contains records",
            "the self-test log contains errors",
        )
        return [message for bit, message in enumerate(messages) if status & (1 << bit)]

    async def self_test(self, drive: Drive, progress: Progress) -> dict[str, Any]:
        """Run the drive's extended SMART self-test."""

        return await self._run_self_test(drive, progress, test_type="long")

    async def short_self_test(self, drive: Drive, progress: Progress) -> dict[str, Any]:
        """Run the drive's short SMART self-test without changing the long-test API."""

        return await self._run_self_test(drive, progress, test_type="short")

    async def _run_self_test(
        self, drive: Drive, progress: Progress, *, test_type: str
    ) -> dict[str, Any]:
        if test_type not in {"short", "long"}:
            raise ValueError("SMART self-test type must be short or long")
        label = "Short" if test_type == "short" else "Extended"
        current = await self.validate(drive)
        if self.demo:
            for percent in (0, 18, 52, 81, 100):
                await progress(percent, f"{label} SMART self-test")
                await asyncio.sleep(self.demo_step_seconds)
            raw = self._demo_smart()
            return {"status": "passed", "detail": f"{label} self-test completed", "raw": raw}
        self._cancel_requested = False
        self._self_test_drive = current
        try:
            initial = await self.smart(current)
            if initial["health"] == "unsupported":
                return {
                    "status": "unsupported",
                    "detail": "SMART self-test status is unavailable",
                    "raw": initial["raw"],
                }
            if self._self_test_in_progress(initial["raw"]):
                return {
                    "status": "incomplete",
                    "detail": "A SMART self-test was already running; it was not adopted",
                    "raw": initial["raw"],
                }
            initial_log = self._self_test_log_fingerprint(initial["raw"])
            started = await self._runner.run(
                "smartctl", "-t", test_type, self._smart_path(current), timeout=45
            )
            combined = f"{started.stdout}\n{started.stderr}".lower()
            if started.returncode & 0b00000111 or "unsupported" in combined:
                return {
                    "status": "unsupported",
                    "detail": f"{label} self-test is unsupported",
                    "raw": {},
                }
            await progress(0, f"{label} SMART self-test started")
            if test_type == "short":
                durations = self.self_test_recommended_seconds(initial["raw"])
                timeout_seconds = self._short_self_test_timeout(durations["short"])
            else:
                timeout_seconds = self.self_test_timeout_seconds
            deadline = time.monotonic() + timeout_seconds
            while True:
                if self._cancel_requested:
                    raise asyncio.CancelledError
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    await self._abort_self_test(current)
                    return {
                        "status": "incomplete",
                        "detail": f"{label} self-test exceeded its safety time limit",
                        "raw": {},
                    }
                await asyncio.sleep(min(self.self_test_poll_seconds, remaining_time))
                if time.monotonic() >= deadline:
                    await self._abort_self_test(current)
                    return {
                        "status": "incomplete",
                        "detail": f"{label} self-test exceeded its safety time limit",
                        "raw": {},
                    }
                current = await self.validate(current)
                snapshot = await self.smart(current)
                raw = snapshot["raw"]
                remaining = self._remaining_percent(raw)
                if remaining is not None and remaining > 0:
                    await progress(
                        max(0, 100 - remaining),
                        self._self_test_poll_detail(label, remaining),
                    )
                    continue
                if self._self_test_in_progress(raw):
                    await progress(0, self._self_test_poll_detail(label, None))
                    continue
                latest_log = self._self_test_log_fingerprint(raw)
                if latest_log is None or latest_log == initial_log:
                    # Some bridges omit execution status.  Never mistake the
                    # previous log's successful entry for the test we started.
                    await progress(
                        0,
                        f"Waiting for a new SMART self-test result; "
                        f"last polled {self._poll_timestamp()}",
                    )
                    continue
                outcome, detail = self._self_test_outcome(raw)
                await progress(100, detail)
                return {"status": outcome, "detail": detail, "raw": raw}
        except SafetyError as exc:
            return {"status": "incomplete", "detail": str(exc), "raw": {}}
        except asyncio.CancelledError:
            await self._abort_self_test(current)
            raise
        finally:
            self._self_test_drive = None

    async def estimate_info(self, drive: Drive) -> dict[str, Any]:
        """Read drive-advertised self-test durations without starting a test."""

        current = await self.validate(drive)
        if self.demo:
            return {
                "self_test_seconds": {"short": 120, "long": 450 * 60},
                "notes": ["Demo durations are synthetic."],
            }
        try:
            result = await self._probe_runner.run(
                "smartctl", "-a", "-j", self._smart_path(current), timeout=45
            )
        except CommandError as exc:
            return {
                "self_test_seconds": {"short": None, "long": None},
                "notes": [f"SMART duration estimate is unavailable: {exc}"],
            }
        await self.validate(current)
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError:
            raw = {}
        durations = self.self_test_recommended_seconds(raw)
        notes: list[str] = []
        status = result.returncode & 0xFF
        if status & 0b00000111:
            durations = {"short": None, "long": None}
            notes.append("SMART duration estimate is unavailable for this drive or bridge.")
        elif not any(value is not None for value in durations.values()):
            notes.append("The drive did not report recommended SMART self-test durations.")
        else:
            notes.append("Durations are reported by the drive firmware and may vary in practice.")
        return {"self_test_seconds": durations, "notes": notes}

    @staticmethod
    def self_test_recommended_seconds(raw: dict[str, Any]) -> dict[str, int | None]:
        """Extract ATA drive-advertised SMART polling durations from smartctl JSON."""

        if not isinstance(raw, dict):
            return {"short": None, "long": None}
        ata = raw.get("ata_smart_data")
        if not isinstance(ata, dict):
            ata = {}
        self_test = ata.get("self_test")
        if not isinstance(self_test, dict):
            self_test = {}
        polling = self_test.get("polling_minutes")
        if not isinstance(polling, dict):
            polling = {}

        def seconds(key: str) -> int | None:
            value = polling.get(key)
            if isinstance(value, bool):
                return None
            try:
                minutes = int(value)
            except (TypeError, ValueError):
                return None
            return minutes * 60 if minutes > 0 else None

        return {"short": seconds("short"), "long": seconds("extended")}

    def _short_self_test_timeout(self, recommended_seconds: int | None) -> float:
        """Allow firmware time plus grace, bounded to thirty minutes."""

        if recommended_seconds is None:
            return self.short_self_test_timeout_cap_seconds
        with_grace = recommended_seconds + max(120, recommended_seconds // 2)
        return min(self.short_self_test_timeout_cap_seconds, max(5 * 60, with_grace))

    @staticmethod
    def _poll_timestamp() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @classmethod
    def _self_test_poll_detail(cls, label: str, remaining: int | None) -> str:
        if remaining is None:
            state = "firmware reports the test is running without a percentage"
        else:
            state = f"firmware reports {remaining}% remaining"
        return f"{label} SMART self-test running; {state}; last polled {cls._poll_timestamp()}"

    @staticmethod
    def _remaining_percent(raw: dict[str, Any]) -> int | None:
        candidates = [
            raw.get("ata_smart_data", {}).get("self_test", {}).get("status", {}),
            raw.get("scsi_self_test_0", {}),
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            value = candidate.get("remaining_percent")
            if value is not None:
                try:
                    return max(0, min(100, int(value)))
                except (TypeError, ValueError):
                    pass
            text = _text(candidate.get("string") or candidate.get("status")).lower()
            match = re.search(r"(\d+)%.*remaining", text)
            if match:
                return max(0, min(100, int(match.group(1))))
        return None

    @classmethod
    def _self_test_in_progress(cls, raw: dict[str, Any]) -> bool:
        remaining = cls._remaining_percent(raw)
        if remaining is not None and remaining > 0:
            return True
        candidates = [
            raw.get("ata_smart_data", {}).get("self_test", {}).get("status", {}),
            raw.get("scsi_self_test_0", {}),
        ]
        for status in candidates:
            if not isinstance(status, dict):
                continue
            text = _text(status.get("string") or status.get("status")).lower()
            if "in progress" in text:
                return True
        ata_status = candidates[0]
        if isinstance(ata_status, dict):
            try:
                # ATA execution status high nibble 0xF denotes in progress.
                return int(ata_status.get("value")) >> 4 == 0xF
            except (TypeError, ValueError):
                pass
        return False

    @staticmethod
    def _self_test_log_fingerprint(raw: dict[str, Any]) -> str | None:
        if not isinstance(raw, dict):
            return None
        ata_log = raw.get("ata_smart_self_test_log")
        if not isinstance(ata_log, dict):
            ata_log = {}
        standard = ata_log.get("standard")
        if not isinstance(standard, dict):
            standard = {}
        tables = standard.get("table")
        if not tables:
            scsi_log = raw.get("scsi_self_test_log")
            if not isinstance(scsi_log, dict):
                scsi_log = {}
            tables = scsi_log.get("table")
        if not isinstance(tables, list) or not tables:
            return None
        return json.dumps(tables, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _self_test_outcome(raw: dict[str, Any]) -> tuple[str, str]:
        tables = raw.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
        if not tables:
            tables = raw.get("scsi_self_test_log", {}).get("table", [])
        if not tables:
            return "incomplete", "No terminal self-test result was reported"
        status = tables[0].get("status", {})
        text = _text(status.get("string") if isinstance(status, dict) else status)
        lowered = text.lower()
        if "completed without error" in lowered or lowered in {"completed", "passed"}:
            return "passed", text or "Self-test passed"
        if any(word in lowered for word in ("fail", "error", "aborted", "interrupted")):
            return "failed", text or "Self-test failed"
        return "incomplete", text or "Self-test result was not conclusive"

    async def benchmark(self, drive: Drive, progress: Progress) -> dict[str, Any]:
        current = await self.validate(drive)
        if self.demo:
            for percent in (0, 25, 61, 100):
                await progress(percent, "Measuring sequential read speed")
                await asyncio.sleep(self.demo_step_seconds)
            return {
                "status": "passed",
                "read_mbps": 182.4,
                "detail": "30 second sequential read sample completed",
                "raw": {"jobs": [{"read": {"bw_bytes": 182_400_000}}]},
            }
        sample_bytes = min(2 * 1024**3, current.size_bytes)
        return await self._fio(
            current,
            progress,
            destructive=False,
            args=("--rw=read", "--runtime=30", "--time_based=1", f"--size={sample_bytes}"),
            detail="Sequential read benchmark",
            benchmark=True,
            timeout=60,
            expected_bytes=sample_bytes,
        )

    async def surface(
        self, drive: Drive, progress: Progress, destructive: bool = False
    ) -> dict[str, Any]:
        current = await self.validate(drive, destructive=destructive)
        if self.demo:
            label = (
                "Destructive write and checksum verification" if destructive else "Full read scan"
            )
            for percent in (0, 12, 39, 73, 100):
                await progress(percent, label)
                await asyncio.sleep(self.demo_step_seconds)
            return {"status": "passed", "detail": f"{label} completed", "raw": {"demo": True}}
        if destructive:
            args = (
                "--rw=write",
                f"--size={current.size_bytes}",
                "--verify=sha256",
                "--do_verify=1",
                "--verify_fatal=1",
                "--refill_buffers=1",
                "--verify_backlog=1024",
                "--verify_backlog_batch=1024",
            )
            detail = "Full-drive write and SHA-256 verification"
        else:
            args = ("--rw=read", f"--size={current.size_bytes}")
            detail = "Full-drive read scan"
        return await self._fio(
            current,
            progress,
            destructive=destructive,
            args=args,
            detail=detail,
            benchmark=False,
            timeout=60 * 60 * 72,
            expected_bytes=current.size_bytes,
        )

    async def erase_plan(self, drive: Drive) -> dict[str, Any]:
        """Return the currently safe erase methods without mutating the drive."""
        if self.demo:
            return {
                "quick": {
                    "available": True,
                    "method": "quick_format_exfat",
                    "secure": False,
                    "detail": "Demo quick format recreates the selected exFAT filesystem.",
                    "estimated_minutes": 1,
                    "targets": [
                        {
                            "id": "demo-volume",
                            "path": "/dev/demo1",
                            "size_bytes": drive.size_bytes,
                            "partuuid": "demo-partition",
                            "filesystem": "exfat",
                            "label": "DRIVECHECK",
                        }
                    ],
                },
                "initialize": {
                    "available": True,
                    "method": "initialize_exfat",
                    "secure": False,
                    "detail": "Demo disk initialization replaces the partition layout with one exFAT volume.",
                    "estimated_minutes": 2,
                },
                "secure": {
                    "available": True,
                    "method": "ata_secure_erase",
                    "secure": True,
                    "detail": "Demo firmware secure erase simulates the drive's secure erase command.",
                    "estimated_minutes": 120,
                },
                "full": {
                    "available": True,
                    "method": "full_overwrite",
                    "secure": True,
                    "detail": "Demo full erase simulates a complete overwrite and readback.",
                },
            }
        if self.firmware_erase_active:
            detail = "ATA firmware erase recovery is required before another drive action."
            return {
                "quick": self._unavailable_erase("quick_format_exfat", False, detail),
                "initialize": self._unavailable_erase("initialize_exfat", False, detail),
                "secure": self._unavailable_erase("ata_secure_erase", True, detail),
                "full": self._unavailable_erase("full_overwrite", True, detail),
            }
        try:
            current = await self.validate(drive, destructive=True)
        except (SafetyError, CommandError) as exc:
            detail = f"Drive is not safely erasable: {exc}"
            return {
                "quick": self._unavailable_erase("quick_format_exfat", False, detail),
                "initialize": self._unavailable_erase("initialize_exfat", False, detail),
                "secure": self._unavailable_erase("ata_secure_erase", True, detail),
                "full": self._unavailable_erase("full_overwrite", True, detail),
            }
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            detail = "Root privileges are required to erase a drive."
            return {
                "quick": self._unavailable_erase("quick_format_exfat", False, detail),
                "initialize": self._unavailable_erase("initialize_exfat", False, detail),
                "secure": self._unavailable_erase("ata_secure_erase", True, detail),
                "full": self._unavailable_erase("full_overwrite", True, detail),
            }

        full_available = bool(shutil.which("fio"))
        full = {
            "available": full_available,
            "method": "full_overwrite",
            "secure": True,
            "detail": (
                "Writes and reads back the entire drive with SHA-256 verification."
                if full_available
                else "fio is required for a full overwrite."
            ),
        }
        quick = await self._quick_format_plan(current)
        initialize = self._initialize_plan()
        secure = await self._secure_erase_plan(current)
        return {"quick": quick, "initialize": initialize, "secure": secure, "full": full}

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
        if profile not in {"quick_erase", "initialize_disk", "secure_erase", "full_erase"}:
            raise ValueError("unknown erase profile")
        current = await self.validate(drive, destructive=True)
        if self.demo:
            choice = (await self.erase_plan(current))[
                {
                    "quick_erase": "quick",
                    "initialize_disk": "initialize",
                    "secure_erase": "secure",
                    "full_erase": "full",
                }[profile]
            ]
        elif profile == "quick_erase":
            choice = await self._quick_format_plan(current)
        elif profile == "initialize_disk":
            choice = self._initialize_plan()
        elif profile == "secure_erase":
            choice = await self._secure_erase_plan(current)
        else:
            choice = {
                "available": bool(shutil.which("fio")),
                "method": "full_overwrite",
                "secure": True,
                "detail": "Writes and reads back the entire drive with SHA-256 verification.",
            }
        if not choice["available"]:
            raise SafetyError(choice["detail"])
        method = choice["method"]
        if expected_method is not None and expected_method != method:
            raise SafetyError("erase method changed; review and confirm the new plan")
        if self.demo:
            for percent in (0, 35, 72, 100):
                await progress(percent, choice["detail"])
                await asyncio.sleep(self.demo_step_seconds)
            return {"status": "passed", "method": method, "detail": choice["detail"]}
        if profile == "full_erase":
            result = await self.surface(drive, progress, destructive=True)
            return {**result, "method": "full_overwrite"}
        if profile == "initialize_disk":
            return await self._initialize_exfat(drive, progress)
        if method == "ata_secure_erase":
            return await self._ata_secure_erase(
                drive,
                progress,
                recovery_dir=recovery_dir,
                estimated_minutes=choice.get("estimated_minutes"),
            )
        return await self._quick_format_exfat(drive, progress, target_id)

    async def _secure_erase_plan(self, drive: Drive) -> dict[str, Any]:
        if shutil.which("hdparm"):
            try:
                security = await self._ata_security(drive)
            except (CommandError, SafetyError):
                return self._unavailable_erase(
                    "ata_secure_erase",
                    True,
                    "ATA security state could not be verified safely.",
                )
            if security.supported is True:
                if security.ready:
                    return {
                        "available": True,
                        "method": "ata_secure_erase",
                        "secure": True,
                        "detail": "Drive firmware reports ATA Secure Erase ready.",
                        "estimated_minutes": security.erase_minutes,
                    }
                states = []
                for name in ("enabled", "locked", "frozen"):
                    value = getattr(security, name)
                    if value is not False:
                        states.append(name if value is True else f"unknown {name}")
                return self._unavailable_erase(
                    "ata_secure_erase",
                    True,
                    "ATA Secure Erase is not safe while security state is "
                    + ", ".join(states)
                    + ".",
                )
            if security.supported is None:
                return self._unavailable_erase(
                    "ata_secure_erase",
                    True,
                    "ATA Secure Erase support could not be determined.",
                )
        return self._unavailable_erase(
            "ata_secure_erase",
            True,
            "This drive or adapter does not report ATA Secure Erase support.",
        )

    @staticmethod
    def _unavailable_erase(method: str, secure: bool, detail: str) -> dict[str, Any]:
        return {
            "available": False,
            "method": method,
            "secure": secure,
            "detail": detail,
            "estimated_minutes": None,
        }

    async def _quick_format_plan(self, drive: Drive) -> dict[str, Any]:
        required = ("mkfs.exfat", "blkid", "lsblk")
        missing = [name for name in required if not shutil.which(name)]
        if missing:
            return Hardware._unavailable_erase(
                "quick_format_exfat",
                False,
                "Quick format requires: " + ", ".join(missing) + ".",
            )
        targets = await self._format_targets(drive.path)
        if not targets:
            return {
                **Hardware._unavailable_erase(
                    "quick_format_exfat",
                    False,
                    "Quick format requires an existing unmounted partition. Use Initialize/reset disk to create a new layout.",
                ),
                "targets": [],
            }
        return {
            "available": True,
            "method": "quick_format_exfat",
            "secure": False,
            "detail": "Quick-formats one selected existing partition as exFAT without changing the partition table or other partitions.",
            "estimated_minutes": 2,
            "targets": targets,
        }

    @staticmethod
    def _initialize_plan() -> dict[str, Any]:
        required = ("wipefs", "sgdisk", "mkfs.exfat", "partprobe", "blkid", "lsblk")
        missing = [name for name in required if not shutil.which(name)]
        if missing:
            return Hardware._unavailable_erase(
                "initialize_exfat",
                False,
                "Disk initialization requires: " + ", ".join(missing) + ".",
            )
        return {
            "available": True,
            "method": "initialize_exfat",
            "secure": False,
            "detail": "Replaces the entire partition layout with one empty exFAT volume; old data is not securely overwritten.",
            "estimated_minutes": 2,
        }

    async def _ata_security(self, drive: Drive):
        current = await self.validate(drive, destructive=True)
        result = await self._probe_runner.run("hdparm", "-I", current.path, timeout=45)
        if result.returncode:
            raise CommandError("hdparm could not identify ATA security state")
        return parse_hdparm_security(result.stdout)

    async def _ata_secure_erase(
        self,
        drive: Drive,
        progress: Progress,
        *,
        recovery_dir: Path,
        estimated_minutes: int | None,
    ) -> dict[str, Any]:
        current = await self.validate(drive, destructive=True)
        password = secrets.token_hex(12)
        journal = RecoveryJournal(recovery_dir, current.identity)
        payload = {
            "version": 1,
            "identity": current.identity,
            "serial": current.serial,
            "path": current.path,
            "password": password,
            "stage": "password_prepared",
        }
        try:
            journal.create(payload)
        except (OSError, RuntimeError) as exc:
            raise SafetyError(
                "a protected ATA erase recovery journal could not be created"
            ) from exc

        armed = False
        try:
            with self._exclusive_claim(current.path):
                current = await self.validate(current, destructive=True)
                device_number = self._pin_device(current.path)
                security = await self._ata_security(current)
                if not security.ready:
                    raise SafetyError("ATA security state changed before erase")
                await progress(None, "Preparing ATA Secure Erase; progress unavailable")
                self.firmware_erase_active = True
                armed = True
                set_password = await self._runner.run(
                    "hdparm",
                    "--user-master",
                    "u",
                    "--security-set-pass",
                    password,
                    current.path,
                    timeout=60,
                    sensitive_args={4},
                )
                if set_password.returncode:
                    return self._ata_recovery_result()
                payload["stage"] = "password_set"
                journal.update(payload)
                await progress(None, "Firmware erase running; progress unavailable")
                timeout = min(48 * 60 * 60, max(60 * 60, ((estimated_minutes or 720) + 30) * 60))
                erase = await self._run_ata_erase_command(
                    current, password, device_number, progress, timeout
                )
                if erase.returncode or self._cancel_requested:
                    return self._ata_recovery_result()
                security = await self._ata_security(current)
                if security.enabled is not False or security.locked is not False:
                    return self._ata_recovery_result()
        except asyncio.CancelledError:
            if armed:
                raise
            try:
                journal.remove()
            except OSError as exc:
                raise SafetyError(
                    "ATA recovery journal cleanup requires operator attention"
                ) from exc
            raise
        except (CommandError, SafetyError, OSError, RuntimeError):
            if armed:
                return self._ata_recovery_result()
            try:
                journal.remove()
            except OSError as exc:
                raise SafetyError(
                    "ATA recovery journal cleanup requires operator attention"
                ) from exc
            raise
        try:
            journal.remove()
        except OSError:
            return self._ata_recovery_result()
        self.firmware_erase_active = False
        await progress(100, "ATA Secure Erase completed")
        return {
            "status": "passed",
            "method": "ata_secure_erase",
            "detail": "Drive firmware completed ATA Secure Erase and disabled its security password.",
        }

    async def _run_ata_erase_command(
        self,
        drive: Drive,
        password: str,
        device_number: int,
        progress: Progress,
        timeout: float,
    ) -> CommandResult:
        self._cancel_requested = False
        task = asyncio.create_task(
            self._runner.run(
                "hdparm",
                "--user-master",
                "u",
                "--security-erase",
                password,
                drive.path,
                timeout=timeout,
                sensitive_args={4},
            )
        )
        safety_error: SafetyError | CommandError | None = None
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.io_safety_poll_seconds)
                if done:
                    break
                try:
                    current = await self.validate(drive, destructive=True)
                    if self._pin_device(current.path) != device_number:
                        raise SafetyError("block device number changed")
                    if self._cancel_requested:
                        raise SafetyError("ATA erase cancellation was requested")
                    await progress(None, "Firmware erase running; progress unavailable")
                except (SafetyError, CommandError) as exc:
                    safety_error = exc
                    await self._runner.cancel()
                    break
            result = await task
        except asyncio.CancelledError:
            await self._runner.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if safety_error is not None:
            raise safety_error
        return result

    @staticmethod
    def _ata_recovery_result() -> dict[str, Any]:
        return {
            "status": "incomplete",
            "method": "ata_secure_erase",
            "detail": "ATA erase state is uncertain; use the protected recovery journal before any further action.",
            "recovery_required": True,
        }

    async def _quick_format_exfat(
        self, drive: Drive, progress: Progress, target_id: str | None
    ) -> dict[str, Any]:
        current = await self.validate(drive, destructive=True)
        device_number = self._pin_device(current.path)
        try:
            before = await self._format_targets(current.path)
            target = next((item for item in before if item["id"] == target_id), None)
            if target is None:
                raise SafetyError("the selected quick-format volume is missing or changed")
            topology = tuple(item["id"] for item in before)
            target_device_number = self._pin_device(target["path"])
            await self._erase_revalidate(current, device_number)
            with self._exclusive_claim(current.path):
                if (
                    tuple(item["id"] for item in await self._format_targets(current.path))
                    != topology
                ):
                    raise SafetyError("partition topology changed before quick format")

            # mkfs.exfat performs a quick format by default and opens the selected
            # partition exclusively. It recreates that volume's filesystem metadata
            # without rewriting the disk's partition table or scanning every sector.
            self._cancel_requested = False
            task = asyncio.create_task(
                self._runner.run(
                    "mkfs.exfat", "-K", "-L", "DRIVECHECK", target["path"], timeout=60 * 60
                )
            )
            safety_error: SafetyError | CommandError | None = None
            try:
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=self.io_safety_poll_seconds)
                    if done:
                        break
                    try:
                        await self._erase_revalidate(current, device_number)
                        if self._pin_device(target["path"]) != target_device_number:
                            raise SafetyError("quick-format volume block device changed")
                        if (
                            tuple(item["id"] for item in await self._format_targets(current.path))
                            != topology
                        ):
                            raise SafetyError("partition topology changed during quick format")
                        if self._cancel_requested:
                            raise SafetyError("quick format cancellation was requested")
                        await progress(90, f"Quick-formatting {target['path']} as exFAT")
                    except (SafetyError, CommandError) as exc:
                        safety_error = exc
                        await self._runner.cancel()
                        break
                formatted = await task
            except asyncio.CancelledError:
                await self._runner.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            if safety_error is not None:
                raise safety_error
            if formatted.returncode:
                raise CommandError("mkfs.exfat could not quick-format the selected volume")
            await self._erase_revalidate(current, device_number)
            if self._pin_device(target["path"]) != target_device_number:
                raise SafetyError("quick-format volume block device changed")
            after = await self._format_targets(current.path)
            if tuple(item["id"] for item in after) != topology:
                raise SafetyError("partition topology changed after quick format")
            verified = await self._runner.run("blkid", "-o", "export", target["path"], timeout=30)
            fields = dict(
                line.split("=", 1) for line in verified.stdout.splitlines() if "=" in line
            )
            if verified.returncode or fields.get("TYPE") != "exfat":
                raise CommandError("the selected exFAT volume could not be verified")
        except (CommandError, SafetyError) as exc:
            return {
                "status": "failed",
                "method": "quick_format_exfat",
                "target": target_id,
                "detail": str(exc),
            }
        await progress(100, f"Quick exFAT format completed on {target['path']}")
        return {
            "status": "passed",
            "method": "quick_format_exfat",
            "target": target,
            "detail": f"{target['path']} was quick-formatted as exFAT; the partition table and other volumes were preserved. Old file contents may remain recoverable.",
        }

    async def _initialize_exfat(self, drive: Drive, progress: Progress) -> dict[str, Any]:
        current = await self.validate(drive, destructive=True)
        device_number = self._pin_device(current.path)
        try:
            old_partitions = await self._erase_partitions(current.path)
            with self._exclusive_claim(current.path):
                total_steps = len(old_partitions) + 6
                step = 0
                for path in (*old_partitions, current.path):
                    await self._erase_revalidate(current, device_number)
                    result = await self._runner.run("wipefs", "--all", "--force", path, timeout=120)
                    if result.returncode:
                        raise CommandError("wipefs could not remove old signatures")
                    step += 1
                    await progress(int(step / total_steps * 100), "Removing old signatures")
                commands = (
                    ("sgdisk", "--zap-all", current.path),
                    (
                        "sgdisk",
                        "--clear",
                        "--new=1:0:0",
                        "--typecode=1:0700",
                        "--change-name=1:DRIVECHECK",
                        current.path,
                    ),
                    ("partprobe", current.path),
                )
                for command in commands:
                    await self._erase_revalidate(current, device_number)
                    result = await self._runner.run(*command, timeout=120)
                    if result.returncode:
                        raise CommandError(f"{command[0]} could not prepare the new volume")
                    step += 1
                    await progress(int(step / total_steps * 100), "Creating a new GPT volume")
                partition = await self._wait_for_single_partition(current.path)
                await self._erase_revalidate(current, device_number)
                if await self._erase_partitions(current.path) != (partition,):
                    raise SafetyError("the new partition topology could not be verified")

            # exfatprogs opens the partition O_EXCL itself. Holding an O_EXCL
            # claim on the parent disk here makes that safe formatter open fail
            # with EBUSY, so hand the claim directly to mkfs after one final
            # identity/topology check. If an automounter wins the tiny handoff
            # window, mkfs's exclusive open fails instead of formatting it.
            formatted = await self._run_initialized_exfat_format(
                current, partition, device_number, progress
            )
            if formatted.returncode:
                raise CommandError("mkfs.exfat could not create the volume")
            step += 1
            await progress(int(step / total_steps * 100), "Creating the exFAT filesystem")

            await self._erase_revalidate(current, device_number)
            with self._exclusive_claim(current.path):
                await self._erase_revalidate(current, device_number)
                if await self._erase_partitions(current.path) != (partition,):
                    raise SafetyError("the new partition topology could not be verified")
                verified = await self._runner.run("blkid", "-o", "export", partition, timeout=30)
                fields = dict(
                    line.split("=", 1) for line in verified.stdout.splitlines() if "=" in line
                )
                if verified.returncode or fields.get("TYPE") != "exfat":
                    raise CommandError("the new exFAT filesystem could not be verified")
                await self._erase_revalidate(current, device_number)
                if await self._erase_partitions(current.path) != (partition,):
                    raise SafetyError("the new partition topology could not be verified")
        except (CommandError, SafetyError) as exc:
            return {
                "status": "failed",
                "method": "initialize_exfat",
                "detail": str(exc),
            }
        await progress(100, "Disk initialization completed")
        return {
            "status": "passed",
            "method": "initialize_exfat",
            "detail": "The partition layout was replaced with one quick-formatted exFAT volume; old data was not securely overwritten.",
        }

    async def _run_initialized_exfat_format(
        self,
        drive: Drive,
        partition: str,
        device_number: int,
        progress: Progress,
    ) -> CommandResult:
        await self._erase_revalidate(drive, device_number)
        if await self._erase_partitions(drive.path) != (partition,):
            raise SafetyError("the new partition topology changed before formatting")
        self._cancel_requested = False
        task = asyncio.create_task(
            self._runner.run("mkfs.exfat", "-K", "-L", "DRIVECHECK", partition, timeout=60 * 60)
        )
        safety_error: SafetyError | CommandError | None = None
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.io_safety_poll_seconds)
                if done:
                    break
                try:
                    await self._erase_revalidate(drive, device_number)
                    if await self._erase_partitions(drive.path) != (partition,):
                        raise SafetyError("partition topology changed during formatting")
                    if self._cancel_requested:
                        raise SafetyError("quick format cancellation was requested")
                    await progress(90, "Creating the exFAT filesystem")
                except (SafetyError, CommandError) as exc:
                    safety_error = exc
                    await self._runner.cancel()
                    break
            result = await task
        except asyncio.CancelledError:
            await self._runner.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if safety_error is not None:
            raise safety_error
        await self._erase_revalidate(drive, device_number)
        if await self._erase_partitions(drive.path) != (partition,):
            raise SafetyError("partition topology changed after formatting")
        return result

    async def _erase_revalidate(self, drive: Drive, device_number: int) -> Drive:
        current = await self.validate(drive, destructive=True)
        if self._pin_device(current.path) != device_number:
            raise SafetyError("block device number changed")
        return current

    async def _format_targets(self, disk_path: str) -> list[dict[str, Any]]:
        result = await self._discovery_runner.run(
            "lsblk",
            "--json",
            "--bytes",
            "--paths",
            "--output",
            "PATH,TYPE,PKNAME,SIZE,START,PARTUUID,PARTTYPE,MAJ:MIN,FSTYPE,LABEL,MOUNTPOINTS",
            disk_path,
            timeout=15,
        )
        if result.returncode:
            raise CommandError("quick-format volume topology could not be read")
        try:
            nodes = json.loads(result.stdout).get("blockdevices", [])
        except (AttributeError, json.JSONDecodeError) as exc:
            raise CommandError("quick-format volume topology was invalid") from exc
        if len(nodes) != 1 or _text(nodes[0].get("path")) != disk_path:
            raise SafetyError("volume topology did not uniquely match the selected drive")
        disk_name = Path(disk_path).name
        targets: list[dict[str, Any]] = []
        for child in nodes[0].get("children") or []:
            path = _text(child.get("path"))
            if (
                _text(child.get("type")) != "part"
                or Path(_text(child.get("pkname"))).name != disk_name
                or not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", path)
                or child.get("children")
                or _mountpoints(child)
            ):
                raise SafetyError("partition topology is unsafe for quick format")
            try:
                size = int(child.get("size") or 0)
                raw_start = child.get("start")
                if raw_start is None or raw_start == "":
                    raise ValueError("missing start offset")
                start = int(raw_start)
            except (TypeError, ValueError) as exc:
                raise SafetyError("quick-format volume boundaries are invalid") from exc
            if size <= 0 or start < 0:
                raise SafetyError("quick-format volume boundaries are invalid")
            partuuid = _text(child.get("partuuid"))
            parttype = _text(child.get("parttype"))
            major_minor = _text(child.get("maj:min"))
            if not re.fullmatch(r"\d+:\d+", major_minor):
                raise SafetyError("quick-format volume device number is invalid")
            target_id = hashlib.sha256(
                f"{path}\0{size}\0{start}\0{partuuid}\0{parttype}\0{major_minor}".encode()
            ).hexdigest()[:24]
            targets.append(
                {
                    "id": target_id,
                    "path": path,
                    "size_bytes": size,
                    "start_offset": start,
                    "partuuid": partuuid or None,
                    "parttype": parttype or None,
                    "device_number": major_minor,
                    "filesystem": _text(child.get("fstype")) or None,
                    "label": _text(child.get("label")) or None,
                }
            )
        return sorted(targets, key=lambda item: item["path"])

    async def _erase_partitions(self, disk_path: str) -> tuple[str, ...]:
        result = await self._discovery_runner.run(
            "lsblk", "--json", "--paths", "--output", "PATH,TYPE,PKNAME", disk_path, timeout=15
        )
        if result.returncode:
            raise CommandError("partition topology could not be read")
        try:
            nodes = json.loads(result.stdout).get("blockdevices", [])
        except (AttributeError, json.JSONDecodeError) as exc:
            raise CommandError("partition topology was invalid") from exc
        if len(nodes) != 1 or _text(nodes[0].get("path")) != disk_path:
            raise SafetyError("partition topology did not uniquely match the selected drive")
        disk_name = Path(disk_path).name
        partitions: list[str] = []
        for child in nodes[0].get("children") or []:
            if (
                _text(child.get("type")) != "part"
                or Path(_text(child.get("pkname"))).name != disk_name
            ):
                raise SafetyError("unexpected stacked block device appeared during erase")
            path = _text(child.get("path"))
            if not re.fullmatch(r"/dev/[A-Za-z0-9._+-]+", path) or child.get("children"):
                raise SafetyError("partition topology is unsafe")
            partitions.append(path)
        return tuple(sorted(partitions))

    async def _wait_for_single_partition(self, disk_path: str) -> str:
        for _ in range(20):
            partitions = await self._erase_partitions(disk_path)
            if len(partitions) == 1:
                return partitions[0]
            await asyncio.sleep(0.1)
        raise SafetyError("new partition did not appear uniquely")

    async def _fio(
        self,
        drive: Drive,
        progress: Progress,
        *,
        destructive: bool,
        args: tuple[str, ...],
        detail: str,
        benchmark: bool,
        timeout: float,
        expected_bytes: int,
    ) -> dict[str, Any]:
        current = await self.validate(drive, destructive=destructive)
        # Linux O_EXCL on a block device rejects an already mounted/in-use
        # target and holds an exclusive claim that prevents a new mount while
        # fio is active. fio itself uses a non-exclusive device open.
        with self._exclusive_claim(current.path):
            return await self._fio_claimed(
                current,
                progress,
                destructive=destructive,
                args=args,
                detail=detail,
                benchmark=benchmark,
                timeout=timeout,
                expected_bytes=expected_bytes,
            )

    async def _fio_claimed(
        self,
        drive: Drive,
        progress: Progress,
        *,
        destructive: bool,
        args: tuple[str, ...],
        detail: str,
        benchmark: bool,
        timeout: float,
        expected_bytes: int,
    ) -> dict[str, Any]:
        current = await self.validate(drive, destructive=destructive)
        device_number = self._pin_device(current.path)
        logical_sector = self._logical_sector_bytes.get(current.identity, 0)
        block_size = self._fio_block_size(expected_bytes, logical_sector)
        self._cancel_requested = False

        json_buffer = ""
        latest_snapshot: dict[str, Any] | None = None
        stream_invalid = False
        decoder = json.JSONDecoder()

        async def parse_status(chunk: str) -> None:
            nonlocal json_buffer, latest_snapshot, stream_invalid
            json_buffer += chunk
            if len(json_buffer) > 4_000_000:
                # Parsed documents are discarded as they arrive.  A single
                # malformed/hostile document must not grow memory indefinitely.
                json_buffer = ""
                stream_invalid = True
                return
            while True:
                json_buffer = json_buffer.lstrip()
                try:
                    snapshot, end = decoder.raw_decode(json_buffer)
                except json.JSONDecodeError:
                    return
                json_buffer = json_buffer[end:]
                if not isinstance(snapshot, dict):
                    continue
                latest_snapshot = snapshot
                stream_invalid = False
                percent = self._fio_progress(snapshot, expected_bytes, destructive, benchmark)
                await progress(min(99.99, percent), detail)

        await progress(0, detail)
        command_parts = [
            "fio",
            "--name=drivecheck",
            f"--filename={current.path}",
            "--allow_file_create=0",
            *self._fio_direct_args(),
            f"--ioengine={self._fio_engine()}",
            "--iodepth=16",
            f"--bs={block_size}",
            "--output-format=json",
            "--status-interval=2",
        ]
        if not destructive:
            command_parts.append("--readonly")
        command = (*command_parts, *args)
        io_task = asyncio.create_task(
            self._runner.run(*command, timeout=timeout, stdout_chunk=parse_status)
        )
        safety_error: SafetyError | CommandError | None = None
        try:
            while not io_task.done():
                done, _ = await asyncio.wait({io_task}, timeout=self.io_safety_poll_seconds)
                if done:
                    break
                try:
                    await self.validate(current, destructive=destructive)
                    if self._pin_device(current.path) != device_number:
                        raise SafetyError("block device number changed")
                except (SafetyError, CommandError) as exc:
                    safety_error = exc
                    await self._runner.cancel()
                    break
            result = await io_task
        except asyncio.CancelledError:
            await self._runner.cancel()
            await asyncio.gather(io_task, return_exceptions=True)
            raise
        if safety_error is not None:
            return {"status": "incomplete", "detail": str(safety_error), "raw": {}}
        if stream_invalid:
            return {
                "status": "incomplete",
                "detail": "fio status output exceeded safety limits",
                "raw": {},
            }
        try:
            raw = latest_snapshot or self._json_documents(result.stdout)[-1]
        except (json.JSONDecodeError, IndexError):
            return {"status": "incomplete", "detail": "fio returned invalid JSON", "raw": {}}
        # Revalidation after I/O catches disconnect/replacement and new mounts.
        try:
            await self.validate(current, destructive=destructive)
            if self._pin_device(current.path) != device_number:
                raise SafetyError("block device number changed")
        except SafetyError as exc:
            return {"status": "incomplete", "detail": str(exc), "raw": raw}
        jobs = raw.get("jobs") or []
        errors = [job.get("error", 0) for job in jobs if job.get("error", 0)]
        if result.returncode or errors:
            return {"status": "failed", "detail": f"{detail} reported I/O errors", "raw": raw}
        if not jobs:
            return {"status": "incomplete", "detail": "fio reported no completed jobs", "raw": raw}
        read_bytes = sum(int(job.get("read", {}).get("io_bytes") or 0) for job in jobs)
        write_bytes = sum(int(job.get("write", {}).get("io_bytes") or 0) for job in jobs)
        if destructive:
            complete = read_bytes >= expected_bytes and write_bytes >= expected_bytes
        else:
            complete = read_bytes >= expected_bytes
        if not complete:
            return {
                "status": "incomplete",
                "detail": f"{detail} did not cover the expected number of bytes",
                "raw": raw,
            }
        await progress(100, f"{detail} completed")
        response: dict[str, Any] = {
            "status": "passed",
            "detail": f"{detail} completed",
            "raw": raw,
        }
        if benchmark:
            bw = jobs[0].get("read", {}).get("bw_bytes")
            if bw is None:
                # Older fio JSON reports KiB/s as bw.
                legacy = jobs[0].get("read", {}).get("bw")
                bw = float(legacy) * 1024 if legacy is not None else None
            if bw is None:
                response.update(status="incomplete", detail="fio omitted read bandwidth")
                response["read_mbps"] = None
            else:
                response["read_mbps"] = round(float(bw) / 1_000_000, 1)
        return response

    @staticmethod
    def _pin_device(path: str) -> int:
        try:
            device_stat = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise SafetyError("drive path cannot be opened as a block device") from exc
        if not stat.S_ISBLK(device_stat.st_mode):
            raise SafetyError("drive path is not a block device")
        return device_stat.st_rdev

    @staticmethod
    def _fio_engine() -> str:
        return "libaio"

    @staticmethod
    def _fio_direct_args() -> tuple[str, ...]:
        return ("--direct=1",)

    @staticmethod
    def _smart_path(drive: Drive) -> str:
        return drive.path

    @staticmethod
    @contextmanager
    def _exclusive_claim(path: str):
        flags = os.O_RDONLY | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise SafetyError("could not claim the unmounted block device exclusively") from exc
        try:
            claimed_stat = os.fstat(descriptor)
            if not stat.S_ISBLK(claimed_stat.st_mode):
                raise SafetyError("claimed path is not a block device")
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _fio_block_size(size_bytes: int, logical_sector: int) -> int:
        if logical_sector <= 0 or size_bytes <= 0 or size_bytes % logical_sector:
            raise SafetyError("drive size is not aligned to its logical sector size")
        block_size = 1024 * 1024
        while block_size > logical_sector and size_bytes % block_size:
            block_size //= 2
        if block_size < logical_sector or block_size % logical_sector:
            block_size = logical_sector
        if size_bytes % block_size:
            raise SafetyError("no safe fio block size covers the entire requested region")
        return block_size

    @staticmethod
    def _json_documents(output: str) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        decoder = json.JSONDecoder()
        position = 0
        while position < len(output):
            while position < len(output) and output[position].isspace():
                position += 1
            if position == len(output):
                break
            value, position = decoder.raw_decode(output, position)
            if isinstance(value, dict):
                documents.append(value)
        return documents

    @staticmethod
    def _fio_progress(
        snapshot: dict[str, Any], expected_bytes: int, destructive: bool, benchmark: bool
    ) -> float:
        jobs = snapshot.get("jobs") or []
        read_bytes = sum(int(job.get("read", {}).get("io_bytes") or 0) for job in jobs)
        write_bytes = sum(int(job.get("write", {}).get("io_bytes") or 0) for job in jobs)
        if benchmark:
            runtime = max((int(job.get("read", {}).get("runtime") or 0) for job in jobs), default=0)
            return round(max(0.0, min(99.99, runtime / 30_000 * 100)), 4)
        denominator = expected_bytes * (2 if destructive else 1)
        if denominator <= 0:
            return 0.0
        measured = (read_bytes + write_bytes) / denominator * 100
        return round(max(0.0, min(99.99, measured)), 4)

    async def cancel(self) -> None:
        self._cancel_requested = True
        await self._runner.cancel()
        if self._self_test_drive is not None:
            await self._abort_self_test(self._self_test_drive)

    async def _abort_self_test(self, drive: Drive) -> None:
        if self.demo:
            return
        try:
            current = await self.validate(drive)
            await self._runner.run("smartctl", "-X", self._smart_path(current), timeout=30)
        except (CommandError, SafetyError, asyncio.CancelledError):
            # Cancellation remains best effort; the job is never reported passed.
            pass

    @staticmethod
    def _demo_smart() -> dict[str, Any]:
        return {
            "smart_status": {"passed": True},
            "model_name": "WDC WD40EFZX-68AWUN0",
            "serial_number": "DEMO-WD40-2026",
            "power_on_time": {"hours": 286},
            "temperature": {"current": 31},
            "ata_smart_self_test_log": {
                "standard": {"table": [{"status": {"string": "Completed without error"}}]}
            },
        }


__all__ = [
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "Drive",
    "Hardware",
    "SafetyError",
    "get_hardware",
]


def get_hardware(demo: bool) -> Hardware:
    """Return the native hardware adapter without importing macOS code on Linux."""
    if demo:
        return Hardware(demo=True)
    if platform.system() == "Darwin":
        from drivecheck.macos import MacHardware

        return MacHardware(demo=False)
    return Hardware(demo=False)
