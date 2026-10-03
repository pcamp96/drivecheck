"""Human-readable DriveCheck report rendering without exposing raw payloads."""

from typing import Any

PHASES = {
    "smart_before": "Initial SMART health",
    "self_test": "Drive self-test",
    "benchmark": "Read benchmark",
    "surface": "Full surface scan",
    "smart_after": "Final SMART health",
}


def _value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("string", "value", "raw"):
            if key in value and not isinstance(value[key], (dict, list)):
                return value[key]
        return None
    return value if not isinstance(value, (dict, list)) else None


def _text(value: Any, fallback: str = "Not reported") -> str:
    value = _value(value)
    if value is None or str(value).strip() == "":
        return fallback
    return str(value).strip()


def _sentence(value: Any) -> str:
    text = _text(value, "").rstrip(". ")
    return text + "." if text else ""


def _state(result: dict[str, Any]) -> str:
    return _text(result.get("status", result.get("health", "unknown")), "unknown").lower()


def _self_test_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    ata = raw.get("ata_smart_self_test_log")
    standard = ata.get("standard") if isinstance(ata, dict) else None
    candidates = [standard.get("table", []) if isinstance(standard, dict) else []]
    for key in ("nvme_self_test_log", "scsi_self_test_log"):
        log = raw.get(key)
        candidates.append(log.get("table", []) if isinstance(log, dict) else [])
    for rows in candidates:
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    return []


def _row_lba(row: dict[str, Any]) -> str | None:
    for key in ("lba", "lba_of_first_error", "first_error_lba", "failing_lba"):
        value = _value(row.get(key))
        if value is not None and str(value).strip() not in {"", "-", "0xffffffffffffffff"}:
            try:
                block = int(value)
                if 0 <= block < 2**64 - 1:
                    return f"{block:,}"
            except (ValueError, TypeError):
                continue
    return None


def failure_reason(run: dict[str, Any]) -> str:
    """Return the most specific current-run failure explanation available."""

    results = run.get("results") if isinstance(run.get("results"), dict) else {}
    for phase in PHASES:
        result = results.get(phase)
        if not isinstance(result, dict) or _state(result) != "failed":
            continue
        cause = _text(result.get("detail"), "")
        rows = _self_test_rows(result.get("raw")) if phase == "self_test" else []
        current = rows[0] if rows else {}
        if not cause and current:
            cause = _text(
                current.get("status", current.get("self_test_result", current.get("result"))), ""
            )
        if not cause:
            warnings = result.get("warnings")
            if isinstance(warnings, list) and warnings:
                cause = _text(warnings[0], "")
        cause = cause or "the check reported a failure"
        if "read failure" in cause.lower():
            cause = "the drive could not read part of its surface (read failure)"
        location = f" at LBA {_row_lba(current)}" if _row_lba(current) else ""
        return f"{PHASES[phase]} failed: {cause.rstrip('. ')}{location}."

    detail = _text(run.get("detail"), "")
    status = _text(run.get("status"), "unknown").lower()
    for phase in PHASES:
        result = results.get(phase)
        if not isinstance(result, dict):
            continue
        state = _state(result)
        if state in {"passed", "pass", "recorded", "unknown"}:
            continue
        cause = _text(result.get("detail"), "")
        warnings = result.get("warnings")
        if not cause and isinstance(warnings, list) and warnings:
            cause = _text(warnings[0], "")
        cause = cause or detail or "the check did not provide a conclusive result"
        return f"{PHASES[phase]} was {state}: {cause.rstrip('. ')}."
    if detail:
        return f"Run {status}: {detail.rstrip('. ')}."
    return f"Run {status}; no specific failure cause was reported."


