import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from drivecheck.config import DEFAULT_SETTINGS
from drivecheck.notifications import NotificationError, run_message, send, station_ready_message


def setting(provider):
    return {
        **DEFAULT_SETTINGS["notifications"],
        "provider": provider,
        "enabled": True,
        "discord_webhook": "https://discord.com/api/webhooks/123/test_secret",
        "telegram_token": "123:token_secret",
        "telegram_chat_id": "-987",
    }


def sample_run(**updates):
    run = {
        "id": "a" * 32,
        "status": "passed",
        "profile": "quick",
        "detail": "Selected checks passed.",
        "workflow_status": "complete",
        "drive": {"model": "IronWolf 4 TB", "serial": "ABC123"},
        "results": {"benchmark": {"status": "passed", "read_mbps": 209.4}},
        "lifecycle": {"eject_status": "pending"},
    }
    run.update(updates)
    return run


def test_started_message_is_short_and_describes_the_actual_profile():
    message = run_message(sample_run(status="running", profile="extended"), event="started")
    assert message.splitlines() == [
        "🔎 Extended test started",
        "",
        "IronWolf 4 TB",
        "Serial: ABC123",
        "",
        "Checking: SMART health, extended self-test, read speed, and a full read scan.",
    ]
    assert "Run:" not in message and "Selected checks" not in message


def test_started_message_labels_partial_duration_as_a_minimum():
    message = run_message(
        sample_run(
            status="running",
            profile="extended",
            estimate={"total_seconds": None, "minimum_seconds": 450},
            timing={"remaining_seconds": 450},
        ),
        event="started",
    )
    assert "Estimated time: at least 8 min; full timing unavailable." in message
    assert "Estimated time: about" not in message


def test_finished_and_ready_are_distinct_messages():
    run = sample_run(
        workflow_status="awaiting_action",
        lifecycle={"eject_status": "pending", "action_window_seconds": 180},
        extended_estimate={"total_seconds": 50_400},
    )
    finished = run_message(run, event="finished")
    assert finished.startswith("✅ Quick test passed\n\nIronWolf 4 TB\nSerial: ABC123")
    assert "Read speed: 209.4 MB/s." in finished
    assert "Scope: Short SMART test + read sample." in finished
    assert "Extended estimate: about 14 hr." in finished
    assert "Choose the next action below. Auto-eject in 3 min." in finished
    assert "Safe to remove" not in finished
    assert "Run:" not in finished

    run["lifecycle"] = {"eject_status": "ejected"}
    ready = run_message(run, event="ready")
    assert ready.splitlines() == [
        "🟢 Safe to remove",
        "",
        "IronWolf 4 TB",
        "Serial: ABC123",
        "",
        "Previous result: Quick test passed.",
        "You can unplug this drive now.",
    ]
    assert "Read speed" not in ready and "Scope:" not in ready


def test_finished_message_labels_partial_extended_estimate_as_a_minimum():
    run = sample_run(
        extended_estimate={"total_seconds": None, "minimum_seconds": 50_400},
    )
    message = run_message(run, event="finished")
    assert "Extended estimate: at least 14 hr; full timing unavailable." in message
    assert "Extended estimate: about" not in message


def test_failed_message_uses_bounded_human_reason_without_raw_json():
    run = sample_run(
        status="failed",
        detail='{"json_format_version":[1,0],"unsafe":"raw"}',
        results={
            "smart_before": {
                "status": "failed",
                "detail": "SMART reports failing health",
            }
        },
    )
    message = run_message(run, event="finished")
    assert message.startswith("❌ Quick test failed")
    assert "Reason: Initial SMART health failed: SMART reports failing health." in message
    assert "Scope:" not in message
    assert "json_format_version" not in message and "Run:" not in message


def test_release_failure_does_not_claim_drive_is_safe():
    run = sample_run(
        lifecycle={
            "eject_status": "failed",
            "eject_detail": "The enclosure still reports the device as busy.",
        }
    )
    message = run_message(run, event="release_failed")
    assert message.startswith("⛔ Eject failed")
    assert "The quick test passed, but the drive was not released." in message
    assert "Keep the drive connected" in message
    assert "Safe to remove" not in message and "unplug" not in message


