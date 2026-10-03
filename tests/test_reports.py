from drivecheck.reports import failure_reason, human_report


def lucy_failure_run():
    return {
        "id": "run-lucy",
        "status": "failed",
        "detail": "A drive check failed.",
        "profile": "extended",
        "started_at": "2026-10-03T10:00:00Z",
        "finished_at": "2026-10-03T12:00:00Z",
        "drive": {
            "model": "WDC WD40EFRX",
            "serial": "WD-WCC4E5PX43ZV",
            "size_bytes": 4_000_787_030_016,
        },
        "lifecycle": {
            "notification_status": "sent",
            "eject_status": "ejected",
            "eject_detail": "USB drive powered off and is safe to remove",
        },
        "results": {
            "smart_before": {
                "health": "passed",
                "warnings": [],
                "raw": {
                    "smart_status": {"passed": True},
                    "ata_smart_attributes": {
                        "table": [
                            {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
                            {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}},
                            {"id": 198, "name": "Offline_Uncorrectable", "raw": {"value": 0}},
                        ]
                    },
                    "ata_smart_self_test_log": {
                        "standard": {
                            "table": [
                                {
                                    "type": {"string": "Short offline"},
                                    "status": {"string": "Completed: read failure"},
                                    "lba": 604616,
                                }
                            ]
                        }
                    },
                },
            },
            "self_test": {
                "status": "failed",
                "detail": "Completed: read failure",
                "raw": {
                    "private_token": "987654:SECRET",
                    "ata_smart_self_test_log": {
                        "standard": {
                            "table": [
                                {
                                    "type": {"string": "Extended offline"},
                                    "status": {"string": "Completed: read failure"},
                                    "passed": False,
                                    "remaining_percent": 90,
                                    "lifetime_hours": 63211,
                                    "lba": 622728,
                                },
                                {
                                    "type": {"string": "Short offline"},
                                    "status": {"string": "Completed: read failure"},
                                    "lba": 604616,
                                },
                            ]
                        }
                    },
                },
            },
        },
        "discord_webhook": "https://discord.com/api/webhooks/123/SECRET",
    }


def test_failure_reason_uses_current_self_test_lba_and_cause():
    reason = failure_reason(lucy_failure_run())
    assert (
        reason
        == "Drive self-test failed: the drive could not read part of its surface (read failure) at LBA 622,728."
    )


def test_human_report_distinguishes_current_and_historical_evidence():
    report = human_report(lucy_failure_run())
    assert "VERDICT: FAILED" in report
    assert "CAUSE: Drive self-test failed: the drive could not read part of its surface" in report
    assert "This run: Extended offline: Completed: read failure; failing LBA 622,728" in report
    assert "Older recorded self-test (historical): Short offline" in report
    assert "Recorded self-test history: Short offline" in report
    assert "Initial SMART health:\n  Status: Passed." in report
    assert "Read benchmark:\n  Not run or not recorded" in report
    assert "Full surface scan:\n  Not run or not recorded" in report
    assert "Reallocated sectors=0" in report
    assert "Overall SMART flag: passes at this snapshot" in report
    assert "987654:SECRET" not in report
    assert "discord.com" not in report
    assert '"ata_smart' not in report


def test_sparse_unsupported_report_does_not_claim_success():
    run = {
        "status": "incomplete",
        "detail": "SMART is unsupported through this USB bridge",
        "profile": "quick",
        "drive": {"model": "Unknown disk", "serial": "", "size_bytes": None},
        "results": {"smart_before": {"health": "unsupported", "warnings": []}},
    }
    report = human_report(run, demo=True)
    assert failure_reason(run) == (
        "Initial SMART health was unsupported: SMART is unsupported through this USB bridge."
    )
    assert "MODE: Simulation" in report
    assert "Serial: Unavailable" in report
    assert "Capacity: Not reported" in report
    assert "Status: Unsupported." in report
    assert report.count("Not run or not recorded") == 2
    assert "Missing or unsupported checks mean coverage is incomplete" in report
    assert "VERDICT: PASSED" not in report


def test_generic_benchmark_and_surface_evidence_is_readable():
    run = {
        "id": "run-ok",
        "status": "passed",
        "profile": "extended",
        "drive": {"model": "Disk", "serial": "SERIAL", "size_bytes": 1000},
        "results": {
            "smart_before": {"health": "passed", "raw": {}},
            "self_test": {"status": "passed", "detail": "Completed without error"},
            "benchmark": {"status": "passed", "read_mbps": 184.2},
            "surface": {"status": "passed", "io_bytes": 1000, "expected_bytes": 1000},
            "smart_after": {"health": "passed", "raw": {}},
        },
    }
    report = human_report(run)
    assert "Sequential read: 184.2 MB/s." in report
    assert "Coverage reported: 1000 of 1000 bytes." in report
    assert "CAUSE:" not in report


def test_null_vendor_fields_are_treated_as_missing_evidence():
    run = {
        "status": "failed",
        "profile": "extended",
        "results": {
            "self_test": {
                "status": "failed",
                "detail": "Unknown test failure",
                "raw": {
                    "ata_smart_self_test_log": None,
                    "ata_smart_attributes": None,
                    "nvme_self_test_log": None,
                },
            }
        },
    }
    report = human_report(run)
    assert "Unknown test failure" in report
    assert "VERDICT: FAILED" in report