def _capacity(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "Not reported"
    if size < 0:
        return "Not reported"
    units = ("bytes", "KB", "MB", "GB", "TB", "PB")
    amount = float(size)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if amount < 1000 or candidate == units[-1]:
            break
        amount /= 1000
    display = f"{int(amount)} {unit}" if unit == "bytes" else f"{amount:.2f} {unit}"
    return f"{display} ({size:,} bytes)"


def _self_test_line(row: dict[str, Any]) -> str:
    test_type = _text(row.get("type", row.get("self_test_code")), "Type not reported")
    status = _text(
        row.get("status", row.get("self_test_result", row.get("result"))),
        "Result not reported",
    )
    parts = [f"{test_type}: {status}"]
    lba = _row_lba(row)
    if lba:
        parts.append(f"failing LBA {lba}")
    hours = _value(row.get("lifetime_hours", row.get("power_on_hours")))
    if hours is not None:
        parts.append(f"lifetime hour {hours}")
    remaining = _value(row.get("remaining_percent"))
    if remaining is not None:
        parts.append(f"{remaining}% remaining when reported")
    return "; ".join(parts) + "."


def _ata_counters(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return []
    attributes = raw.get("ata_smart_attributes")
    table = attributes.get("table", []) if isinstance(attributes, dict) else []
    if not isinstance(table, list):
        return []
    labels = {
        5: "Reallocated sectors",
        187: "Reported uncorrectable errors",
        188: "Command timeout value",
        197: "Pending sectors",
        198: "Offline uncorrectable sectors",
        199: "Interface CRC errors",
    }
    counters = []
    for item in table:
        if not isinstance(item, dict) or item.get("id") not in labels:
            continue
        raw_value = item.get("raw", {})
        value = _value(raw_value)
        if value is None and isinstance(raw_value, dict):
            value = raw_value.get("value")
        if value is not None:
            counters.append(f"{labels[item['id']]}={value}")
    return counters


def _device_evidence(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return []
    lines = []
    overall = raw.get("smart_status")
    if isinstance(overall, dict) and isinstance(overall.get("passed"), bool):
        lines.append(
            "Overall SMART flag: "
            + ("passes at this snapshot" if overall["passed"] else "reports failure")
            + ". This is separate from the self-test result."
        )
    nvme = raw.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        fields = []
        for key, label in (
            ("critical_warning", "critical warning"),
            ("media_errors", "media errors"),
            ("num_err_log_entries", "error-log entries"),
            ("percentage_used", "percentage used"),
        ):
            value = _value(nvme.get(key))
            if value is not None:
                fields.append(f"{label}={value}")
        if fields:
            lines.append("NVMe counters reported: " + ", ".join(fields) + ".")
    scsi = raw.get("scsi_error_counter_log")
    if isinstance(scsi, dict):
        fields = []
        for operation in ("read", "write", "verify"):
            values = scsi.get(operation)
            if not isinstance(values, dict):
                continue
            count = _value(values.get("total_uncorrected_errors"))
            if count is not None:
                fields.append(f"{operation} uncorrected errors={count}")
        if fields:
            lines.append("SCSI counters reported: " + ", ".join(fields) + ".")
    return lines


def _phase_lines(phase: str, result: dict[str, Any]) -> list[str]:
    state = _state(result).replace("_", " ").title()
    lines = [f"  Status: {state}."]
    detail = _sentence(result.get("detail"))
    if detail:
        lines.append(f"  Detail: {detail}")
    warnings = result.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if _text(warning, ""):
                lines.append(f"  Warning: {_sentence(warning)}")

    raw = result.get("raw")
    rows = _self_test_rows(raw)
    if phase == "self_test" and rows:
        lines.append("  This run: " + _self_test_line(rows[0]))
        for row in rows[1:3]:
            lines.append("  Older recorded self-test (historical): " + _self_test_line(row))
    elif phase in {"smart_before", "smart_after"}:
        for row in rows[:3]:
            lines.append("  Recorded self-test history: " + _self_test_line(row))

    counters = _ata_counters(raw)
    if counters:
        lines.append("  Selected ATA counters reported: " + ", ".join(counters) + ".")
    lines.extend("  " + item for item in _device_evidence(raw))

    if phase == "benchmark":
        speed = _value(result.get("read_mbps"))
        if speed is not None:
            lines.append(f"  Sequential read: {speed} MB/s.")
    if phase == "surface":
        checked = _value(result.get("io_bytes", result.get("bytes_checked")))
        expected = _value(result.get("expected_bytes"))
        if checked is not None:
            coverage = f" of {expected}" if expected is not None else ""
            lines.append(f"  Coverage reported: {checked}{coverage} bytes.")
    return lines


def human_report(run: dict[str, Any], demo: bool = False) -> str:
    """Render a concise UTF-8 plain-text report suitable for an attachment."""

    drive = run.get("drive") if isinstance(run.get("drive"), dict) else {}
    results = run.get("results") if isinstance(run.get("results"), dict) else {}
    status = _text(run.get("status"), "unknown").upper()
    expected = ["smart_before", "benchmark", "smart_after"]
    if run.get("profile") in {"extended", "verify"}:
        expected = ["smart_before", "self_test", "benchmark", "surface", "smart_after"]

    lines = ["DriveCheck Report", "=" * 17, f"VERDICT: {status}"]
    if demo:
        lines.append("MODE: Simulation (no physical drive was tested)")
    if status not in {"PASSED", "PASS"}:
        lines.append("CAUSE: " + failure_reason(run))

    lines.extend(
        [
            "",
            "Drive and run",
            f"  Model: {_text(drive.get('model'))}",
            f"  Serial: {_text(drive.get('serial'), 'Unavailable')}",
            f"  Capacity: {_capacity(drive.get('size_bytes'))}",
            f"  Profile: {_text(run.get('profile')).title()}",
            f"  Run ID: {_text(run.get('id'))}",
            f"  Started: {_text(run.get('started_at'))}",
            f"  Finished: {_text(run.get('finished_at'))}",
            "",
            "Release and notification",
        ]
    )
    lifecycle = run.get("lifecycle") if isinstance(run.get("lifecycle"), dict) else {}
    lines.append(f"  Notification: {_text(lifecycle.get('notification_status'), 'Not reported')}.")
    lines.append(f"  Safe release: {_text(lifecycle.get('eject_status'), 'Not reported')}.")
    release_detail = _sentence(lifecycle.get("eject_detail"))
    if release_detail:
        lines.append(f"  Release detail: {release_detail}")

    lines.extend(["", "Step evidence"])
    for phase in expected:
        lines.append(f"{PHASES[phase]}:")
        result = results.get(phase)
        if not isinstance(result, dict):
            lines.append("  Not run or not recorded; no conclusion is available for this check.")
            continue
        lines.extend(_phase_lines(phase, result))

    lines.extend(
        [
            "",
            "Interpretation",
            "  Historical SMART records describe earlier tests and do not replace the result from this run.",
            "  Missing or unsupported checks mean coverage is incomplete; they are not passes.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["failure_reason", "human_report"]