def test_firmware_recovery_and_quick_format_warnings_are_never_hidden():
    recovery = sample_run(
        status="incomplete",
        profile="quick_erase",
        results={
            "erase": {
                "status": "incomplete",
                "method": "ata_secure_erase",
                "recovery_required": True,
            }
        },
    )
    message = run_message(recovery, event="finished")
    assert message.startswith("⚠️ Firmware erase needs recovery")
    assert "DO NOT power off or remove this drive." in message

    fallback = sample_run(
        profile="quick_erase",
        results={"erase": {"status": "passed", "method": "quick_format_exfat"}},
    )
    message = run_message(fallback, event="finished")
    assert "Method: Quick exFAT format." in message
    assert "Not a secure erase; old files may be recoverable." in message


def test_report_event_renders_result_even_after_eject():
    run = sample_run(lifecycle={"eject_status": "ejected"})
    message = run_message(run, event="report-requested")
    assert message.startswith("✅ Quick test passed")
    assert "Safe to remove" not in message
    assert message.endswith("Safe eject: Confirmed.")


def test_finished_message_marks_unconfirmed_release_without_claiming_safety():
    run = sample_run(
        workflow_status="interrupted",
        lifecycle={"eject_status": "interrupted"},
    )
    message = run_message(run, event="finished")
    assert "Safe eject was not confirmed." in message
    assert "Keep the drive connected and check the dashboard." in message
    assert "Safe to remove" not in message


def test_warning_message_includes_the_specific_warning_reason():
    run = sample_run(
        status="warning",
        results={
            "smart_before": {
                "status": "warning",
                "detail": "The drive reports two pending sectors",
            }
        },
    )
    message = run_message(run, event="finished")
    assert message.startswith("⚠️ Quick test completed with a warning")
    assert (
        "Warning: Initial SMART health was warning: The drive reports two pending sectors."
        in message
    )


def test_ready_station_message_is_an_invitation_not_a_diagnostic_dump():
    message = station_ready_message(
        booted_at="2026-10-03T22:04:41+00:00",
        platform="linux",
        mode="hardware",
        capabilities={"can_test": True, "can_eject": True, "limitations": []},
        queued=0,
    )
    assert message.splitlines() == [
        "🟢 DriveCheck is ready",
        "",
        "Dock a drive to start its read-only Quick test.",
        "Station: linux.",
    ]
    assert "Started:" not in message and "Queue:" not in message


def test_ready_station_does_not_promise_automatic_intake_when_disabled():
    message = station_ready_message(
        booted_at="2026-10-03T22:04:41+00:00",
        platform="linux",
        mode="hardware",
        capabilities={"can_test": True, "can_eject": True, "limitations": []},
        queued=0,
        auto_test=False,
    )
    assert "Open the dashboard to start a drive test." in message
    assert "Dock a drive" not in message


async def test_discord_confirmed_delivery_and_mentions_disabled():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "message"})

    await send(setting("discord"), "Drive passed @everyone", httpx.MockTransport(handler))
    assert captured[0].url.params["wait"] == "true"
    assert json.loads(captured[0].content)["allowed_mentions"] == {"parse": []}


async def test_telegram_payload_and_application_failure():
    def handler(request):
        body = json.loads(request.content)
        assert body["chat_id"] == "-987"
        assert body["text"] == "Drive passed"
        assert "parse_mode" not in body
        return httpx.Response(200, json={"ok": False, "description": "contains a token secret"})

    with pytest.raises(NotificationError, match="did not confirm") as error:
        await send(setting("telegram"), "Drive passed", httpx.MockTransport(handler))
    assert "secret" not in str(error.value)


async def test_telegram_reply_markup_is_sent_and_discord_ignores_it():
    keyboard = {
        "inline_keyboard": [
            [{"text": "Test again", "callback_data": "dc:" + "a" * 32 + ":extended"}]
        ]
    }
    captured = []

    def handler(request):
        captured.append(request)
        if request.url.path.endswith("/sendMessage"):
            assert json.loads(request.content)["reply_markup"] == keyboard
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        payload = json.loads(request.content)
        assert "reply_markup" not in payload
        return httpx.Response(200, json={"id": "message"})

    transport = httpx.MockTransport(handler)
    await send(setting("telegram"), "Failed", transport, reply_markup=keyboard)
    await send(setting("discord"), "Failed", transport, reply_markup=keyboard)
    assert len(captured) == 2


