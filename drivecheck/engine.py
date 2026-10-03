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
from drivecheck.hardware import Drive, Hardware, SafetyError
from drivecheck.storage import Store

TERMINAL = {"passed", "warning", "failed", "incomplete", "cancelled"}
PROFILES = {"quick", "extended", "verify"}


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
        self.hardware = hardware or Hardware(demo=config.demo)
        if config.demo:
            self.hardware.demo_step_seconds = config.demo_step_seconds
        self.drives: list[Drive] = []
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self.tasks: list[asyncio.Task] = []
        self.active_task: asyncio.Task | None = None
        self.active_run_id: str | None = None
        self.discovery_error: str | None = None
        self.notification_error: str | None = None
        self.station_error: str | None = None
        self.enqueue_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.auto_attempted: set[str] = set()
        self.initial_scan = True
        self.tools = {name: bool(shutil.which(name)) for name in ("lsblk", "smartctl", "fio")}

    async def start(self):
        self.store.recover()
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
            "settings": self.settings.public(),
            "system": {
                "active_run_id": self.active_run_id,
                "discovery_error": self.discovery_error,
                "tools": self.tools,
                "notification_error": self.notification_error,
                "station_error": self.station_error,
            },
        }

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
                self.auto_attempted.intersection_update(identities)
                if self.initial_scan:
                    # Restart does not repeat every historical intake or resume an erase.
                    self.auto_attempted.update(
                        run["drive"]["identity"] for run in self.store.runs()
                    )
                    self.initial_scan = False
                if self.settings.value["auto_test"]:
                    for drive in self.drives:
                        if drive.eligible and drive.identity not in self.auto_attempted:
                            try:
                                await self.enqueue(drive.id, "extended")
                                self.auto_attempted.add(drive.identity)
                            except (SafetyError, ValueError):
                                pass
            except Exception:
                self.discovery_error = (
                    "Drive discovery failed. Check Linux tools and service permissions."
                )
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
        async with self.enqueue_lock:
            drive = next((drive for drive in self.drives if drive.id == drive_id), None)
            if drive is None:
                raise ValueError("Drive is no longer connected. Rescan and try again.")
            if any(
                run["drive_id"] == drive_id and run["status"] in {"queued", "running"}
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
        if run["status"] in TERMINAL:
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
            raise
        except SafetyError as error:
            run.update(status="incomplete", detail=f"Safety check stopped testing: {error}")
        except Exception:
            run.update(
                status="incomplete",
                detail="A test could not complete. Check tools, adapter compatibility, and service logs.",
            )
        finally:
            run["finished_at"] = now()
            self.record(run, run["detail"])
            self.notice(run, "finished")

    def notice(self, run: dict, event: str):
        settings = self.settings.value["notifications"]
        if settings["enabled"] and settings["provider"] != "none":
            self.store.enqueue_notice(
                f"{run['id']}:{event}", notifications.run_message(run, self.config.demo)
            )

    async def notifier(self):
        while True:
            if self.settings.value["notifications"]["enabled"]:
                for notice_id, message, attempts in self.store.pending_notices(time.time()):
                    try:
                        await notifications.send(
                            copy.deepcopy(self.settings.value["notifications"]), message
                        )
                        self.store.delivered(notice_id)
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
                    self.publish()
            await asyncio.sleep(2)
