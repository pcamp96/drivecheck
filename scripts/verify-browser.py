#!/usr/bin/env python3
"""Real browser integration check against an isolated simulated station.

Uses installed Chromium (run `uv run --extra dev playwright install chromium`).
Never probes real devices or calls messaging providers.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

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
        "lba_of_first_error": 604616,
    }
    current = {
        "status": {"string": "Completed: read failure"},
        "type": {"string": "Extended offline"},
        "lifetime_hours": 4190,
        "lba_of_first_error": 622728,
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
                    "ata_smart_self_test_log": {
                        "standard": {"table": [current, historical]}
                    },
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
        env = {
            **os.environ,
            "DRIVECHECK_API_KEY": TOKEN,
            "DRIVECHECK_DEMO": "true",
            "DRIVECHECK_ALLOW_DESTRUCTIVE": "true",
            "DRIVECHECK_DEMO_STEP_SECONDS": "0.15",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "drivecheck", "--demo", "--data-dir", temp, "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + "/api/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
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
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
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
                expect(page.locator("#report")).to_contain_text("Short offline — Completed: read failure")
                expect(page.locator("#report")).to_contain_text("Failure location: LBA 604,616")
                expect(page.locator("#report")).to_contain_text("This self-test found a drive error")
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

                # Cancellation is exercised through the UI against a real running job.
                page.get_by_role("button", name="Extended test", exact=True).click()
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
                assert setting_cards.nth(1).bounding_box()["y"] > setting_cards.nth(0).bounding_box()["y"]
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
                "Browser verification passed: login, SSE, readable failed SMART/self-test evidence, reconnect/retest actions, report export, cancellation, erase confirmation, dedicated settings, secret clearing, mobile, logout."
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