def test_telegram_user_id_must_be_positive_numeric():
    from drivecheck.notifications import validate_settings

    settings = setting("telegram")
    settings["telegram_user_id"] = "@owner"
    with pytest.raises(ValueError, match="positive numeric"):
        validate_settings(settings)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_provider_http_failures_safe_and_retryable(status):
    def handler(request):
        return httpx.Response(status, json={"secret": "do not log me"})

    with pytest.raises(NotificationError) as error:
        await send(setting("discord"), "message", httpx.MockTransport(handler))
    assert "test_secret" not in str(error.value)
    assert "do not log me" not in str(error.value)


async def test_network_errors_do_not_expose_token_url():
    def handler(request):
        raise httpx.ConnectError(f"connect to {request.url} failed", request=request)

    with pytest.raises(NotificationError) as error:
        await send(setting("telegram"), "message", httpx.MockTransport(handler))
    assert "token_secret" not in str(error.value)


@pytest.mark.parametrize("provider", ["telegram", "discord"])
async def test_readable_attachment_and_reason_delivered_in_one_request(provider):
    captured = []
    report = "DriveCheck report\nWhy it failed: read failure at LBA 622,728.\n"
    caption = "DriveCheck: failed\nWhy it failed: drive could not read its surface."
    keyboard = {
        "inline_keyboard": [[{"text": "Eject", "callback_data": "dc:" + "b" * 32 + ":eject"}]]
    }

    def handler(request):
        captured.append(request)
        mime = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
        )
        parts = {
            part.get_param("name", header="content-disposition"): part for part in mime.iter_parts()
        }
        field = "document" if provider == "telegram" else "files[0]"
        assert parts[field].get_filename() == "drivecheck-test.txt"
        assert parts[field].get_payload(decode=True).decode("utf-8") == report
        if provider == "telegram":
            assert request.url.path.endswith("/sendDocument")
            assert parts["caption"].get_payload(decode=True).decode() == caption
            assert parts["chat_id"].get_payload(decode=True).decode() == "-987"
            assert json.loads(parts["reply_markup"].get_payload(decode=True)) == keyboard
            assert "parse_mode" not in parts
            return httpx.Response(
                200, json={"ok": True, "result": {"document": {"file_name": "drivecheck-test.txt"}}}
            )
        payload = json.loads(parts["payload_json"].get_payload(decode=True))
        assert payload["content"] == caption
        assert payload["allowed_mentions"] == {"parse": []}
        assert request.url.params["wait"] == "true"
        return httpx.Response(
            200, json={"id": "confirmed", "attachments": [{"filename": "drivecheck-test.txt"}]}
        )

    await send(
        setting(provider),
        caption,
        httpx.MockTransport(handler),
        attachment={"filename": "drivecheck-test.txt", "text": report},
        reply_markup=keyboard,
    )
    assert len(captured) == 1


async def test_telegram_document_failure_remains_retryable_without_leaking_credentials():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "token_secret"})

    with pytest.raises(NotificationError, match="did not confirm") as error:
        await send(
            setting("telegram"),
            "Failed report",
            httpx.MockTransport(handler),
            attachment={"filename": "report.txt", "text": "Read failure"},
        )
    assert "token_secret" not in str(error.value)


async def test_invalid_attachment_name_never_calls_provider():
    def handler(request):
        raise AssertionError("Invalid file must not be sent")

    with pytest.raises(NotificationError, match="attachment is invalid"):
        await send(
            setting("telegram"),
            "message",
            httpx.MockTransport(handler),
            attachment={"filename": "../private.txt", "text": "report"},
        )


@pytest.mark.parametrize("provider", ["telegram", "discord"])
async def test_delivery_is_not_confirmed_if_provider_omits_attachment(provider):
    def handler(request):
        return httpx.Response(
            200, json={"ok": True, "result": {}, "id": "message", "attachments": []}
        )

    with pytest.raises(NotificationError, match="did not confirm the report attachment"):
        await send(
            setting(provider),
            "Failed report",
            httpx.MockTransport(handler),
            attachment={"filename": "report.txt", "text": "Read failure"},
        )


def test_firmware_start_explains_cancellation_is_unavailable():
    message = run_message(
        sample_run(status="running", profile="quick_erase", erase_method="ata_secure_erase"),
        event="started",
    )
    assert "Cancellation: Unavailable once firmware erase starts." in message
