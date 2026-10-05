#!/usr/bin/env python3
"""Real browser integration check against an isolated simulated station.

Uses installed Chromium (run `uv run --extra dev playwright install chromium`).
Never probes real devices or calls messaging providers.
"""

import json
import socket
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from drivecheck.app import create_app
from drivecheck.config import Config
from drivecheck.storage import Store

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
TOKEN = "browser-test-token-not-a-real-station-secret"


def failed_report_fixture(drive: dict) -> dict:
    """Representative ATA failure based on a real report, with synthetic identity."""
    historical = {
        "status": {"string": "Completed: read failure"},
        "type": {"string": "Short offline"},
        "lifetime_hours": 4182,
        "lba": 604616,
    }
    current = {
        "status": {"string": "Completed: read failure"},
        "type": {"string": "Extended offline"},
        "lifetime_hours": 4190,
        "lba": 622728,
    }
    fixture_drive = {
        **drive,
        "id": "fixture-absent-drive",
        "path": "/dev/fixture-absent",
        "serial": "FIXTURE-FAILED-DRIVE",
        "identity": "fixture-absent-identity",
    }
    return {
        "id": "browser-failed-report",
        "drive_id": fixture_drive["id"],
        "drive": fixture_drive,
        "profile": "extended",
        "status": "failed",
        "workflow_status": "finished",
        "phase": "self_test",
        "progress": 36,
        "detail": "A drive self-test found a read failure.",
        "created_at": "2099-01-01T00:00:00+00:00",
        "started_at": "2099-01-01T00:00:01+00:00",
        "finished_at": "2099-01-01T00:06:00+00:00",
        "results": {
            "smart_before": {
                "health": "warning",
                "warnings": ["the self-test log contains errors"],
                "raw": {
                    "smart_status": {"passed": True},
                    "ata_smart_attributes": {
                        "table": [
                            {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 2}},
                            {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 1}},
                            {"id": 198, "name": "Offline_Uncorrectable", "raw": {"value": 3}},
                        ]
                    },
                    "ata_smart_self_test_log": {"standard": {"table": [historical]}},
                },
            },
            "self_test": {
                "status": "failed",
                "detail": "Completed: read failure",
                "raw": {
                    "smart_status": {"passed": True},
                    "ata_smart_self_test_log": {"standard": {"table": [current, historical]}},
                },
            },
        },
        "logs": [{"time": "2099-01-01T00:06:00+00:00", "message": "Self test: failed"}],
        "lifecycle": {
            "notification_status": "disabled",
            "eject_status": "ejected",
            "eject_detail": "Drive safely ejected after the failed test.",
        },
        "simulated": True,
    }


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="drivecheck-browser-") as temp:
        application = create_app(
            Config(
                data_dir=Path(temp),
                api_key=TOKEN,
                public_origin=base,
                demo=True,
                allow_destructive=True,
                demo_step_seconds=0.15,
            )
        )
        server = uvicorn.Server(
            uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error")
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + "/api/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if not server_thread.is_alive():
                    raise RuntimeError("Preview exited before startup")
                time.sleep(0.05)
            else:
                raise RuntimeError("Preview startup timed out")
            state = httpx.get(
                base + "/api/state", headers={"Authorization": f"Bearer {TOKEN}"}
            ).json()
            store = Store(Path(temp) / "drivecheck.sqlite3")
            store.save(failed_report_fixture(state["drives"][0]))
            store.close()
            access_link = application.state.engine.access_links.issue("browser-failed-report")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                link_context = browser.new_context(viewport={"width": 1100, "height": 800})
                link_page = link_context.new_page()
                link_page.goto(access_link)
                expect(link_page.locator("#app-view")).to_be_visible()
                expect(link_page.locator("#report")).to_contain_text("Completed: read failure")
                assert link_page.url == base + "/"
                assert "access=" not in link_page.url
                link_page.locator("#logout-button").click()
                expect(link_page.locator("#login-view")).to_be_visible()
                link_context.close()
                reused_context = browser.new_context(viewport={"width": 1100, "height": 800})
                reused_page = reused_context.new_page()
                reused_page.goto(access_link)
                expect(reused_page.locator("#login-error")).to_contain_text(
                    "expired or was already used"
                )
                assert reused_page.url == base + "/"
                reused_context.close()

                # RAID ownership is rendered and confirmed entirely from synthetic state.
                ownership_state = json.loads(json.dumps(state))
                base_drive = ownership_state["drives"][0]
                raid_drive = {
                    **base_drive,
                    "id": "fixture-inactive-raid",
                    "serial": "FIXTURE-RAID-1",
                    "eligible": False,
                    "reasons": ["device_in_use"],
                    "ownership": {
                        "take_control_available": True,
                        "detail": "Claimed by an inactive Linux RAID array.",
                        "arrays": [
                            {
                                "path": "/dev/md127",
                                "state": "inactive",
                                "members": ["/dev/sdz1", "/dev/sdy1"],
                            }
                        ],
                    },
                }
                mounted_drive = {
                    **base_drive,
                    "id": "fixture-mounted-raid",
                    "serial": "FIXTURE-MOUNTED",
                    "eligible": False,
                    "mounted": True,
                    "reasons": ["mounted", "device_in_use"],
                    "ownership": {
                        "take_control_available": False,
                        "detail": "Mounted RAID member remains in use.",
                        "arrays": [],
                    },
                }
                active_drive = {
                    **base_drive,
                    "id": "fixture-active-raid",
                    "serial": "FIXTURE-ACTIVE",
                    "eligible": False,
                    "reasons": ["device_in_use"],
                    "ownership": {
                        "take_control_available": False,
                        "detail": "Active RAID arrays cannot be released.",
                        "arrays": [
                            {"path": "/dev/md0", "state": "active", "members": ["/dev/sdx1"]}
                        ],
                    },
                }
                ownership_state["drives"] = [raid_drive, mounted_drive, active_drive]
                ownership_state["runs"] = []
                ownership_state["system"]["active_run_id"] = None
                take_calls = []

                mock_context = browser.new_context(viewport={"width": 1200, "height": 900})
                mock_page = mock_context.new_page()
                mock_page.goto(base)
                mock_page.locator("#token").fill(TOKEN)
                mock_page.get_by_role("button", name="Sign in", exact=True).click()
                expect(mock_page.locator("#app-view")).to_be_visible()
                mock_page.route(
                    "**/api/state",
                    lambda route: route.fulfill(status=200, json=ownership_state),
                )
                mock_page.route("**/api/events", lambda route: route.abort())

                def take_control(route):
                    take_calls.append(route.request.post_data_json)
                    if len(take_calls) == 1:
                        route.fulfill(status=409, json={"detail": "The RAID array became active."})
                    else:
                        route.fulfill(
                            status=200,
                            json={
                                "status": "released",
                                "detail": "Inactive RAID claim released; Quick test queued.",
                                "run": {"id": "fixture-quick", "profile": "quick"},
                            },
                        )

                mock_page.route("**/api/drives/fixture-inactive-raid/take-control", take_control)
                mock_page.reload()
                expect(mock_page.locator("#drive-list")).to_contain_text(
                    "Claimed by an inactive Linux RAID array."
                )
                expect(mock_page.locator("#drive-list")).to_contain_text(
                    "/dev/md127 · inactive · Members: /dev/sdz1, /dev/sdy1"
                )
                expect(mock_page.get_by_role("button", name="Take control")).to_have_count(1)
                expect(
                    mock_page.get_by_role("button", name="Quick erase", exact=True)
                ).to_have_count(0)
                expect(
                    mock_page.get_by_role("button", name="Full erase", exact=True)
                ).to_have_count(0)
                expect(
                    mock_page.locator(".drive-card", has_text="FIXTURE-MOUNTED").get_by_role(
                        "button", name="Take control"
                    )
                ).to_have_count(0)
                expect(
                    mock_page.locator(".drive-card", has_text="FIXTURE-ACTIVE").get_by_role(
                        "button", name="Take control"
                    )
                ).to_have_count(0)
                mock_page.get_by_role("button", name="Take control").click()
                expect(mock_page.locator("#take-control-dialog")).to_contain_text("FIXTURE-RAID-1")
                expect(mock_page.locator("#take-control-dialog")).to_contain_text(
                    "preserves the RAID metadata and files"
                )
                mock_page.locator("#take-control-confirmation").fill("TAKE CONTROL wrong")
                mock_page.locator("#take-control-submit").click()
                expect(mock_page.locator("#take-control-error")).to_contain_text("exactly")
                assert take_calls == []
                mock_page.locator("#take-control-confirmation").fill("TAKE CONTROL FIXTURE-RAID-1")
                mock_page.locator("#take-control-submit").click()
                expect(mock_page.locator("#take-control-error")).to_contain_text("became active")
                expect(mock_page.locator("#take-control-submit")).to_be_enabled()
                mock_page.locator("#take-control-confirmation").fill("TAKE CONTROL FIXTURE-RAID-1")
                expect(mock_page.locator("#take-control-error")).to_have_text("")
                mock_page.locator("#take-control-submit").click()
                expect(mock_page.locator("#take-control-dialog")).to_be_hidden()
                expect(mock_page.locator(".toast")).to_contain_text("Quick test queued")
                assert take_calls == [
                    {"confirmation": "TAKE CONTROL FIXTURE-RAID-1"},
                    {"confirmation": "TAKE CONTROL FIXTURE-RAID-1"},
                ]
                mock_context.close()

                erase_state = json.loads(json.dumps(state))
                erase_drive = {
                    **erase_state["drives"][0],
                    "id": "fixture-erase-drive",
                    "serial": "FIXTURE-ERASE-1",
                    "eligible": True,
                    "mounted": False,
                    "reasons": [],
                }
                erase_state["drives"] = [erase_drive]
                erase_state["runs"] = []
                erase_state["settings"]["allow_destructive"] = False
                erase_state["system"]["active_run_id"] = None
                erase_plan_calls = []
                erase_posts = []
                erase_attempts = {"quick_erase": 0, "full_erase": 0}

                erase_context = browser.new_context(viewport={"width": 1200, "height": 900})
                erase_page = erase_context.new_page()
                erase_page.goto(base)
                erase_page.locator("#token").fill(TOKEN)
                erase_page.get_by_role("button", name="Sign in", exact=True).click()
                expect(erase_page.locator("#app-view")).to_be_visible()
                erase_page.route(
                    "**/api/state", lambda route: route.fulfill(status=200, json=erase_state)
                )
                erase_page.route("**/api/events", lambda route: route.abort())

                def erase_plan(route):
                    erase_plan_calls.append(True)
                    quick = (
                        {
                            "available": True,
                            "method": "ata_secure_erase",
                            "secure": True,
                            "detail": "Drive firmware supports Secure Erase.",
                            "estimated_minutes": 4,
                        }
                        if len(erase_plan_calls) == 1
                        else {
                            "available": True,
                            "method": "quick_format_exfat",
                            "secure": False,
                            "detail": "Firmware erase is unavailable; Quick Format is the fallback.",
                            "estimated_minutes": 1,
                        }
                    )
                    route.fulfill(
                        status=200,
                        json={
                            "quick": quick,
                            "full": {
                                "available": True,
                                "method": "full_overwrite",
                                "detail": "Every addressable block will be overwritten.",
                            },
                        },
                    )

                def erase_submit(route):
                    body = route.request.post_data_json
                    erase_posts.append(body)
                    erase_attempts[body["profile"]] += 1
                    if erase_attempts[body["profile"]] == 1:
                        route.fulfill(
                            status=409,
                            json={"detail": "Erase method changed; review the new plan."},
                        )
                    else:
                        route.fulfill(
                            status=200,
                            json={
                                "status": "queued",
                                "detail": "Erase queued.",
                                "run": {
                                    "id": f"fixture-{body['profile']}",
                                    "profile": body["profile"],
                                },
                            },
                        )

                erase_page.route("**/api/drives/fixture-erase-drive/erase-plan", erase_plan)
                erase_page.route("**/api/drives/fixture-erase-drive/erase", erase_submit)
                erase_page.reload()
                expect(
                    erase_page.get_by_role("button", name="Quick erase", exact=True)
                ).to_be_disabled()
                expect(
                    erase_page.get_by_role("button", name="Quick erase", exact=True)
                ).to_have_attribute(
                    "title", "Drive erase is disabled in the station configuration."
                )
                erase_state["settings"]["allow_destructive"] = True
                erase_page.reload()
                erase_page.get_by_role("button", name="Quick erase", exact=True).click()
                expect(erase_page.locator("#erase-method")).to_contain_text(
                    "ATA firmware Secure Erase"
                )
                erase_page.locator("#erase-confirmation").fill("QUICK ERASE wrong")
                erase_page.locator("#erase-submit").click()
                expect(erase_page.locator("#erase-error")).to_contain_text("exactly")
                assert erase_posts == []
                erase_page.locator("#erase-confirmation").fill("QUICK ERASE FIXTURE-ERASE-1")
                erase_page.locator("#erase-submit").click()
                expect(erase_page.locator("#erase-error")).to_contain_text("method changed")
                erase_page.locator("#erase-confirmation").fill("QUICK ERASE FIXTURE-ERASE-1")
                erase_page.locator("#erase-submit").click()
                expect(erase_page.locator("#erase-dialog")).to_be_hidden()
                assert erase_posts[-1]["expected_method"] == "ata_secure_erase"

                erase_page.get_by_role("button", name="Quick erase", exact=True).click()
                expect(erase_page.locator("#erase-method")).to_contain_text("NOT SECURE")
                expect(erase_page.locator("#erase-method")).to_contain_text(
                    "old files may be recoverable"
                )
                erase_page.locator("#erase-cancel").click()

                erase_page.get_by_role("button", name="Full erase", exact=True).click()
                expect(erase_page.locator("#erase-method")).to_contain_text("Complete overwrite")
                erase_page.locator("#erase-confirmation").fill("FULL ERASE FIXTURE-ERASE-1")
                erase_page.locator("#erase-submit").click()
                expect(erase_page.locator("#erase-error")).to_contain_text("method changed")
                erase_page.locator("#erase-confirmation").fill("FULL ERASE FIXTURE-ERASE-1")
                erase_page.locator("#erase-submit").click()
                expect(erase_page.locator("#erase-dialog")).to_be_hidden()
                assert erase_posts[-1]["expected_method"] == "full_overwrite"
                assert erase_posts[-1]["confirmation"] == "FULL ERASE FIXTURE-ERASE-1"
                erase_state["runs"] = [
                    {
                        "id": "fixture-erase-report",
                        "drive_id": erase_drive["id"],
                        "drive": erase_drive,
                        "profile": "quick_erase",
                        "status": "warning",
                        "workflow_status": "complete",
                        "phase": "erase",
                        "progress": 100,
                        "detail": "Quick Format completed; recovery remains possible.",
                        "created_at": "2099-01-01T00:00:00+00:00",
                        "started_at": "2099-01-01T00:00:01+00:00",
                        "finished_at": "2099-01-01T00:01:00+00:00",
                        "results": {
                            "erase": {
                                "status": "warning",
                                "method": "quick_format_exfat",
                                "recovery_state": "Old file data may be recoverable",
                                "recovery_required": True,
                                "detail": "Only filesystem metadata was replaced.",
                            }
                        },
                        "logs": [],
                        "lifecycle": {},
                    }
                ]
                erase_page.reload()
                erase_page.locator('[data-run-id="fixture-erase-report"]').click()
                expect(erase_page.locator("#report")).to_contain_text("Actual erase method")
                expect(erase_page.locator("#report")).to_contain_text("Quick Format (exFAT)")
                expect(erase_page.locator("#report")).to_contain_text(
                    "Old files may still be recoverable"
                )
                erase_state["runs"] = [
                    {
                        **erase_state["runs"][0],
                        "id": "fixture-awaiting-erase",
                        "profile": "quick",
                        "status": "passed",
                        "workflow_status": "awaiting_action",
                        "lifecycle": {"action_deadline": "2099-01-01T00:03:00+00:00"},
                    }
                ]
                erase_state["system"]["active_run_id"] = "fixture-awaiting-erase"
                erase_page.reload()
                expect(
                    erase_page.get_by_role("button", name="Quick erase", exact=True)
                ).to_be_enabled()
                erase_page.get_by_role("button", name="Quick erase", exact=True).click()
                expect(erase_page.locator("#erase-deadline")).to_contain_text(
                    "Opening this dialog does not pause the timer"
                )
                erase_page.locator("#erase-cancel").click()
                erase_context.close()

                # Extended estimates are reviewed before submission, while active runs
                # distinguish overall progress from the drive's current task progress.
                timing_state = json.loads(json.dumps(state))
                timing_drive = {
                    **timing_state["drives"][0],
                    "id": "fixture-timing-drive",
                    "serial": "FIXTURE-TIMING-1",
                    "eligible": True,
                    "reasons": [],
                }
                timing_state["drives"] = [timing_drive]
                timing_state["runs"] = []
                timing_state["system"]["active_run_id"] = None
                estimate_plan = {
                    "profile": "extended",
                    "total_seconds": 28800,
                    "minimum_seconds": 27000,
                    "estimated_finish_at": (
                        datetime.now(UTC) + timedelta(hours=8)
                    ).isoformat(),
                    "phases": [
                        {
                            "phase": "self_test",
                            "label": "Extended drive self-test",
                            "seconds": 25200,
                            "source": "drive_firmware",
                        },
                        {
                            "phase": "surface",
                            "label": "Full surface scan",
                            "seconds": 3600,
                            "source": "measured_throughput",
                        },
                    ],
                    "notes": ["The drive firmware reports time in coarse increments."],
                    "complete": True,
                }
                timing_context = browser.new_context(viewport={"width": 1200, "height": 900})
                timing_page = timing_context.new_page()
                timing_page.goto(base)
                timing_page.locator("#token").fill(TOKEN)
                timing_page.get_by_role("button", name="Sign in", exact=True).click()
                expect(timing_page.locator("#app-view")).to_be_visible()
                timing_page.route(
                    "**/api/state", lambda route: route.fulfill(status=200, json=timing_state)
                )
                timing_page.route("**/api/events", lambda route: route.abort())
                timing_page.route(
                    "**/api/drives/fixture-timing-drive/test-estimate?profile=extended",
                    lambda route: route.fulfill(status=200, json=estimate_plan),
                )
                timing_page.reload()
                timing_page.get_by_role("button", name="Extended test", exact=True).click()
                expect(timing_page.locator("#estimate-dialog")).to_contain_text("8 hr 0 min")
                expect(timing_page.locator("#estimate-dialog")).to_contain_text(
                    "Extended drive self-test"
                )
                expect(timing_page.locator("#estimate-dialog")).to_contain_text(
                    "coarse increments"
                )
                timing_page.locator("#estimate-cancel").click()

                now = datetime.now(UTC)
                timing_state["runs"] = [
                    {
                        "id": "fixture-timed-run",
                        "drive_id": timing_drive["id"],
                        "drive": timing_drive,
                        "profile": "extended",
                        "status": "running",
                        "workflow_status": "running",
                        "phase": "self_test",
                        "progress": 22,
                        "detail": "The extended self-test is running.",
                        "started_at": (now - timedelta(minutes=8)).isoformat(),
                        "steps": [
                            "smart_before",
                            "self_test",
                            "benchmark",
                            "surface",
                            "smart_after",
                        ],
                        "results": {"smart_before": {"status": "passed"}},
                        "logs": [],
                        "lifecycle": {},
                        "task": {
                            "phase": "self_test",
                            "progress_percent": 10,
                            "started_at": (now - timedelta(minutes=5)).isoformat(),
                            "last_update_at": now.isoformat(),
                            "detail": "Drive firmware reports 90% remaining.",
                        },
                        "timing": {
                            "remaining_seconds": 7260,
                            "estimated_finish_at": (now + timedelta(seconds=7260)).isoformat(),
                            "phase_remaining_seconds": 3600,
                            "phase_estimated_finish_at": (now + timedelta(hours=1)).isoformat(),
                            "phase_elapsed_seconds": 300,
                            "overdue": False,
                            "notes": [],
                        },
                    }
                ]
                timing_state["system"]["active_run_id"] = "fixture-timed-run"
                timing_page.reload()
                expect(timing_page.locator("#active-percent")).to_have_text("22")
                expect(timing_page.locator("#task-percent")).to_have_text("10%")
                expect(timing_page.locator("#task-detail")).to_contain_text(
                    "Drive firmware reports 90% remaining"
                )
                expect(timing_page.locator("#run-remaining")).to_contain_text("hr")
                expect(timing_page.locator("#run-eta")).not_to_have_text("Estimate unavailable")

                timing_state["runs"][0]["timing"].update(
                    {
                        "remaining_seconds": 0,
                        "estimated_finish_at": (now - timedelta(minutes=1)).isoformat(),
                        "phase_remaining_seconds": None,
                        "phase_estimated_finish_at": None,
                        "overdue": True,
                    }
                )
                timing_page.reload()
                expect(timing_page.locator("#task-warning")).to_contain_text(
                    "Taking longer than estimated"
                )

                timing_state["runs"][0]["task"]["progress_percent"] = None
                timing_state["runs"][0]["timing"] = {
                    "remaining_seconds": None,
                    "estimated_finish_at": None,
                    "phase_remaining_seconds": None,
                    "phase_estimated_finish_at": None,
                    "phase_elapsed_seconds": 300,
                    "overdue": False,
                    "notes": [],
                }
                timing_page.reload()
                expect(timing_page.locator("#task-percent")).to_have_text(
                    "Progress unavailable"
                )
                expect(timing_page.locator("#run-remaining")).to_have_text(
                    "Estimate unavailable"
                )

                # ATA firmware erase reports no real percentage. Even if an older
                # station sends its synthetic 2%, show indeterminate progress while
                # retaining an approximate duration when the run has one.
                timed_run = json.loads(json.dumps(timing_state["runs"][0]))
                firmware_started = datetime.now(UTC) - timedelta(minutes=5)
                firmware_run = {
                    "id": "fixture-firmware-erase",
                    "drive_id": timing_drive["id"],
                    "drive": timing_drive,
                    "profile": "quick_erase",
                    "erase_method": "ata_secure_erase",
                    "status": "running",
                    "workflow_status": "testing",
                    "phase": "erase",
                    "progress": 2,
                    "detail": "ATA Secure Erase is running in drive firmware",
                    "started_at": firmware_started.isoformat(),
                    "steps": ["erase"],
                    "results": {},
                    "logs": [],
                    "lifecycle": {},
                    "task": {
                        "phase": "erase",
                        "progress_percent": 2,
                        "started_at": firmware_started.isoformat(),
                        "last_update_at": datetime.now(UTC).isoformat(),
                        "detail": "ATA Secure Erase is running in drive firmware",
                    },
                    "estimate": {
                        "total_seconds": 3600,
                        "phases": [
                            {"phase": "erase", "seconds": 3600, "source": "drive_firmware"}
                        ],
                        "notes": ["Drive firmware duration is approximate."],
                    },
                }
                timing_state["runs"] = [firmware_run]
                timing_state["system"]["active_run_id"] = firmware_run["id"]
                timing_page.reload()
                expect(timing_page.locator("#active-percent")).to_have_text("—")
                expect(timing_page.locator("#progress-bar").locator("..")).to_be_hidden()
                expect(timing_page.locator("#task-percent")).to_have_text(
                    "Progress unavailable"
                )
                expect(timing_page.locator("#task-detail")).to_contain_text(
                    "Firmware erase running; progress unavailable"
                )
                expect(timing_page.locator("#task-detail")).to_contain_text("Activity")
                expect(timing_page.locator("#run-remaining")).to_contain_text("About")
                expect(timing_page.locator("#run-eta")).to_contain_text("About")
                expect(timing_page.locator("#cancel-button")).to_be_hidden()
                timing_page.locator(
                    '#run-list [data-run-id="fixture-firmware-erase"]'
                ).click()
                expect(timing_page.locator("#report")).to_contain_text(
                    "Unavailable (firmware managed)"
                )

                # An explicit null ETA is an instruction from the backend, not a
                # missing field. Do not resurrect the original provisional estimate.
                firmware_run["timing"] = {
                    "remaining_seconds": 10,
                    "estimated_finish_at": None,
                    "phase_remaining_seconds": 10,
                    "phase_estimated_finish_at": None,
                    "phase_elapsed_seconds": 300,
                    "overdue": True,
                    "calculated_at": (datetime.now(UTC) - timedelta(seconds=20)).isoformat(),
                    "notes": [],
                }
                firmware_run["task"]["last_update_at"] = datetime.now(UTC).isoformat()
                timing_page.reload()
                expect(timing_page.locator("#run-remaining")).to_have_text(
                    "Estimate unavailable"
                )
                expect(timing_page.locator("#run-eta")).to_have_text("Estimate unavailable")
                expect(timing_page.locator("#task-warning")).to_contain_text(
                    "completion time is unavailable"
                )

                # Runs started by the previous backend have neither timing nor an
                # estimate. Keep those honest while still suppressing the fake 2%.
                firmware_run.pop("estimate")
                firmware_run.pop("timing")
                firmware_run["started_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
                firmware_run["task"]["started_at"] = firmware_run["started_at"]
                timing_page.reload()
                expect(timing_page.locator("#active-percent")).to_have_text("—")
                expect(timing_page.locator("#run-remaining")).to_have_text(
                    "Estimate unavailable"
                )

                firmware_run["status"] = "queued"
                firmware_run["phase"] = "queued"
                firmware_run["started_at"] = None
                firmware_run["task"] = {}
                timing_page.reload()
                expect(timing_page.locator("#active-detail")).to_have_text(
                    "Firmware erase queued; progress unavailable."
                )

                timing_state["runs"] = [timed_run]
                timing_state["system"]["active_run_id"] = timed_run["id"]

                timing_state["runs"][0].update(
                    {
                        "status": "passed",
                        "workflow_status": "awaiting_action",
                        "progress": 100,
                        "lifecycle": {
                            "action_deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
                        },
                    }
                )
                action_calls = []

                def expired_action(route):
                    action_calls.append(route.request.post_data_json)
                    route.fulfill(status=409, json={"detail": "The Quick choice window expired."})

                timing_page.route("**/api/runs/fixture-timed-run/action", expired_action)
                timing_page.route("**/api/runs/fixture-new-window/action", expired_action)
                timing_page.reload()
                timing_page.get_by_role("button", name="Run Extended test").click()
                expect(timing_page.locator("#estimate-dialog")).to_be_visible()
                expect(timing_page.locator("#action-countdown")).to_contain_text(
                    "ejects automatically"
                )
                first_window = json.loads(json.dumps(timing_state["runs"][0]))
                timing_state["runs"][0]["id"] = "fixture-new-window"
                timing_state["system"]["active_run_id"] = "fixture-new-window"
                timing_page.evaluate("refreshState()")
                timing_page.get_by_role("button", name="Start Extended", exact=True).click()
                expect(timing_page.locator("#estimate-error")).to_contain_text(
                    "different drive"
                )
                assert action_calls == []

                # A server-side expiry after a correctly bound confirmation remains
                # visible in the modal rather than silently selecting another run.
                timing_state["runs"] = [first_window]
                timing_state["system"]["active_run_id"] = "fixture-timed-run"
                timing_page.evaluate("refreshState()")
                timing_page.locator("#estimate-cancel").click()
                timing_page.get_by_role("button", name="Run Extended test").click()
                expect(timing_page.locator("#estimate-dialog")).to_be_visible()
                timing_page.get_by_role("button", name="Start Extended", exact=True).click()
                expect(timing_page.locator("#estimate-error")).to_contain_text(
                    "choice window expired"
                )
                assert action_calls == [{"action": "extended"}]
                timing_context.close()

                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base)
                page.locator("#token").fill(TOKEN)
                page.get_by_role("button", name="Sign in", exact=True).click()
                expect(page.locator("#app-view")).to_be_visible()
                expect(page.locator("#stream-state")).to_contain_text("Live updates connected")
                expect(page.locator("#drive-count")).to_have_text("1")
                expect(page.locator("#mode-flag")).to_contain_text("Simulated")

                # A failed report explains current state, historical evidence, and this run
                # without requiring the operator to read the underlying smartctl JSON.
                page.locator('.run-item[data-run-id="browser-failed-report"]').click()
                expect(page.locator("#report")).to_contain_text(
                    "SMART’s overall check passes now, but recorded errors need attention"
                )
                expect(page.locator("#report")).to_contain_text("Recorded self-test history")
                expect(page.locator("#report")).to_contain_text(
                    "Short offline — Completed: read failure"
                )
                expect(page.locator("#report")).to_contain_text("Failure location: LBA 604,616")
                expect(page.locator("#report")).to_contain_text(
                    "This self-test found a drive error"
                )
                expect(page.locator("#report")).to_contain_text(
                    "The drive could not read part of its surface"
                )
                expect(page.locator("#report")).to_contain_text(
                    "Extended offline — Completed: read failure"
                )
                expect(page.locator("#report")).to_contain_text("Failure location: LBA 622,728")
                expect(page.locator("#report")).to_contain_text(
                    "A passing SMART overall check is only one signal"
                )
                expect(page.locator(".technical-details").first).not_to_have_attribute("open", "")
                expect(
                    page.get_by_role("button", name="Reconnect / test again", exact=True)
                ).to_be_visible()
                page.screenshot(path=str(ARTIFACTS / "failed-report-desktop.png"), full_page=True)
                page.get_by_role("button", name="Reconnect / test again", exact=True).click()
                expect(page.locator(".report-action-status")).to_contain_text(
                    "Reconnect the drive or power-cycle its USB dock"
                )
                expect(
                    page.get_by_role("button", name="Reconnect / test again", exact=True)
                ).to_be_focused()

                # Confirm a second browser sees the same live test without refreshing.
                second = context.new_page()
                second.goto(base)
                expect(second.locator("#app-view")).to_be_visible()
                page.get_by_role("button", name="Extended test", exact=True).click()
                expect(page.locator("#estimate-dialog")).to_be_visible()
                expect(page.locator("#estimate-dialog")).to_contain_text("Estimated duration")
                page.get_by_role("button", name="Start Extended", exact=True).click()
                expect(second.locator("#active-content")).to_be_visible()
                expect(page.locator("#run-list")).to_contain_text("Passed", timeout=15000)
                expect(second.locator("#run-list")).to_contain_text("Passed", timeout=15000)
                expect(
                    page.get_by_role("button", name="Test again (read-only)", exact=True)
                ).to_be_visible()
                page.screenshot(path=str(ARTIFACTS / "dashboard-desktop.png"), full_page=True)
                with page.expect_download() as download_info:
                    page.get_by_role("link", name="Export JSON").click()
                download_info.value.save_as(ARTIFACTS / "browser-report.json")
                report = json.loads((ARTIFACTS / "browser-report.json").read_text())
                assert report["status"] == "passed" and report["simulated"] is True
                assert len(report["results"]) == 5
                with page.expect_download() as readable_download_info:
                    page.get_by_role("link", name="Export readable report", exact=True).click()
                readable_path = ARTIFACTS / "browser-report.txt"
                readable_download_info.value.save_as(readable_path)
                readable = readable_path.read_text(encoding="utf-8")
                assert "drivecheck" in readable.casefold()
                assert "passed" in readable.casefold()
                assert not readable.lstrip().startswith("{")
                assert '"results":' not in readable

                # Cancellation is exercised through the UI against a real running job.
                page.get_by_role("button", name="Extended test", exact=True).click()
                expect(page.locator("#estimate-dialog")).to_be_visible()
                page.get_by_role("button", name="Start Extended", exact=True).click()
                expect(page.locator("#cancel-button")).to_be_visible()
                page.locator("#cancel-button").click()
                expect(page.locator("#run-list")).to_contain_text("Cancelled")

                page.get_by_role("button", name="Erase + verify", exact=True).click()
                expect(page.locator("#verify-dialog")).to_be_visible()
                page.locator("#verify-confirmation").fill("ERASE wrong")
                page.locator("#verify-submit").click()
                expect(page.locator("#verify-error")).to_contain_text("exactly")
                page.locator("#verify-cancel").click()

                # Save both provider configurations disabled; no messages are sent.
                page.get_by_role("link", name="Settings", exact=True).click()
                expect(page.locator("#settings")).to_be_visible()
                expect(page.locator("#dashboard-view")).to_be_hidden()
                setting_cards = page.locator("#settings-form fieldset")
                assert (
                    setting_cards.nth(1).bounding_box()["y"]
                    > setting_cards.nth(0).bounding_box()["y"]
                )
                page.screenshot(path=str(ARTIFACTS / "settings-desktop.png"), full_page=True)
                page.locator("#notification-provider").select_option("discord")
                page.locator("#discord-webhook").fill(
                    "https://discord.com/api/webhooks/123/synthetic_secret"
                )
                page.get_by_role("button", name="Save notification settings", exact=True).click()
                expect(page.locator("#discord-configured")).to_have_text("A webhook is saved.")
                expect(page.locator("#discord-webhook")).to_have_value("")
                page.locator("#forget-discord").click()
                expect(page.locator("#discord-configured")).to_have_text("No webhook saved.")
                page.locator("#notification-provider").select_option("telegram")
                page.locator("#telegram-token").fill("123:synthetic_secret")
                page.locator("#telegram-chat").fill("-987")
                page.locator("#telegram-user").fill("123456")
                page.get_by_role("button", name="Save notification settings", exact=True).click()
                expect(page.locator("#telegram-configured")).to_have_text(
                    "Telegram credentials saved"
                )
                expect(page.locator("#telegram-configured")).to_have_class(
                    "credential-status is-saved"
                )
                expect(page.locator("#telegram-token")).to_have_value("")
                expect(page.locator("#telegram-token")).to_have_attribute(
                    "placeholder", "Token saved · leave blank to keep it"
                )
                expect(page.locator("#telegram-user")).to_have_value("123456")
                page.reload()
                expect(page.locator("#telegram-configured")).to_have_text(
                    "Telegram credentials saved"
                )
                page.locator("#forget-telegram").click()
                expect(page.locator("#telegram-configured")).to_have_text(
                    "Telegram credentials not configured"
                )
                expect(page.locator("#telegram-configured")).to_have_class("credential-status")

                # Automation switches persist immediately, independently of notification drafts.
                notifications_before = context.request.get(f"{base}/api/state").json()["settings"][
                    "notifications"
                ]
                page.locator("#telegram-token").fill("999:unsaved_notification_draft")
                page.locator("#auto-eject-delay").fill("181")
                page.locator("#auto-eject-delay").press("Tab")
                expect(page.locator("#automation-status")).to_have_text(
                    "Automation settings saved."
                )
                page.locator("#auto-eject-delay").fill("180")
                page.locator("#auto-eject-delay").press("Tab")
                expect(page.locator("#automation-status")).to_have_text(
                    "Automation settings saved."
                )
                page.locator('label[for="auto-eject"]').click()
                expect(page.locator("#automation-status")).to_have_text(
                    "Automation settings saved."
                )
                expect(second.locator("#auto-eject")).to_be_checked()
                expect(page.locator("#telegram-token")).to_have_value(
                    "999:unsaved_notification_draft"
                )
                assert (
                    context.request.get(f"{base}/api/state").json()["settings"]["notifications"]
                    == notifications_before
                )
                page.reload()
                expect(page.locator("#auto-eject")).to_be_checked()
                page.locator('label[for="auto-test"]').click()
                expect(page.locator("#automation-status")).to_have_text(
                    "Automation settings saved."
                )
                expect(second.locator("#auto-test")).to_be_checked()
                page.reload()
                expect(page.locator("#auto-test")).to_be_checked()
                saved = json.loads((Path(temp) / "settings.json").read_text())
                assert saved["auto_test"] and saved["auto_eject"]
                assert saved["auto_eject_delay_seconds"] == 180

                # Automatic intake performs Quick first, then offers the same timed
                # Extended/Eject choice shown in Telegram.
                application.state.engine.auto_attempted.clear()
                page.get_by_role("link", name="Workbench", exact=True).click()
                page.locator("#scan-button").click()
                expect(page.locator("#awaiting-action")).to_be_visible(timeout=15000)
                expect(page.locator("#action-countdown")).to_contain_text("ejects automatically")
                expect(page.get_by_role("button", name="Run Extended test")).to_be_visible()
                expect(page.get_by_role("button", name="Eject now")).to_be_visible()
                page.get_by_role("button", name="Run Extended test").click()
                expect(page.locator("#estimate-dialog")).to_be_visible()
                expect(page.locator("#action-countdown")).to_contain_text("ejects automatically")
                page.get_by_role("button", name="Start Extended", exact=True).click()
                expect(page.locator("#awaiting-action")).to_be_hidden(timeout=15000)
                expect(page.locator("#run-list")).to_contain_text("Extended", timeout=15000)

                page.get_by_role("link", name="Settings", exact=True).click()
                page.locator('label[for="auto-test"]').click()
                expect(page.locator("#auto-test")).to_be_enabled()
                page.locator('label[for="auto-eject"]').click()
                expect(page.locator("#auto-eject")).to_be_enabled()

                # Failed saves visibly roll back instead of pretending the switch persisted.
                page.route(
                    "**/api/settings",
                    lambda route: route.fulfill(
                        status=503, json={"detail": "Synthetic save failure"}
                    ),
                )
                page.locator('label[for="auto-eject"]').click()
                expect(page.locator("#automation-status")).to_contain_text(
                    "Could not save: Synthetic save failure"
                )
                expect(page.locator("#auto-eject")).not_to_be_checked()
                page.unroute("**/api/settings")

                # Reconnect establishes a fresh snapshot; mobile has no horizontal overflow.
                page.reload()
                expect(page.locator("#stream-state")).to_contain_text("Live updates connected")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(ARTIFACTS / "dashboard-mobile.png"), full_page=True)
                page.locator("#logout-button").click()
                expect(page.locator("#login-view")).to_be_visible()
                assert not errors, errors
                browser.close()
            print(
                "Browser verification passed: access links, RAID takeover, erase confirmations, Extended estimates, live task timing, Quick choices, reports, settings, mobile, logout."
            )
        finally:
            server.should_exit = True
            server_thread.join(timeout=10)


if __name__ == "__main__":
    main()
