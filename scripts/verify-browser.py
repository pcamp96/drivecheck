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

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
TOKEN = "browser-test-token-not-a-real-station-secret"


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

                # Confirm a second browser sees the same live test without refreshing.
                second = context.new_page()
                second.goto(base)
                expect(second.locator("#app-view")).to_be_visible()
                page.get_by_role("button", name="Extended test", exact=True).click()
                expect(second.locator("#active-content")).to_be_visible()
                expect(page.locator("#run-list")).to_contain_text("Passed", timeout=15000)
                expect(second.locator("#run-list")).to_contain_text("Passed", timeout=15000)
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
                page.locator("#notification-provider").select_option("discord")
                page.locator("#discord-webhook").fill(
                    "https://discord.com/api/webhooks/123/synthetic_secret"
                )
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.locator("#discord-configured")).to_have_text("A webhook is saved.")
                expect(page.locator("#discord-webhook")).to_have_value("")
                page.locator("#forget-discord").click()
                expect(page.locator("#discord-configured")).to_have_text("No webhook saved.")
                page.locator("#notification-provider").select_option("telegram")
                page.locator("#telegram-token").fill("123:synthetic_secret")
                page.locator("#telegram-chat").fill("-987")
                page.get_by_role("button", name="Save settings", exact=True).click()
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
                "Browser verification passed: login, SSE in two tabs, completed report/export, cancellation, erase confirmation, settings/secret clearing, reconnect, mobile, logout."
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
