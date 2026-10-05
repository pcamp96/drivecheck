"""Explicitly approximate job timing, separate from reported drive progress."""

import math
from datetime import UTC, datetime, timedelta

LABELS = {
    "smart_before": "Initial SMART health",
    "self_test": "Drive self-test",
    "benchmark": "Read benchmark",
    "surface": "Full surface scan",
    "smart_after": "Final SMART health",
    "erase": "Drive erasure",
}


def steps_for(profile: str) -> list[str]:
    if profile in {"quick_erase", "secure_erase", "full_erase"}:
        return ["erase"]
    steps = ["smart_before", "self_test", "benchmark"]
    if profile != "quick":
        steps.append("surface")
    return [*steps, "smart_after"]


def positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def duration(seconds) -> str:
    if seconds is None:
        return "duration unavailable"
    minutes = max(1, math.ceil(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def build_estimate(drive, profile, info, read_mbps=None, *, clock=None) -> dict:
    clock = clock or datetime.now(UTC)
    durations = info.get("self_test_seconds", {})
    test_seconds = positive(durations.get("short" if profile == "quick" else "long"))
    notes = list(info.get("notes", []))
    speed = positive(read_mbps)
    provisional = speed is None
    speed = speed or 100.0
    # Start-of-disk benchmarks can overstate whole-drive throughput. This
    # allowance is a scheduling estimate, never evidence of checked sectors.
    surface = math.ceil(
        drive.size_bytes / (speed * 1_000_000) * 1.25 * (2 if profile == "verify" else 1)
    )
    values = {
        "smart_before": 5,
        "self_test": test_seconds,
        "benchmark": 30,
        "surface": surface,
        "smart_after": 5,
        "erase": None,
    }
    if "surface" in steps_for(profile):
        notes.append(
            "Full scan provisionally assumes 100 MB/s plus a 25% timing allowance; it updates after the benchmark."
            if provisional
            else f"Full scan uses a measured {speed:.1f} MB/s read sample plus a 25% timing allowance; speed can vary across the drive."
        )
    notes.append(
        "Drive self-test times are firmware recommendations, not deadlines. Progress may update in coarse increments."
    )
    phases = [
        {
            "phase": phase,
            "label": LABELS[phase],
            "seconds": values[phase],
            "source": "drive firmware recommendation"
            if phase == "self_test"
            else ("provisional throughput assumption" if provisional else "measured read sample")
            if phase == "surface"
            else "fixed benchmark duration"
            if phase == "benchmark"
            else "health-check allowance",
        }
        for phase in steps_for(profile)
    ]
    known = sum(item["seconds"] or 0 for item in phases)
    total = known if all(item["seconds"] is not None for item in phases) else None
    return {
        "profile": profile,
        "total_seconds": total,
        "minimum_seconds": known,
        "estimated_finish_at": (clock + timedelta(seconds=total)).isoformat()
        if total is not None
        else None,
        "phases": phases,
        "notes": notes,
        "complete": total is not None,
        "read_mbps": read_mbps,
        "generated_at": clock.isoformat(),
    }


def build_erase_estimate(drive, profile, plan, *, clock=None) -> dict:
    """Keep duration estimates separate from measured erase completion."""
    clock = clock or datetime.now(UTC)
    method = plan.get("method")
    seconds = None
    source = "duration unavailable"
    notes = []
    if method == "ata_secure_erase":
        minutes = positive(plan.get("estimated_minutes"))
        seconds = minutes * 60 if minutes is not None else None
        source = "drive firmware recommendation"
        notes.append(
            "Firmware erase does not report a completion percentage. The countdown is an estimate, not measured progress."
        )
        if seconds is None:
            notes.append(
                "The drive did not supply a usable firmware erase duration; ETA is unavailable."
            )
    elif method == "full_overwrite":
        seconds = math.ceil(drive.size_bytes * 2 / 100_000_000 * 1.25)
        source = "provisional write/read throughput assumption"
        notes.append(
            "Full erase provisionally assumes 100 MB/s for writing and read-back verification plus a 25% allowance; ETA updates from actual I/O."
        )
    elif method == "quick_format_exfat":
        minutes = positive(plan.get("estimated_minutes"))
        seconds = minutes * 60 if minutes is not None else None
        source = "quick-format operation allowance"
        notes.append(
            "Quick format percentage describes operation steps, not erased sectors. Old file contents may remain recoverable."
        )
    return {
        "profile": profile,
        "method": method,
        "total_seconds": seconds,
        "minimum_seconds": seconds,
        "estimated_finish_at": (clock + timedelta(seconds=seconds)).isoformat()
        if seconds is not None
        else None,
        "phases": [
            {"phase": "erase", "label": LABELS["erase"], "seconds": seconds, "source": source}
        ],
        "notes": notes,
        "complete": seconds is not None,
        "generated_at": clock.isoformat(),
    }


def live_timing(run: dict, *, clock=None) -> dict:
    clock = clock or datetime.now(UTC)
    task = run.get("task", {})
    elapsed_clock = clock
    terminal = run.get("status") not in {"running", "queued"}
    if terminal:
        try:
            elapsed_clock = datetime.fromisoformat(
                (run.get("finished_at") or task.get("last_update_at") or clock.isoformat()).replace(
                    "Z", "+00:00"
                )
            )
        except (ValueError, TypeError):
            pass
    phases = (run.get("estimate") or {}).get("phases", [])
    notes = list((run.get("estimate") or {}).get("notes", []))
    phase = task.get("phase", run.get("phase"))
    elapsed = 0
    try:
        started = datetime.fromisoformat(task["started_at"].replace("Z", "+00:00"))
        elapsed = max(0, (elapsed_clock - started).total_seconds())
    except (KeyError, ValueError, TypeError):
        started = clock
    current = next((item for item in phases if item["phase"] == phase), None)
    seconds = current.get("seconds") if current else None
    # During surface I/O use its actual reported completion to refine the rate.
    percent = positive(task.get("progress_percent"))
    full_erase = phase == "erase" and run.get("erase_method") == "full_overwrite"
    if (phase == "surface" or full_erase) and percent and 0 < percent < 100 and elapsed >= 30:
        observed_elapsed = elapsed
        try:
            observed = datetime.fromisoformat(task["last_update_at"].replace("Z", "+00:00"))
            observed_elapsed = max(0, (observed - started).total_seconds())
        except (KeyError, ValueError, TypeError):
            pass
        if observed_elapsed >= 30:
            seconds = observed_elapsed * 100 / percent
        prefix = "Full erase" if full_erase else "Full scan"
        notes = [note for note in notes if not note.startswith(prefix)]
        notes.append(
            "Full erase ETA uses the observed write/read verification rate; later regions may take longer."
            if full_erase
            else "Full scan ETA now uses the observed rate of this scan; unread regions may take longer."
        )
    overdue = seconds is not None and elapsed > seconds and run.get("status") == "running"
    phase_remaining = max(0, seconds - elapsed) if seconds is not None and not overdue else None
    future = []
    if current:
        index = phases.index(current)
        future = phases[index + 1 :]
    remaining = None
    if phase_remaining is not None and all(item["seconds"] is not None for item in future):
        remaining = phase_remaining + sum(item["seconds"] for item in future)
    if run.get("status") not in {"running", "queued"}:
        remaining = phase_remaining = None
        overdue = False
    if overdue:
        notes.append(
            "The current task exceeded its estimate. Completion time is unknown until the drive reports more progress."
        )
    return {
        "remaining_seconds": round(remaining) if remaining is not None else None,
        "estimated_finish_at": (clock + timedelta(seconds=remaining)).isoformat()
        if remaining is not None
        else None,
        "phase_remaining_seconds": round(phase_remaining) if phase_remaining is not None else None,
        "phase_estimated_finish_at": (started + timedelta(seconds=seconds)).isoformat()
        if seconds is not None and not overdue and not terminal
        else None,
        "phase_elapsed_seconds": round(elapsed),
        "overdue": overdue,
        "notes": notes,
        "calculated_at": clock.isoformat(),
    }
