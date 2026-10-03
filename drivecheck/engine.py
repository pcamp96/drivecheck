"""Single-drive scheduler and live state broadcaster."""

import asyncio
import contextlib
import copy
import shutil
import time
import uuid
from datetime import UTC, datetime

from drivecheck import notifications
from drivecheck.config import Config, Settings
from drivecheck.hardware import Drive, Hardware, SafetyError, get_hardware
from drivecheck.storage import Store

TERMINAL = {"passed", "warning", "failed", "incomplete", "cancelled"}
PROFILES = {"quick", "extended", "verify"}
AUTO_DETACH_SCANS = 2


def now() -> str:
    return datetime.now(UTC).isoformat()


def verdict(results: dict) -> tuple[str, str]:
    states = [part.get("status", part.get("health", "incomplete")) for part in results.values()]
    if "failed" in states:
        return "failed", "A test found errors. Review the report before using this drive."
    if any(status in {"incomplete", "unsupported"} for status in states):
        return (
            "incomplete",
            "Some checks could not finish or were unsupported. Review test coverage.",
        )
    if "warning" in states:
        return "warning", "Tests completed with health warnings. Review the report."
    if not states or any(status != "passed" for status in states):
        return "incomplete", "Not all checks returned a conclusive result."
    return (
        "passed",
        "Selected checks passed. This is a baseline, not a guarantee of future reliability.",
    )


