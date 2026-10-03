"""Single-drive scheduler and live state broadcaster."""

import asyncio
import contextlib
import copy
import hashlib
import secrets
import shutil
import time
import uuid
from datetime import UTC, datetime

from drivecheck import notifications, timing
from drivecheck.access import AccessLinks
from drivecheck.config import Config, Settings
from drivecheck.hardware import Drive, SafetyError, get_hardware
from drivecheck.storage import Store
from drivecheck.telegram import TelegramInterface

TERMINAL = {"passed", "warning", "failed", "incomplete", "cancelled"}
ERASE_PROFILES = {"quick_erase", "full_erase"}
PROFILES = {"quick", "extended", "verify", *ERASE_PROFILES}
AUTO_DETACH_SCANS = 2
BUSY_WORKFLOWS = {"finishing", "awaiting_action"}


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
        self.hardware = hardware or get_hardware(config.demo)
        if config.demo:
            self.hardware.demo_step_seconds = config.demo_step_seconds
        self.drives: list[Drive] = []
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self.tasks: list[asyncio.Task] = []
        self.active_task: asyncio.Task | None = None
        self.active_run_id: str | None = None
        self.release_in_progress = False
        self.stopping = False
        self.discovery_error: str | None = None
        self.notification_error: str | None = None
        self.station_error: str | None = None
        self.telegram_error: str | None = None
        self.access_links = AccessLinks(config)
        self.action_waits: dict[str, dict] = {}
        self.reconnect_contexts: dict[str, dict] = {}
        self.erase_intents: dict[str, dict] = {}
        self.enqueue_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.notice_event = asyncio.Event()
        self.auto_attempted: set[str] = set()
        self.auto_absent_scans: dict[str, int] = {}
        self.initial_scan = True
        self.tools = {name: bool(shutil.which(name)) for name in ("lsblk", "smartctl", "fio")}
        self.capabilities = self._hardware_capabilities()
        self.boot_id = uuid.uuid4().hex
        self.booted_at: str | None = None
        self.startup_notice_id = f"startup:ready:{self.boot_id}"
        self._startup_notice_queued = False
        if getattr(config, "headless", False):
            # Headless is an appliance workflow. Its two required behaviors are
            # runtime invariants rather than dashboard preferences.
            self.settings.value["auto_test"] = True
            self.settings.value["auto_eject"] = True

    async def start(self):
        self.booted_at = now()
        self.store.discard_pending_startup_notices()
        self.store.recover()
        self._validate_headless_startup()
        await self.scan()
        self.tasks = [
            asyncio.create_task(self.supervise("scheduler", self.worker)),
            asyncio.create_task(self.supervise("discovery", self.monitor)),
            asyncio.create_task(self.supervise("notifications", self.notifier)),
            asyncio.create_task(self.supervise("telegram", TelegramInterface(self).run)),
        ]
        if self.discovery_error is None:
            self._queue_startup_ready_notice()

    async def stop(self):
        self.stopping = True
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
        for run in runs:
            if run.get("task"):
                run["timing"] = timing.live_timing(run)
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
                "telegram_error": self.telegram_error,
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
                "can_erase": bool(self.config.allow_destructive),
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
            capabilities["can_erase"] = bool(self.config.allow_destructive)
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

    def _queue_startup_ready_notice(self) -> None:
        if self._startup_notice_queued:
            return
        settings = self.settings.value.get("notifications", {})
        if not settings.get("notify_ready", True):
            return
        if not settings.get("enabled") or settings.get("provider") == "none":
            return
        try:
            notifications.validate_settings(settings)
        except (KeyError, TypeError, ValueError):
            self.notification_error = (
                "Startup notification was not queued because notification settings are incomplete."
            )
            return
        message = notifications.station_ready_message(
            booted_at=self.booted_at or now(),
            platform=self.capabilities.get("platform", "unknown"),
            mode="demo" if self.config.demo else "hardware",
            capabilities=self.capabilities,
            queued=self.queue.qsize(),
            auto_test=self.settings.value["auto_test"] or getattr(self.config, "headless", False),
        )
        self.store.enqueue_notice(self.startup_notice_id, message)
        self._startup_notice_queued = True
        self.notice_event.set()

    async def scan(self):
        async with self.scan_lock:
            try:
                self.drives = await self.hardware.discover()
                self._mark_erase_recovery()
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
                                await self.enqueue(drive.id, "quick", automatic=True)
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

    def _recovery_pending(self, drive: Drive) -> bool:
        return (self.config.data_dir / "erase-recovery" / f"{drive.identity}.json").exists()

    def _mark_erase_recovery(self) -> None:
        for drive in self.drives:
            if self._recovery_pending(drive):
                drive.eligible = False
                if "erase_recovery_required" not in drive.reasons:
                    drive.reasons.append("erase_recovery_required")
                drive.ownership = {
                    "take_control_available": False,
                    "arrays": [],
                    "detail": "An interrupted firmware erase needs operator recovery. Do not power off the drive or start another job.",
                }

    def _read_sample(self, identity: str) -> float | None:
        for previous in self.store.runs():
            if previous.get("drive", {}).get("identity") != identity:
                continue
            result = previous.get("results", {}).get("benchmark", {})
            speed = timing.positive(result.get("read_mbps"))
            if result.get("status") == "passed" and speed:
                return speed
        return None

    async def test_estimate(self, drive_id: str, profile: str = "extended") -> dict:
        if profile not in {"quick", "extended", "verify"}:
            raise ValueError("Choose a read-only test estimate")
        drive = next((item for item in self.drives if item.id == drive_id), None)
        if drive is None or self._recovery_pending(drive):
            raise ValueError("Drive is missing or requires firmware recovery")
        info = await self.hardware.estimate_info(drive)
        return timing.build_estimate(drive, profile, info, self._read_sample(drive.identity))

    def _refresh_test_estimate(self, run: dict) -> None:
        if run["profile"] in ERASE_PROFILES:
            return
        raw = run.get("results", {}).get("smart_before", {}).get("raw", {})
        durations = self.hardware.self_test_recommended_seconds(raw)
        if not any(value is not None for value in durations.values()):
            previous = next(
                (p for p in run.get("estimate", {}).get("phases", []) if p["phase"] == "self_test"),
                {},
            )
            durations["short" if run["profile"] == "quick" else "long"] = previous.get("seconds")
        info = {"self_test_seconds": durations, "notes": []}
        speed = timing.positive(run.get("results", {}).get("benchmark", {}).get("read_mbps"))
        run["estimate"] = timing.build_estimate(
            Drive(**run["drive"]),
            run["profile"],
            info,
            speed or self._read_sample(run["drive"]["identity"]),
        )

    async def erase_plan(self, drive_id: str) -> dict:
        drive = next((item for item in self.drives if item.id == drive_id), None)
        if drive is None or self._recovery_pending(drive):
            raise ValueError("Drive is missing or needs firmware erase recovery")
        if not self.capabilities.get("can_erase"):
            raise ValueError("Erase is unavailable on this station")
        return await self.hardware.erase_plan(drive)

    def _erase_phrase(self, profile: str, serial: str) -> str:
        return ("QUICK ERASE " if profile == "quick_erase" else "FULL ERASE ") + serial

    def _telegram_authority(self, chat_id: int, user_id: int) -> str:
        notice = self.settings.value["notifications"]
        chat = str(notice.get("telegram_chat_id", ""))
        sender = str(notice.get("telegram_user_id", "")) or chat
        if (
            not notice.get("enabled")
            or notice.get("provider") != "telegram"
            or chat != str(chat_id)
            or sender != str(user_id)
            or user_id <= 0
        ):
            raise ValueError("Telegram erase authorization is no longer valid")
        return hashlib.sha256(str(notice.get("telegram_token", "")).encode()).hexdigest()

    async def begin_erase(self, run_id: str, profile: str, *, chat_id: int, user_id: int) -> dict:
        authority = self._telegram_authority(chat_id, user_id)
        context = self.reconnect_contexts.get(run_id)
        if context:
            self._context_drive(run_id)
        run = self.store.get(context["run_id"] if context else run_id)
        if profile not in ERASE_PROFILES or not run or not self.config.allow_destructive:
            raise ValueError("Manual erase is disabled or this action is invalid")
        drive = next(
            (item for item in self.drives if item.identity == run["drive"]["identity"]), None
        )
        expected = context["drive"] if context else run["drive"]
        if drive is None or drive.path != expected["path"] or not drive.eligible:
            raise ValueError("The original drive is no longer available for erasing")
        plan = (await self.erase_plan(drive.id))["quick" if profile == "quick_erase" else "full"]
        if not plan.get("available"):
            raise ValueError(plan.get("detail", "This erase method is unavailable"))
        if authority != self._telegram_authority(chat_id, user_id):
            raise ValueError("Telegram erase authorization changed")
        if context:
            self._context_drive(run_id)
        wait = self.action_waits.get(run_id)
        if run.get("workflow_status") in BUSY_WORKFLOWS and not wait:
            raise ValueError("Wait for the current job and safe release")
        deadline = min(
            time.monotonic() + 120,
            wait["deadline"] if wait else float("inf"),
            context["deadline"] if context else float("inf"),
        )
        if deadline <= time.monotonic() or (wait and wait["event"].is_set()):
            raise ValueError("The drive's action window has expired")
        # Bound memory and replace the sender's older pending confirmation.
        for key, intent in list(self.erase_intents.items()):
            if intent["deadline"] <= time.monotonic() or intent["principal"] == (chat_id, user_id):
                self.erase_intents.pop(key, None)
        if len(self.erase_intents) >= 100:
            self.erase_intents.pop(next(iter(self.erase_intents)))
        intent_id = secrets.token_urlsafe(24)
        self.erase_intents[intent_id] = {
            "drive": drive.to_dict(),
            "profile": profile,
            "method": plan["method"],
            "principal": (chat_id, user_id),
            "authority": authority,
            "deadline": deadline,
            "waiting_run": run_id if wait else None,
            "reconnect_context": run_id if context else None,
        }
        phrase = self._erase_phrase(profile, drive.serial)
        seconds = max(0, int(deadline - time.monotonic()))
        message = (
            f"⚠️ Confirm {profile.replace('_', ' ')}\n\n"
            f"{drive.model}\nSerial: {drive.serial}\nCapacity: {drive.size_bytes / 1e12:.2f} TB\n\n"
            f"Method: {plan['detail']}\nAll data on this drive will be lost."
        )
        if plan["method"] == "quick_format_exfat":
            message += (
                "\nQuick format is NOT secure erasure; old file contents may remain recoverable."
            )
        message += f"\n\nReply with exactly:\n{phrase}\n\nExpires in {seconds} seconds."
        if wait:
            message += " The automatic eject countdown continues."
        return {"intent_id": intent_id, "message": message, "expires_in": seconds}

    async def confirm_erase(
        self, intent_id: str, confirmation: str, *, chat_id: int, user_id: int
    ) -> dict:
        intent = self.erase_intents.get(intent_id)
        if not intent or intent["deadline"] <= time.monotonic():
            self.erase_intents.pop(intent_id, None)
            raise ValueError("Erase confirmation expired. Choose the action again.")
        if intent["principal"] != (chat_id, user_id) or intent[
            "authority"
        ] != self._telegram_authority(chat_id, user_id):
            raise ValueError("Erase confirmation is not authorized")
        if confirmation != self._erase_phrase(intent["profile"], intent["drive"]["serial"]):
            raise ValueError("The erase phrase must match exactly")
        response = await self.request_erase(
            intent["drive"]["id"], intent["profile"], confirmation, intent["method"], intent=intent
        )
        self.erase_intents.pop(intent_id, None)
        return response

    async def request_erase(
        self,
        drive_id: str,
        profile: str,
        confirmation: str,
        expected_method: str,
        *,
        intent: dict | None = None,
    ) -> dict:
        if profile not in ERASE_PROFILES or not self.config.allow_destructive:
            raise ValueError("Manual erase is disabled in station configuration")
        if self.stopping or not self.capabilities.get("can_erase"):
            raise ValueError("Erase is unavailable on this station")
        async with self.enqueue_lock:
            drive = next((item for item in self.drives if item.id == drive_id), None)
            if not drive or self._recovery_pending(drive):
                raise ValueError("Drive is missing or needs firmware recovery")
            if not drive.serial or confirmation != self._erase_phrase(profile, drive.serial):
                raise ValueError("The erase phrase must match the exact drive serial")
            await self.hardware.validate(drive, destructive=True)
            plan = (await self.hardware.erase_plan(drive))[
                "quick" if profile == "quick_erase" else "full"
            ]
            if not plan.get("available") or plan.get("method") != expected_method:
                raise ValueError(
                    "The erase method changed or is unavailable. Review a fresh confirmation."
                )
            if intent is not None:
                if intent.get("reconnect_context"):
                    self._context_drive(intent["reconnect_context"])
                original = intent["drive"]
                if (
                    intent["deadline"] <= time.monotonic()
                    or original["identity"] != drive.identity
                    or original["path"] != drive.path
                    or intent["authority"] != self._telegram_authority(*intent["principal"])
                ):
                    raise ValueError("Erase confirmation expired or the drive changed")
            wait_run = next(
                (
                    r
                    for r in self.store.runs()
                    if r["drive"]["identity"] == drive.identity
                    and r.get("workflow_status") == "awaiting_action"
                ),
                None,
            )
            if wait_run:
                wait = self.action_waits.get(wait_run["id"])
                if (
                    not wait
                    or wait["deadline"] <= time.monotonic()
                    or wait["event"].is_set()
                    or self.stopping
                ):
                    raise ValueError("This choice has expired or was already used")
                if intent is not None and intent["waiting_run"] != wait_run["id"]:
                    raise ValueError("The drive's original action window changed")
                wait.update(
                    choice=profile,
                    confirmation=confirmation,
                    expected_method=expected_method,
                    erase_intent=intent,
                )
                wait["event"].set()
                return {"status": "accepted", "detail": "Confirmed erase requested."}
            if intent is not None and intent["waiting_run"]:
                raise ValueError("The drive's original action window ended")
            run = await self._enqueue_locked(
                drive_id,
                profile,
                confirmation,
                expected_method=expected_method,
                erase_intent=intent,
            )
            return {"status": "queued", "detail": "Confirmed erase queued.", "run": run}

    async def enqueue(
        self, drive_id: str, profile: str, confirmation: str = "", *, automatic: bool = False
    ) -> dict:
        if self.stopping:
            raise ValueError("The station is stopping")
        if profile not in PROFILES:
            raise ValueError("Choose quick, extended, or verify")
        if automatic and profile != "quick":
            raise ValueError("Automatic intake only permits the read-only quick profile")
        if profile in ERASE_PROFILES:
            raise ValueError("Use the erase confirmation endpoint")
        if not self.capabilities.get("can_test", True):
            limitations = "; ".join(self.capabilities.get("limitations") or [])
            suffix = f" {limitations}" if limitations else ""
            raise ValueError(f"Drive testing is unavailable on this station.{suffix}")
        if profile == "verify" and not self.capabilities.get("can_verify", True):
            raise ValueError("Write verification is unavailable on this station.")
        async with self.enqueue_lock:
            return await self._enqueue_locked(drive_id, profile, confirmation, automatic=automatic)

    async def _enqueue_locked(
        self,
        drive_id: str,
        profile: str,
        confirmation: str = "",
        *,
        automatic: bool = False,
        replacing_run_id: str | None = None,
        expected_method: str | None = None,
        erase_intent: dict | None = None,
        reconnect_context: str | None = None,
    ) -> dict:
        if self.stopping:
            raise ValueError("The station is stopping")
        drive = next((drive for drive in self.drives if drive.id == drive_id), None)
        if drive is None:
            raise ValueError("Drive is no longer connected. Rescan and try again.")
        if automatic and drive.identity in self.auto_attempted:
            raise ValueError("This drive has already had its intake test")
        if any(
            run["drive_id"] == drive_id
            and run["id"] != replacing_run_id
            and (
                run["status"] in {"queued", "running"}
                or run.get("workflow_status") in BUSY_WORKFLOWS
            )
            for run in self.store.runs()
        ):
            raise ValueError("This drive already has a queued or running test")
        if self._recovery_pending(drive):
            raise SafetyError("This drive requires firmware erase recovery")
        if profile in ERASE_PROFILES:
            allowed = (
                {"ata_secure_erase", "quick_format_exfat"}
                if profile == "quick_erase"
                else {"full_overwrite"}
            )
            if (
                not self.config.allow_destructive
                or expected_method not in allowed
                or confirmation != self._erase_phrase(profile, drive.serial)
                or not drive.serial
            ):
                raise ValueError(
                    "Erase requires an enabled station and exact method/serial confirmation"
                )
        if profile == "verify":
            if not self.config.allow_destructive:
                raise ValueError("Write verification is disabled in station configuration")
            if not drive.serial or confirmation != f"ERASE {drive.serial}":
                raise ValueError(
                    "Confirm the exact drive serial using ERASE followed by its serial"
                )
        drive = await self.hardware.validate(
            drive, destructive=profile == "verify" or profile in ERASE_PROFILES
        )
        estimate = None
        if profile not in ERASE_PROFILES:
            estimate = await self.test_estimate(drive.id, profile)
        if self.stopping:
            raise ValueError("The station is stopping")
        if erase_intent is not None and (
            erase_intent["deadline"] <= time.monotonic()
            or erase_intent["authority"] != self._telegram_authority(*erase_intent["principal"])
        ):
            raise ValueError("Telegram erase confirmation expired or authorization changed")
        if reconnect_context:
            self._context_drive(reconnect_context)
        if erase_intent and erase_intent.get("reconnect_context"):
            self._context_drive(erase_intent["reconnect_context"])
        run = {
            "id": uuid.uuid4().hex,
            "drive_id": drive.id,
            "drive": drive.to_dict(),
            "profile": profile,
            "automatic": automatic,
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
                    "pending" if self.settings.value["notifications"].get("enabled") else "disabled"
                ),
                "eject_status": "not_requested",
                "eject_detail": "",
            },
        }
        run["steps"] = timing.steps_for(profile)
        if estimate is not None:
            run["estimate"] = estimate
        if profile in ERASE_PROFILES:
            run["erase_method"] = expected_method
        self.store.save(run)
        # Any new job supersedes previously offered Telegram controls, including
        # jobs queued from the dashboard or automatic discovery.
        self._forget_reconnect(drive.identity)
        if profile != "verify" and profile not in ERASE_PROFILES:
            # A manual read-only test or retry is also an intake attempt.
            # Record it under the queue lock so discovery cannot race a retry.
            self.auto_attempted.add(drive.identity)
        self.queue.put_nowait(run["id"])
        self.publish()
        return run

    def record(self, run: dict, message: str | None = None):
        if run.get("task"):
            run["timing"] = timing.live_timing(run)
        if message:
            run["logs"].append({"time": now(), "message": message})
            run["logs"] = run["logs"][-300:]
        self.store.save(run)
        self.publish()

    async def cancel(self, run_id: str):
        if run_id == self.active_run_id and getattr(self.hardware, "firmware_erase_active", False):
            raise ValueError(
                "Firmware erase is active or needs recovery; do not cancel or power off the drive"
            )
        run = self.store.get(run_id)
        if run is None:
            raise ValueError("Test not found")
        if run["status"] in TERMINAL and run.get("workflow_status") not in BUSY_WORKFLOWS:
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

    async def retest(self, run_id: str) -> dict:
        original = self.store.get(run_id)
        if original is None:
            raise ValueError("Test not found")
        if original["status"] not in TERMINAL or original.get("workflow_status") in BUSY_WORKFLOWS:
            raise ValueError("Wait for the current test and safe release to finish")
        await self.scan()
        if self.discovery_error:
            raise ValueError(self.discovery_error)
        identity = original["drive"]["identity"]
        matches = [drive for drive in self.drives if drive.identity == identity]
        if not matches:
            return {
                "status": "reconnect_required",
                "detail": "Reconnect the drive or power-cycle its USB dock, then try again. "
                "Safe eject removes the device; mounting a filesystem cannot reconnect it.",
            }
        if len(matches) != 1 or not matches[0].eligible:
            raise ValueError(
                "The drive is not uniquely identified and safe for an unmounted retest"
            )
        current = matches[0]
        for pending in self.store.runs():
            if pending["drive"]["identity"] == identity and (
                pending["status"] in {"queued", "running"}
                or pending.get("workflow_status") in BUSY_WORKFLOWS
            ):
                return {
                    "status": pending["status"],
                    "detail": "This drive already has a test or safe release in progress.",
                    "run": pending,
                }
        queued = await self.enqueue(current.id, "extended")
        return {"status": "queued", "detail": "Read-only extended retest queued.", "run": queued}

    def _forget_reconnect(self, identity: str) -> None:
        for key, context in list(self.reconnect_contexts.items()):
            if context["drive"]["identity"] == identity or context["deadline"] <= time.monotonic():
                self.reconnect_contexts.pop(key, None)

    def _context_drive(self, context_id: str) -> Drive:
        context = self.reconnect_contexts.get(context_id)
        if not context or context["deadline"] <= time.monotonic() or self.stopping:
            self.reconnect_contexts.pop(context_id, None)
            raise ValueError("These buttons expired or were already used. Tap Reconnect / rescan.")
        expected = context["drive"]
        matches = [drive for drive in self.drives if drive.identity == expected["identity"]]
        if len(matches) != 1:
            raise ValueError("The drive changed or disconnected. Tap Reconnect / rescan.")
        drive = matches[0]
        if (
            not drive.eligible
            or self._recovery_pending(drive)
            or any(getattr(drive, key) != expected[key] for key in ("path", "serial", "size_bytes"))
        ):
            raise ValueError(
                "The drive changed or is not safe for testing. Tap Reconnect / rescan."
            )
        return drive

    def _identity_busy(self, identity: str) -> bool:
        return self.release_in_progress or any(
            run["drive"]["identity"] == identity
            and (
                run["status"] in {"queued", "running"}
                or run.get("workflow_status") in BUSY_WORKFLOWS
            )
            for run in self.store.runs()
        )

    async def reconnect_run(self, run_id: str) -> dict:
        """Refresh inventory, never mount filesystems or reset USB controllers."""
        original = self.store.get(run_id)
        if not original:
            raise ValueError("Test not found")
        if self.stopping:
            raise ValueError("The station is stopping")
        await self.scan()
        identity = original["drive"]["identity"]
        self._forget_reconnect(identity)
        identity_lines = (
            notifications._clean_line(original["drive"].get("model"), limit=100)
            + "\nSerial: "
            + notifications._clean_line(original["drive"].get("serial"), limit=100)
        )
        markup = self.telegram_markup(f"{run_id}:reconnect")
        response = {"reply_markup": markup}
        if self.discovery_error:
            return {
                **response,
                "status": "blocked",
                "message": "⚠️ Rescan unavailable\n\n" + self.discovery_error,
            }
        matches = [drive for drive in self.drives if drive.identity == identity]
        if self._identity_busy(identity):
            return {
                **response,
                "status": "busy",
                "message": "🔎 Drive is busy\n\n"
                + identity_lines
                + "\n\nTesting or safe eject is already in progress.",
            }
        if not matches:
            return {
                **response,
                "status": "needs_reconnect",
                "message": "🔌 Reconnect the drive\n\n"
                + identity_lines
                + "\n\nPower-cycle the dock or reconnect USB, then tap Reconnect / rescan.",
            }
        if len(matches) != 1 or not matches[0].eligible or self._recovery_pending(matches[0]):
            return {
                **response,
                "status": "blocked",
                "message": "⚠️ Drive unavailable for testing\n\n"
                + identity_lines
                + "\n\nOpen the dashboard to review mounts, drive ownership, or recovery requirements.",
            }
        if not self.capabilities.get("can_test", True):
            return {
                **response,
                "status": "blocked",
                "message": "⚠️ Drive testing is unavailable on this station. Open the dashboard.",
            }
        current = matches[0]
        await self.hardware.validate(current)
        estimate = await self.test_estimate(current.id, "extended")
        async with self.enqueue_lock:
            # Revalidate the discovered snapshot after the asynchronous estimate.
            context_id = uuid.uuid4().hex
            self.reconnect_contexts[context_id] = {
                "run_id": run_id,
                "drive": current.to_dict(),
                "deadline": time.monotonic() + 300,
            }
            try:
                self._context_drive(context_id)
                if self._identity_busy(identity):
                    self.reconnect_contexts.pop(context_id, None)
                    return {
                        **response,
                        "status": "busy",
                        "message": "🔎 Testing or safe eject is already in progress.",
                    }
                await self.hardware.validate(current)
                self._context_drive(context_id)
                if self.stopping:
                    raise ValueError("The station is stopping")
            except BaseException:
                self.reconnect_contexts.pop(context_id, None)
                raise
            if len(self.reconnect_contexts) > 100:
                self.reconnect_contexts.pop(next(iter(self.reconnect_contexts)))
            message = (
                "🔌 Drive connected\n\n"
                + identity_lines
                + "\n\nChoose a test below. These buttons expire in 5 minutes."
            )
            total = estimate.get("total_seconds")
            minimum = estimate.get("minimum_seconds")
            if total:
                message += "\nExtended estimate: about " + timing.duration(total) + "."
            elif minimum:
                message += (
                    "\nExtended estimate: at least "
                    + timing.duration(minimum)
                    + "; full timing unavailable."
                )
            return {
                "status": "ready",
                "message": message,
                "reply_markup": self.telegram_markup(f"{context_id}:reconnected"),
            }

    async def take_control(self, drive_id: str, confirmation: str) -> dict:
        if self.stopping:
            raise ValueError("The station is stopping")
        if not self.capabilities.get("can_take_control"):
            raise ValueError("Releasing inactive RAID claims requires Linux, root, and mdadm")
        # Match discovery's lock order. Discovery must not enqueue an automatic
        # job between releasing the kernel claim and queuing the chosen Quick test.
        async with self.scan_lock:
            async with self.enqueue_lock:
                if (
                    self.active_run_id is not None
                    or self.release_in_progress
                    or any(
                        run["status"] in {"queued", "running"}
                        or run.get("workflow_status") in BUSY_WORKFLOWS
                        for run in self.store.runs()
                    )
                ):
                    raise ValueError("Wait for all testing and release work to finish first")
                drive = next((item for item in self.drives if item.id == drive_id), None)
                if drive is None:
                    raise ValueError("Drive is no longer connected. Rescan and try again.")
                if self._recovery_pending(drive) or getattr(
                    self.hardware, "firmware_erase_active", False
                ):
                    raise SafetyError(
                        "Firmware erase recovery is required; taking control is blocked"
                    )
                if not drive.serial or confirmation != f"TAKE CONTROL {drive.serial}":
                    raise ValueError(
                        "Confirm the exact serial using TAKE CONTROL followed by its serial"
                    )
                self.release_in_progress = True
                self.publish()
                try:
                    outcome = await self.hardware.take_control(drive, confirmation)
                    if outcome.get("status") != "released":
                        raise ValueError(
                            outcome.get("detail", "The RAID claim could not be released")
                        )
                    self.drives = await self.hardware.discover()
                    matches = [
                        current for current in self.drives if current.identity == drive.identity
                    ]
                    if (
                        len(matches) != 1
                        or not matches[0].eligible
                        or matches[0].path != drive.path
                    ):
                        raise SafetyError(
                            "The released drive could not be confirmed safe for testing"
                        )
                    if self.stopping or asyncio.current_task().cancelling():
                        raise asyncio.CancelledError
                    run = await self._enqueue_locked(matches[0].id, "quick")
                    # This explicit retry uses the same bounded intake lifecycle.
                    # It is not subject to the connection's previous attempt marker.
                    run.update(automatic=True, intake_source="take_control")
                    self.record(
                        run,
                        "User released an inactive RAID claim; RAID metadata preserved. Quick read-only intake queued.",
                    )
                    return {**outcome, "run": run}
                finally:
                    self.release_in_progress = False
                    self.publish()

    async def release(self, drive_id: str, action: str) -> dict:
        if action not in {"unmount", "eject"}:
            raise ValueError("Choose unmount or eject")
        capability = "can_unmount" if action == "unmount" else "can_eject"
        if not self.capabilities.get(capability):
            raise ValueError(f"Safe {action} is unavailable on this station")
        async with self.enqueue_lock:
            busy = any(
                run["status"] in {"queued", "running"}
                or run.get("workflow_status") in BUSY_WORKFLOWS
                for run in self.store.runs()
            )
            if busy or self.active_run_id is not None:
                raise ValueError("Wait for all testing and release work to finish before ejecting")
            drive = next((item for item in self.drives if item.id == drive_id), None)
            if drive is None:
                raise ValueError("Drive is no longer connected. Rescan and try again.")
            if self._recovery_pending(drive) or getattr(
                self.hardware, "firmware_erase_active", False
            ):
                raise SafetyError("Firmware erase recovery is required; safe release is blocked")
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
                    completed = next(
                        (
                            item
                            for item in self.store.runs()
                            if item["drive"]["identity"] == drive.identity
                            and item["status"] in TERMINAL
                        ),
                        None,
                    )
                    if completed is not None:
                        completed.setdefault("lifecycle", {}).update(
                            eject_status=result.get("status", "failed"),
                            eject_detail=result.get(
                                "detail", "Safe eject outcome was not reported."
                            ),
                        )
                        self.record(completed, completed["lifecycle"]["eject_detail"])
                        if result.get("status") == "ejected":
                            self.notice(
                                completed,
                                "ready",
                                notifications.run_message(
                                    completed, self.config.demo, event="ready"
                                ),
                            )
                        else:
                            self.notice(
                                completed,
                                "release_failed",
                                notifications.run_message(
                                    completed, self.config.demo, event="release_failed"
                                ),
                            )
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
        steps = run.get("steps") or timing.steps_for(run["profile"])
        run["steps"] = steps
        cancelled = False
        try:
            for index, phase in enumerate(steps):
                # Revalidate every phase, including queued jobs whose path may have changed.
                drive = await self.hardware.validate(
                    drive,
                    destructive=run["profile"] == "verify" or run["profile"] in ERASE_PROFILES,
                )
                run.update(
                    phase=phase,
                    progress=round(index / len(steps) * 100, 1),
                    detail=phase.replace("_", " ").capitalize(),
                )
                run["task"] = {
                    "phase": phase,
                    "progress_percent": 0,
                    "started_at": now(),
                    "last_update_at": now(),
                    "detail": run["detail"],
                }
                self.record(run, run["detail"])

                async def progress(percent: float, detail: str, index=index):
                    run.update(
                        progress=round(
                            (index + max(0, min(100, percent)) / 100) / len(steps) * 100, 1
                        ),
                        detail=detail,
                    )
                    unknown = (
                        "without a percentage" in detail or "Waiting for a new SMART" in detail
                    )
                    run["task"].update(
                        progress_percent=None if unknown else round(max(0, min(100, percent)), 4),
                        last_update_at=now(),
                        detail=detail,
                    )
                    self.record(run)

                if phase == "erase":
                    if not self.config.allow_destructive or self._recovery_pending(drive):
                        raise SafetyError("Erase is disabled or firmware recovery is required")
                    result = await self.hardware.erase(
                        drive,
                        run["profile"],
                        progress,
                        recovery_dir=self.config.data_dir / "erase-recovery",
                        expected_method=run["erase_method"],
                    )
                elif phase.startswith("smart"):
                    result = await self.hardware.smart(drive)
                elif phase == "self_test":
                    result = await (
                        self.hardware.short_self_test(drive, progress)
                        if run["profile"] == "quick"
                        else self.hardware.self_test(drive, progress)
                    )
                elif phase == "benchmark":
                    result = await self.hardware.benchmark(drive, progress)
                else:
                    result = await self.hardware.surface(
                        drive, progress, destructive=run["profile"] == "verify"
                    )
                run["results"][phase] = result
                run["task"].update(
                    progress_percent=100,
                    last_update_at=now(),
                    detail=result.get("detail", run["detail"]),
                )
                self._refresh_test_estimate(run)
                self.record(
                    run,
                    f"{phase.replace('_', ' ')}: {result.get('status', result.get('health', 'incomplete'))}",
                )
                if result.get("status", result.get("health")) == "failed":
                    break  # No reason to keep stressing a drive already reporting a failure.
            status, detail = verdict(run["results"])
            if run["profile"] in ERASE_PROFILES:
                detail = run["results"].get("erase", {}).get("detail", detail)
            run.update(
                status=status,
                detail=detail,
                progress=100 if len(run["results"]) == len(steps) else run["progress"],
            )
        except asyncio.CancelledError:
            await self.hardware.cancel()
            run.update(status="cancelled", detail="Testing cancelled. Checks were not completed.")
            if getattr(self.hardware, "firmware_erase_active", False) or self._recovery_pending(
                drive
            ):
                run["results"]["erase"] = {
                    "status": "incomplete",
                    "recovery_required": True,
                    "detail": "Firmware erase may still be running or the drive may be locked. Operator recovery required; do not power off.",
                }
                run.update(status="incomplete", detail=run["results"]["erase"]["detail"])
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
            recovery_required = bool(
                run["results"].get("erase", {}).get("recovery_required")
                or self._recovery_pending(drive)
            )
            auto_release = bool(self.settings.value.get("auto_eject")) and not recovery_required
            if recovery_required:
                run["lifecycle"].update(
                    eject_status="not_requested",
                    eject_detail="Firmware erase recovery is required; automatic eject is blocked.",
                )
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
            attachment = None
            if (event == "finished" and run["status"] == "failed") or event.startswith("report-"):
                from drivecheck.reports import human_report

                attachment = {
                    "filename": f"drivecheck-{run['id']}.txt",
                    "text": human_report(run, demo=self.config.demo),
                }
            content = message or notifications.run_message(run, self.config.demo, event=event)
            if attachment is not None:
                content += "\nReadable report attached."
            self.store.enqueue_notice(notice_id, content, attachment=attachment)
            self.notice_event.set()
            return notice_id
        return None

    def notify_report(self, run_id: str) -> dict:
        run = self.store.get(run_id)
        if run is None:
            raise ValueError("Test not found")
        if run["status"] not in TERMINAL or run.get("workflow_status") in BUSY_WORKFLOWS:
            raise ValueError("Wait for the test and safe release to finish")
        notice_id = self.notice(run, f"report-{uuid.uuid4().hex}")
        if notice_id is None:
            raise ValueError("Enable a notification provider before sending a report")
        return {
            "status": "queued",
            "notice_id": notice_id,
            "detail": "Readable report queued for delivery.",
        }

    def telegram_markup(self, notice_id: str) -> dict | None:
        settings = self.settings.value["notifications"]
        if settings.get("provider") != "telegram":
            return None
        run_id, _, event = notice_id.partition(":")
        context = self.reconnect_contexts.get(run_id)
        report_id = context["run_id"] if context else run_id
        run = self.store.get(report_id)
        rows = []
        chat = str(settings.get("telegram_chat_id", ""))
        sender = str(settings.get("telegram_user_id", ""))
        numeric_chat = chat.lstrip("-").isdigit()
        controls_authorized = numeric_chat and (
            (int(chat) > 0 and (not sender or sender == chat))
            or (int(chat) < 0 and sender.isdigit() and int(sender) > 0)
        )
        wait = self.action_waits.get(run_id)
        context_ready = False
        if context and event == "reconnected" and controls_authorized:
            try:
                self._context_drive(run_id)
                context_ready = not self._identity_busy(context["drive"]["identity"])
            except ValueError:
                pass
        if context_ready:
            rows.append(
                [
                    {"text": "Quick test", "callback_data": f"dc:{run_id}:quick"},
                    {"text": "Extended test", "callback_data": f"dc:{run_id}:extended"},
                ]
            )
        if (
            event == "finished"
            and controls_authorized
            and wait
            and wait["deadline"] > time.monotonic()
            and not wait["event"].is_set()
        ):
            rows.append(
                [
                    {"text": "Run Extended", "callback_data": f"dc:{run_id}:extended"},
                    {"text": "Eject now", "callback_data": f"dc:{run_id}:eject"},
                ]
            )
        current = next(
            (drive for drive in self.drives if run and drive.identity == run["drive"]["identity"]),
            None,
        )
        if (
            controls_authorized
            and self.config.allow_destructive
            and self.capabilities.get("can_erase")
            and current
            and current.eligible
            and run
            and run["status"] in TERMINAL
            and (
                context_ready
                or (
                    event == "finished"
                    and wait
                    and wait["deadline"] > time.monotonic()
                    and not wait["event"].is_set()
                )
            )
        ):
            rows.append(
                [
                    {"text": "Quick erase", "callback_data": f"dc:{run_id}:quick_erase"},
                    {"text": "Full erase", "callback_data": f"dc:{run_id}:full_erase"},
                ]
            )
        if (
            controls_authorized
            and run
            and run["status"] in TERMINAL
            and not (wait and wait["deadline"] > time.monotonic() and not wait["event"].is_set())
        ):
            rows.append(
                [{"text": "Reconnect / rescan", "callback_data": f"dc:{report_id}:reconnect"}]
            )
        # Only the authorized private recipient receives a bearer sign-in grant.
        # Group members and forwarded channel audiences must use normal login.
        private_authorized = numeric_chat and int(chat) > 0 and (not sender or sender == chat)
        link = (
            self.access_links.issue(report_id if run else "")
            if private_authorized
            else self.config.public_origin
        )
        if link:
            rows.append(
                [
                    {
                        "text": "Open dashboard"
                        if private_authorized
                        else "Open dashboard (sign in)",
                        "url": link,
                    }
                ]
            )
        return {"inline_keyboard": rows} if rows else None

    async def choose_action(self, run_id: str, action: str) -> dict:
        if action not in {"quick", "extended", "eject"}:
            raise ValueError("Choose Extended or Eject; erase requires dashboard confirmation")
        async with self.enqueue_lock:
            if run_id in self.reconnect_contexts:
                if action not in {"quick", "extended"}:
                    raise ValueError("Choose Quick or Extended from the refreshed controls")
                drive = self._context_drive(run_id)
                if self._identity_busy(drive.identity):
                    raise ValueError("This drive already has testing or safe eject in progress")
                queued = await self._enqueue_locked(drive.id, action, reconnect_context=run_id)
                return {
                    "status": "queued",
                    "detail": f"{action.title()} test queued.",
                    "run": queued,
                }
            wait = self.action_waits.get(run_id)
            run = self.store.get(run_id)
            if not wait or not run or run.get("workflow_status") != "awaiting_action":
                raise ValueError(
                    "This choice has expired. Open the dashboard and reconnect if needed."
                )
            if wait["deadline"] <= time.monotonic() or wait["event"].is_set():
                raise ValueError("This choice has expired or was already used")
            await self.hardware.validate(Drive(**run["drive"]))
            # Stop/cancel can invalidate the window during the hardware await.
            latest = self.store.get(run_id)
            if (
                self.stopping
                or self.action_waits.get(run_id) is not wait
                or not latest
                or latest.get("workflow_status") != "awaiting_action"
                or (self.active_task and self.active_task.cancelling())
            ):
                raise ValueError(
                    "This choice was interrupted. Open the dashboard for current status."
                )
            if wait["deadline"] <= time.monotonic():
                raise ValueError("This choice expired while checking the drive")
            wait["choice"] = action
            wait["event"].set()
            return {
                "status": "accepted",
                "detail": f"{action.title()} test requested."
                if action in {"quick", "extended"}
                else "Safe eject requested.",
            }

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
        delay = self.settings.value.get("auto_eject_delay_seconds", 180)
        can_wait = bool(
            run["profile"] == "quick" and run["status"] in {"passed", "warning"} and delay > 0
        )
        wait = None
        if can_wait:
            deadline = time.monotonic() + delay
            wait = {"event": asyncio.Event(), "deadline": deadline, "choice": None}
            self.action_waits[run["id"]] = wait
            run["workflow_status"] = "awaiting_action"
            lifecycle["action_deadline"] = datetime.fromtimestamp(
                time.time() + delay, UTC
            ).isoformat()
            lifecycle["action_window_seconds"] = delay
            lifecycle["eject_detail"] = (
                f"Quick finished. Choose Extended or Eject within {delay} seconds; otherwise the drive ejects automatically."
            )
        if can_wait:
            raw = run.get("results", {}).get("smart_before", {}).get("raw", {})
            info = {
                "self_test_seconds": self.hardware.self_test_recommended_seconds(raw),
                "notes": [],
            }
            extended_estimate = timing.build_estimate(
                drive,
                "extended",
                info,
                run.get("results", {}).get("benchmark", {}).get("read_mbps"),
            )
            run["extended_estimate"] = extended_estimate
        self.record(
            run,
            lifecycle["eject_detail"]
            or "Scan complete; waiting briefly for result notification before safe release.",
        )
        message = notifications.run_message(run, self.config.demo, event="finished")
        notice_id = self.notice(run, "finished", message)
        try:
            if notice_id is not None:
                await self._wait_for_notice(run, notice_id)
            if wait:
                try:
                    async with asyncio.timeout(max(0, wait["deadline"] - time.monotonic())):
                        await wait["event"].wait()
                except TimeoutError:
                    pass
                async with self.enqueue_lock:
                    # Timeout and click compete for one lock, so only one can win.
                    if self.stopping or asyncio.current_task().cancelling():
                        raise asyncio.CancelledError
                    if wait["choice"] in {"quick", "extended", *ERASE_PROFILES}:
                        try:
                            current = await self.hardware.validate(drive)
                            followup = await self._enqueue_locked(
                                current.id,
                                wait["choice"],
                                wait.get("confirmation", ""),
                                replacing_run_id=run["id"],
                                expected_method=wait.get("expected_method"),
                                erase_intent=wait.get("erase_intent"),
                            )
                        except Exception:
                            lifecycle["eject_status"] = "pending"
                            lifecycle["eject_detail"] = (
                                "The selected follow-up could not start safely; attempting safe release."
                            )
                        else:
                            # The continuation is now durably saved and queued while
                            # the original reservation still held the drive.
                            run["workflow_status"] = "complete"
                            lifecycle["eject_status"] = "not_requested"
                            lifecycle["eject_detail"] = (
                                "Drive retained for the requested follow-up job."
                            )
                            lifecycle["followup_run_id"] = followup["id"]
                            self.record(run, lifecycle["eject_detail"])
                            self.notice(
                                run,
                                "extended",
                                f"⏳ {followup['profile'].replace('_', ' ').title()} queued\n\n"
                                + drive.model
                                + "\nSerial: "
                                + drive.serial,
                            )
                            return
                    run["workflow_status"] = "finishing"
                    self.record(run, "Action window ended; safely releasing the drive.")
        finally:
            self.action_waits.pop(run["id"], None)

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
            ready = notifications.run_message(run, self.config.demo, event="ready")
            self.notice(run, "ready", ready)
        else:
            self.notice(
                run,
                "release_failed",
                notifications.run_message(run, self.config.demo, event="release_failed"),
            )

    async def _wait_for_notice(self, run: dict, notice_id: str) -> None:
        deadline = time.monotonic() + max(
            0.0, float(getattr(self.config, "notification_wait_seconds", 30.0))
        )
        wait = self.action_waits.get(run["id"])
        if wait:
            deadline = min(deadline, time.monotonic() + max(0, wait["deadline"] - time.monotonic()))
        while True:
            if wait and wait["event"].is_set():
                return
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

    def notice_message(self, notice_id: str, message: str) -> str:
        run_id, _, event = notice_id.partition(":")
        run = self.store.get(run_id)
        wait = self.action_waits.get(run_id)
        choice_available = (
            wait and wait["deadline"] > time.monotonic() and not wait["event"].is_set()
        )
        if event == "finished" and run and choice_available:
            current = copy.deepcopy(run)
            current["lifecycle"]["action_window_seconds"] = max(
                1, int(wait["deadline"] - time.monotonic())
            )
            return notifications.run_message(current, self.config.demo, event="finished")
        if (
            event == "finished"
            and run
            and run.get("lifecycle", {}).get("action_deadline")
            and (run.get("workflow_status") != "awaiting_action" or not choice_available)
        ):
            return (
                notifications.run_message(run, self.config.demo, event="finished")
                + "\n\nThe action window has ended. Tap Reconnect / rescan to test again."
            )
        return message

    async def notifier(self):
        while True:
            self.notice_event.clear()
            if self.settings.value["notifications"]["enabled"]:
                for notice_id, message, attempts in self.store.pending_notices(time.time()):
                    try:
                        attachment = self.store.notice_attachment(notice_id)
                        markup = self.telegram_markup(notice_id)
                        await notifications.send(
                            copy.deepcopy(self.settings.value["notifications"]),
                            self.notice_message(notice_id, message),
                            **({"attachment": attachment} if attachment is not None else {}),
                            **({"reply_markup": markup} if markup is not None else {}),
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
                async with asyncio.timeout(2):
                    await self.notice_event.wait()
            except TimeoutError:
                pass
