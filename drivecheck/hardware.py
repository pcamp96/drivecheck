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
import re
import signal
import stat
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

Progress = Callable[[int, str], Awaitable[None]]


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
            return CommandResult(
                tuple(args), process.returncode or 0, stdout, stderr, out_cut or err_cut
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
        self._cancel_requested = False
        self._self_test_drive: Drive | None = None
        self.self_test_poll_seconds = 30.0
        self.self_test_timeout_seconds = 48 * 60 * 60.0
        self.io_safety_poll_seconds = 3.0
        self.demo_step_seconds = 0.0
        self._logical_sector_bytes: dict[str, int] = {}

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
        records: list[tuple[dict[str, Any], Drive]] = []
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
                )
            )
        identity_counts: dict[str, int] = {}
        serial_counts: dict[str, int] = {}
        for _, drive in records:
            identity_counts[drive.identity] = identity_counts.get(drive.identity, 0) + 1
            if drive.serial:
                serial_counts[drive.serial] = serial_counts.get(drive.serial, 0) + 1
        drives: list[Drive] = []
        for _, drive in records:
            if identity_counts[drive.identity] > 1 or (
                drive.serial and serial_counts[drive.serial] > 1
            ):
                drive.reasons.append("duplicate_identity")
                drive.eligible = False
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

    async def smart(self, drive: Drive) -> dict[str, Any]:
        current = await self.validate(drive)
        if self.demo:
            raw = self._demo_smart()
            return {"health": "passed", "warnings": [], "raw": raw}
        result = await self._runner.run("smartctl", "-a", "-j", current.path, timeout=45)
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
        current = await self.validate(drive)
        if self.demo:
            for percent in (0, 18, 52, 81, 100):
                await progress(percent, "Extended SMART self-test")
                await asyncio.sleep(self.demo_step_seconds)
            raw = self._demo_smart()
            return {"status": "passed", "detail": "Extended self-test completed", "raw": raw}
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
            initial_entry = self._latest_self_test(initial["raw"])
            started = await self._runner.run("smartctl", "-t", "long", current.path, timeout=45)
            combined = f"{started.stdout}\n{started.stderr}".lower()
            if started.returncode & 0b00000111 or "unsupported" in combined:
                return {
                    "status": "unsupported",
                    "detail": "Extended self-test is unsupported",
                    "raw": {},
                }
            await progress(0, "Extended SMART self-test started")
            deadline = time.monotonic() + self.self_test_timeout_seconds
            observed_running = False
            while True:
                if self._cancel_requested:
                    raise asyncio.CancelledError
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    await self._abort_self_test(current)
                    return {
                        "status": "incomplete",
                        "detail": "Extended self-test exceeded the 48 hour safety limit",
                        "raw": {},
                    }
                await asyncio.sleep(min(self.self_test_poll_seconds, remaining_time))
                if time.monotonic() >= deadline:
                    await self._abort_self_test(current)
                    return {
                        "status": "incomplete",
                        "detail": "Extended self-test exceeded the 48 hour safety limit",
                        "raw": {},
                    }
                current = await self.validate(current)
                snapshot = await self.smart(current)
                raw = snapshot["raw"]
                remaining = self._remaining_percent(raw)
                if remaining is not None and remaining > 0:
                    observed_running = True
                    await progress(max(1, 100 - remaining), "Extended SMART self-test running")
                    continue
                latest_entry = self._latest_self_test(raw)
                if latest_entry is None or (latest_entry == initial_entry and not observed_running):
                    # Some bridges omit execution status.  Never mistake the
                    # previous log's successful entry for the test we started.
                    await progress(1, "Waiting for a new SMART self-test result")
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
                return int(match.group(1))
            if "in progress" in text:
                return 100
        return None

    @classmethod
    def _self_test_in_progress(cls, raw: dict[str, Any]) -> bool:
        remaining = cls._remaining_percent(raw)
        if remaining is not None and remaining > 0:
            return True
        status = raw.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
        if isinstance(status, dict):
            text = _text(status.get("string")).lower()
            if "in progress" in text:
                return True
            try:
                # ATA execution status high nibble 0xF denotes in progress.
                return int(status.get("value")) >> 4 == 0xF
            except (TypeError, ValueError):
                pass
        return False

    @staticmethod
    def _latest_self_test(raw: dict[str, Any]) -> str | None:
        tables = raw.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
        if not tables:
            tables = raw.get("scsi_self_test_log", {}).get("table", [])
        if not isinstance(tables, list) or not tables:
            return None
        return json.dumps(tables[0], sort_keys=True, separators=(",", ":"))

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
                await progress(min(99, percent), detail)

        await progress(0, detail)
        command_parts = [
            "fio",
            "--name=drivecheck",
            f"--filename={current.path}",
            "--allow_file_create=0",
            "--direct=1",
            "--ioengine=libaio",
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
    ) -> int:
        jobs = snapshot.get("jobs") or []
        read_bytes = sum(int(job.get("read", {}).get("io_bytes") or 0) for job in jobs)
        write_bytes = sum(int(job.get("write", {}).get("io_bytes") or 0) for job in jobs)
        if benchmark:
            runtime = max((int(job.get("read", {}).get("runtime") or 0) for job in jobs), default=0)
            return max(0, min(99, int(runtime / 30_000 * 100)))
        denominator = expected_bytes * (2 if destructive else 1)
        return max(0, min(99, int((read_bytes + write_bytes) / denominator * 100)))

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
            await self._runner.run("smartctl", "-X", current.path, timeout=30)
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


__all__ = ["CommandError", "CommandResult", "CommandRunner", "Drive", "Hardware", "SafetyError"]