class Engine:
    def __init__(self, config: Config, settings: Settings, store: Store, hardware=None):
        self.config, self.settings, self.store = config, settings, store
        self.hardware = hardware or (
            Hardware(demo=True) if config.demo else get_hardware(demo=False)
        )
        if config.demo:
            self.hardware.demo_step_seconds = config.demo_step_seconds
        self.drives: list[Drive] = []
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self.tasks: list[asyncio.Task] = []
        self.active_task: asyncio.Task | None = None
        self.active_run_id: str | None = None
        self.release_in_progress = False
        self.discovery_error: str | None = None
        self.notification_error: str | None = None
        self.station_error: str | None = None
        self.enqueue_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.notice_event = asyncio.Event()
        self.auto_attempted: set[str] = set()
        self.auto_absent_scans: dict[str, int] = {}
        self.initial_scan = True
        self.tools = {name: bool(shutil.which(name)) for name in ("lsblk", "smartctl", "fio")}
        self.capabilities = self._hardware_capabilities()
        if getattr(config, "headless", False):
            # Headless is an appliance workflow. Its two required behaviors are
            # runtime invariants rather than dashboard preferences.
            self.settings.value["auto_test"] = True
            self.settings.value["auto_eject"] = True

    async def start(self):
        self.store.recover()
        self._validate_headless_startup()
        await self.scan()
        self.tasks = [
            asyncio.create_task(self.supervise("scheduler", self.worker)),
            asyncio.create_task(self.supervise("discovery", self.monitor)),
            asyncio.create_task(self.supervise("notifications", self.notifier)),
        ]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        if self.active_task:
            self.active_task.cancel()
        await asyncio.gather(
            *self.tasks, *([self.active_task] if self.active_task else []), return_exceptions=True
        )
        await self.hardware.cancel()
        # Persist any queued jobs before closing the database.
        for run in self.store.runs():
            if run["status"] == "queued":
                run.update(
                    status="incomplete",
                    detail="Service stopped before this test started.",
                    finished_at=now(),
                )
                self.store.save(run)

    def state(self) -> dict:
        runs = self.store.runs()
        public_settings = self.settings.public()
        if getattr(self.config, "headless", False):
            public_settings["auto_test"] = True
            public_settings["auto_eject"] = True
        drives = []
        for drive in self.drives:
            item = drive.to_dict()
            item["last_run"] = next((run for run in runs if run["drive_id"] == drive.id), None)
            drives.append(item)
        return {
            "mode": "demo" if self.config.demo else "hardware",
            "version": "0.1.0",
            "connected": True,
            "drives": drives,
            "runs": runs,
            "settings": public_settings,
            "system": {
                "active_run_id": self.active_run_id,
                "release_in_progress": self.release_in_progress,
                "discovery_error": self.discovery_error,
                "tools": self.tools,
                "notification_error": self.notification_error,
                "station_error": self.station_error,
                "platform": self.capabilities.get("platform", "unknown"),
                "capabilities": self.capabilities,
            },
        }

    def _hardware_capabilities(self) -> dict:
        provider = getattr(self.hardware, "capabilities", None)
        if provider is None:
            return {
                "platform": "demo" if self.config.demo else "unknown",
                "can_test": True,
                "can_verify": bool(self.config.allow_destructive),
                "can_unmount": False,
                "can_eject": False,
                "tools": self.tools,
                "limitations": ["Safe release is unavailable on this hardware backend."],
            }
        capabilities = provider()
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if self.config.demo:
            capabilities = dict(capabilities)
            capabilities["can_test"] = True
            capabilities["can_verify"] = bool(self.config.allow_destructive)
        return capabilities

    def _validate_headless_startup(self) -> None:
        if not getattr(self.config, "headless", False):
            return
        notice = self.settings.value.get("notifications", {})
        try:
            notifications.validate_settings(notice)
        except (KeyError, ValueError) as error:
            raise RuntimeError(f"Headless notification configuration is invalid: {error}") from None
        provider = notice.get("provider", "none")
        configured = provider == "discord" and bool(notice.get("discord_webhook"))
        configured = configured or (
            provider == "telegram"
            and bool(notice.get("telegram_token") and notice.get("telegram_chat_id"))
        )
        if not notice.get("enabled") or not configured:
            raise RuntimeError(
                "Headless mode requires an enabled, fully configured Discord or Telegram provider."
            )
        missing = [
            label
            for key, label in (
                ("can_test", "drive testing"),
                ("can_eject", "safe eject"),
            )
            if not self.capabilities.get(key)
        ]
        if missing:
            limitations = "; ".join(self.capabilities.get("limitations") or [])
            suffix = f" {limitations}" if limitations else ""
            raise RuntimeError(
                f"Headless mode cannot start because {', '.join(missing)} is unavailable.{suffix}"
            )

    def publish(self):
        # Slow browsers retain only the latest snapshot rather than unbounded events.
        for queue in self.subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(True)

    async def scan(self):
        async with self.scan_lock:
            try:
                self.drives = await self.hardware.discover()
                self.discovery_error = None
                identities = {drive.identity for drive in self.drives}
                for identity in tuple(self.auto_attempted):
                    if identity in identities:
                        self.auto_absent_scans[identity] = 0
                        continue
                    absent = self.auto_absent_scans.get(identity, 0) + 1
                    self.auto_absent_scans[identity] = absent
                    if absent >= AUTO_DETACH_SCANS:
                        self.auto_attempted.discard(identity)
                        self.auto_absent_scans.pop(identity, None)
                if self.initial_scan:
                    # Restart does not repeat every historical intake or resume an erase.
                    self.auto_attempted.update(
                        run["drive"]["identity"] for run in self.store.runs()
                    )
                    self.initial_scan = False
                if self.settings.value["auto_test"] or getattr(self.config, "headless", False):
                    for drive in self.drives:
                        if drive.eligible and drive.identity not in self.auto_attempted:
                            try:
                                await self.enqueue(drive.id, "extended")
                                self.auto_attempted.add(drive.identity)
                                self.auto_absent_scans[drive.identity] = 0
                            except (SafetyError, ValueError):
                                pass
            except Exception:
                self.discovery_error = "Drive discovery failed. Check platform tools, device access, and service permissions."
            self.publish()

    async def supervise(self, name, loop):
        while True:
            try:
                await loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                message = f"Station {name} encountered an error and will retry. Check service logs."
                if name == "notifications":
                    self.notification_error = message
                else:
                    self.station_error = message
                self.publish()
                await asyncio.sleep(5)

    async def monitor(self):
        while True:
            await asyncio.sleep(self.config.scan_interval)
            await self.scan()

    async def enqueue(self, drive_id: str, profile: str, confirmation: str = "") -> dict:
        if profile not in PROFILES:
            raise ValueError("Choose quick, extended, or verify")
        if getattr(self.config, "headless", False) and profile != "extended":
            raise ValueError("Headless mode only permits the read-only extended intake profile")
        if not self.capabilities.get("can_test", True):
            limitations = "; ".join(self.capabilities.get("limitations") or [])
            suffix = f" {limitations}" if limitations else ""
            raise ValueError(f"Drive testing is unavailable on this station.{suffix}")
        if profile == "verify" and not self.capabilities.get("can_verify", True):
            raise ValueError("Write verification is unavailable on this station.")
        async with self.enqueue_lock:
            drive = next((drive for drive in self.drives if drive.id == drive_id), None)
            if drive is None:
                raise ValueError("Drive is no longer connected. Rescan and try again.")
            if any(
                run["drive_id"] == drive_id
                and (
                    run["status"] in {"queued", "running"}
                    or run.get("workflow_status") == "finishing"
                )
                for run in self.store.runs()
            ):
                raise ValueError("This drive already has a queued or running test")
            if profile == "verify":
                if not self.config.allow_destructive:
                    raise ValueError("Write verification is disabled in station configuration")
                if not drive.serial or confirmation != f"ERASE {drive.serial}":
                    raise ValueError(
                        "Confirm the exact drive serial using ERASE followed by its serial"
                    )
            drive = await self.hardware.validate(drive, destructive=profile == "verify")
            run = {
                "id": uuid.uuid4().hex,
                "drive_id": drive.id,
                "drive": drive.to_dict(),
                "profile": profile,
                "status": "queued",
                "phase": "queued",
                "progress": 0,
                "detail": "Waiting for the station",
                "created_at": now(),
                "started_at": None,
                "finished_at": None,
                "results": {},
                "logs": [],
                "workflow_status": "testing",
                "lifecycle": {
                    "notification_status": (
                        "pending"
                        if self.settings.value["notifications"].get("enabled")
                        else "disabled"
                    ),
                    "eject_status": "not_requested",
                    "eject_detail": "",
                },
            }
            self.store.save(run)
            self.queue.put_nowait(run["id"])
            self.publish()
            return run

    def record(self, run: dict, message: str | None = None):
        if message:
            run["logs"].append({"time": now(), "message": message})
            run["logs"] = run["logs"][-300:]
        self.store.save(run)
        self.publish()

    async def cancel(self, run_id: str):
        run = self.store.get(run_id)
        if run is None:
            raise ValueError("Test not found")
        if run["status"] in TERMINAL and run.get("workflow_status") != "finishing":
            return
        if self.active_run_id == run_id and self.active_task:
            self.active_task.cancel()
            # Wait until child processes and drive self-test have actually stopped.
            with contextlib.suppress(asyncio.CancelledError):
                await self.active_task
        else:
            run.update(
                status="cancelled", finished_at=now(), detail="Cancelled before testing started"
            )
            self.record(run, run["detail"])

    async def release(self, drive_id: str, action: str) -> dict:
        if action not in {"unmount", "eject"}:
            raise ValueError("Choose unmount or eject")
        capability = "can_unmount" if action == "unmount" else "can_eject"
        if not self.capabilities.get(capability):
            raise ValueError(f"Safe {action} is unavailable on this station")
        async with self.enqueue_lock:
            busy = any(
                run["status"] in {"queued", "running"} or run.get("workflow_status") == "finishing"
                for run in self.store.runs()
            )
            if busy or self.active_run_id is not None:
                raise ValueError("Wait for all testing and release work to finish before ejecting")
            drive = next((item for item in self.drives if item.id == drive_id), None)
            if drive is None:
                raise ValueError("Drive is no longer connected. Rescan and try again.")
            # Mounted inventory entries are intentionally ineligible for tests,
            # so their management adapter performs the fresh identity check while
            # allowing only the mount state needed for an explicit unmount.
            if not drive.mounted:
                drive = await self.hardware.validate(drive)
            self.release_in_progress = True
            self.publish()
            try:
                if action == "unmount":
                    result = await self.hardware.unmount(drive)
                else:
                    unmounted = (
                        await self.hardware.unmount(drive)
                        if drive.mounted
                        else {"status": "unmounted", "detail": "Drive was already unmounted."}
                    )
                    if unmounted.get("status") not in {"unmounted", "ejected"}:
                        result = unmounted
                    else:
                        result = await self.hardware.eject(drive)
            finally:
                self.release_in_progress = False
                self.publish()
        await self.scan()
        self.publish()
        return result

    async def worker(self):
        while True:
            run_id = await self.queue.get()
            try:
                run = self.store.get(run_id)
                if not run or run["status"] != "queued":
                    continue
                self.active_run_id = run_id
                self.active_task = asyncio.create_task(self.execute(run))
                try:
                    await self.active_task
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    # A malformed tool response must not kill the station worker.
                    await self.hardware.cancel()
                    latest = self.store.get(run_id)
                    if latest and latest["status"] not in TERMINAL:
                        latest.update(
                            status="incomplete",
                            finished_at=now(),
                            detail="Unexpected test error. Review service logs and retry.",
                        )
                        self.record(latest, latest["detail"])
                    self.station_error = "A test encountered an unexpected error; the station is still accepting jobs."
            finally:
                self.active_task = None
                self.active_run_id = None
                self.queue.task_done()
                self.publish()

    async def execute(self, run: dict):
        drive = Drive(**run["drive"])
        self.station_error = None
        run.update(status="running", started_at=now(), detail="Checking drive identity")
        self.record(run, "Test started")
        if self.settings.value["notifications"]["notify_started"]:
            self.notice(run, "started")
        steps = ["smart_before", "benchmark", "smart_after"]
        if run["profile"] != "quick":
            steps = ["smart_before", "self_test", "benchmark", "surface", "smart_after"]
        cancelled = False
        try:
            for index, phase in enumerate(steps):
                # Revalidate every phase, including queued jobs whose path may have changed.
                drive = await self.hardware.validate(drive, destructive=run["profile"] == "verify")
                run.update(
                    phase=phase,
                    progress=round(index / len(steps) * 100, 1),
                    detail=phase.replace("_", " ").capitalize(),
                )
                self.record(run, run["detail"])

                async def progress(percent: float, detail: str, index=index):
                    run.update(
                        progress=round(
                            (index + max(0, min(100, percent)) / 100) / len(steps) * 100, 1
                        ),
                        detail=detail,
                    )
                    self.record(run)

                if phase.startswith("smart"):
                    result = await self.hardware.smart(drive)
                elif phase == "self_test":
                    result = await self.hardware.self_test(drive, progress)
                elif phase == "benchmark":
                    result = await self.hardware.benchmark(drive, progress)
                else:
                    result = await self.hardware.surface(
                        drive, progress, destructive=run["profile"] == "verify"
                    )
                run["results"][phase] = result
                self.record(
                    run,
                    f"{phase.replace('_', ' ')}: {result.get('status', result.get('health', 'incomplete'))}",
                )
                if result.get("status", result.get("health")) == "failed":
                    break  # No reason to keep stressing a drive already reporting a failure.
            status, detail = verdict(run["results"])
            run.update(
                status=status,
                detail=detail,
                progress=100 if len(run["results"]) == len(steps) else run["progress"],
            )
        except asyncio.CancelledError:
            await self.hardware.cancel()
            run.update(status="cancelled", detail="Testing cancelled. Checks were not completed.")
            cancelled = True
        except SafetyError as error:
            run.update(status="incomplete", detail=f"Safety check stopped testing: {error}")
        except Exception:
            run.update(
                status="incomplete",
                detail="A test could not complete. Check tools, adapter compatibility, and service logs.",
            )
        finally:
            run["finished_at"] = now()
            auto_release = bool(self.settings.value.get("auto_eject"))
            run["workflow_status"] = "finishing" if auto_release and not cancelled else "complete"
            self.record(run, run["detail"])
            if auto_release and not cancelled:
                try:
                    await self._finish_headless(run, drive)
                except asyncio.CancelledError:
                    run["workflow_status"] = "interrupted"
                    run["lifecycle"]["eject_status"] = "failed"
                    run["lifecycle"]["eject_detail"] = (
                        "Safe release was cancelled before completion; inspect the drive manually."
                    )
                    self.record(run, run["lifecycle"]["eject_detail"])
                    raise
            else:
                self.notice(run, "finished")
        if cancelled:
            raise asyncio.CancelledError

    def notice(self, run: dict, event: str, message: str | None = None) -> str | None:
        settings = self.settings.value["notifications"]
        if settings["enabled"] and settings["provider"] != "none":
            notice_id = f"{run['id']}:{event}"
            self.store.enqueue_notice(
                notice_id, message or notifications.run_message(run, self.config.demo)
            )
            self.notice_event.set()
            return notice_id
        return None

    async def _finish_headless(self, run: dict, drive: Drive) -> None:
        lifecycle = run["lifecycle"]
        notifications_enabled = bool(
            self.settings.value["notifications"].get("enabled")
            and self.settings.value["notifications"].get("provider") != "none"
        )
        lifecycle.update(
            notification_status="pending" if notifications_enabled else "disabled",
            eject_status="pending",
            eject_detail="",
        )
        self.record(
            run, "Scan complete; waiting briefly for result notification before safe release."
        )
        message = notifications.run_message(run, self.config.demo)
        notice_id = self.notice(run, "finished", message)
        if notice_id is not None:
            await self._wait_for_notice(run, notice_id)

        try:
            unmount = (
                await self.hardware.unmount(drive)
                if drive.mounted
                else {"status": "unmounted", "detail": "Drive was already unmounted."}
            )
            if unmount.get("status") not in {"unmounted", "ejected"}:
                lifecycle["eject_status"] = unmount.get("status", "failed")
                lifecycle["eject_detail"] = unmount.get(
                    "detail", "The drive could not be safely unmounted."
                )
            else:
                released = await self.hardware.eject(drive)
                lifecycle["eject_status"] = released.get("status", "failed")
                lifecycle["eject_detail"] = released.get(
                    "detail", "The drive eject result was not reported."
                )
        except Exception:
            lifecycle["eject_status"] = "failed"
            lifecycle["eject_detail"] = (
                "Safe release encountered an unexpected error. Inspect the drive manually."
            )
        run["workflow_status"] = "complete"
        self.record(run, lifecycle["eject_detail"])
        if lifecycle["eject_status"] == "ejected":
            ready = notifications.run_message(run, self.config.demo)
            self.notice(run, "ready", ready)

    async def _wait_for_notice(self, run: dict, notice_id: str) -> None:
        deadline = time.monotonic() + max(
            0.0, float(getattr(self.config, "notification_wait_seconds", 30.0))
        )
        while True:
            state = self.store.notice_state(notice_id)
            if state and state["delivered"]:
                run["lifecycle"]["notification_status"] = "sent"
                self.record(run, "Completion notification delivered.")
                return
            if state and state["attempts"] >= 6:
                run["lifecycle"]["notification_status"] = "failed"
                self.record(run, "Completion notification reached its retry limit.")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                run["lifecycle"]["notification_status"] = "pending"
                self.record(
                    run,
                    "Completion notification is still pending; continuing safe release while the outbox retries.",
                )
                return
            await asyncio.sleep(min(0.1, remaining))

    def _update_notice_lifecycle(self, notice_id: str, status: str) -> None:
        run_id, separator, event = notice_id.partition(":")
        if not separator or event != "finished":
            return
        run = self.store.get(run_id)
        if run is None or "lifecycle" not in run:
            return
        run["lifecycle"]["notification_status"] = status
        self.store.save(run)

    async def notifier(self):
        while True:
            self.notice_event.clear()
            if self.settings.value["notifications"]["enabled"]:
                for notice_id, message, attempts in self.store.pending_notices(time.time()):
                    try:
                        await notifications.send(
                            copy.deepcopy(self.settings.value["notifications"]), message
                        )
                        self.store.delivered(notice_id)
                        self._update_notice_lifecycle(notice_id, "sent")
                        self.notification_error = None
                    except (notifications.NotificationError, ValueError) as error:
                        attempts += 1
                        self.store.notice_failed(
                            notice_id,
                            attempts,
                            time.time() + min(3600, 10 * 2**attempts),
                            str(error),
                        )
                        self.notification_error = str(error) + (
                            " Retry limit reached." if attempts >= 6 else ""
                        )
                        if attempts >= 6:
                            self._update_notice_lifecycle(notice_id, "failed")
                    self.publish()
            try:
                await asyncio.wait_for(self.notice_event.wait(), timeout=2)
            except TimeoutError:
                pass
