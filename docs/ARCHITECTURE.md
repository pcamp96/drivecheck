# Implementation contract

Working name: DriveCheck. Native systemd service on Raspberry Pi OS Bookworm or
newer (64-bit, Python 3.11+). Portable demo mode never probes host hardware.
One application process; one test job at a time; SQLite persists snapshots.

## Hardware interface (hardware.py)
`Drive` is a dataclass with fields: id, path, model, serial, size_bytes,
transport, eligible, reasons (list[str]), identity (str), mounted (bool).
`drive.to_dict()` returns all fields. `get_hardware(demo)` selects demo/Linux
`Hardware` or Darwin `MacHardware`. The adapters provide:
- `capabilities()` exposes platform, tools, test/verify/unmount/eject permissions and limitations.
- async `unmount(drive)` and `eject(drive)` return status/detail after fresh safety checks.
- async `discover() -> list[Drive]`
- async `validate(drive: Drive, destructive: bool = False) -> Drive`
  returns fresh matching device or raises SafetyError. Recheck before every phase.
- async `smart(drive: Drive) -> dict` with health (`passed`, `warning`, `failed`,
  `unsupported`), warnings list, raw parsed smartctl JSON. Nonzero smartctl status
  is decoded as bitmask, not blindly interpreted as command failure.
- async `self_test(drive, progress)`; callback async `progress(percent, detail)`;
  returns dict with status (`passed`, `failed`, `unsupported`, `incomplete`),
  detail and raw results. Poll to terminal state, don't assume command success
  means self-test passed. On cancellation abort test.
- async `benchmark(drive, progress)` returns status, read_mbps, detail, raw.
  Read-only fio direct I/O, finite sample; no writes. Progress callback same shape.
- async `surface(drive, progress, destructive=False)` returns status, detail,
  raw. Read all host-addressable sectors; destructive writes + checksum verify
  on whole drive only after manual arming and serial confirmation.
- async `cancel()` terminates active child process group / drive self-test.
All methods fail closed when disks disconnect, mounts appear, or identity changes.
Discovery polls in an independent runner; fio holds an exclusive Linux block
claim, continuously revalidates identity/path/rdev, streams cumulative JSON status,
and requires exact expected byte coverage. A logical-sector-aligned block size
covers disks whose capacity is not a multiple of 1 MiB.

## Backend/dashboard API
All `/api` routes except `/api/login` and `/api/health` require cookie session or
Bearer API key. Login accepts `{token}`. Mutations check same-origin; Bearer API
calls without Origin allowed. `GET /api/state` and `GET /api/events` (SSE `state`)
return the same full snapshot:
```
{
 mode: "demo"|"hardware", version: "0.1.0", connected: true,
 drives: [Drive + {last_run: run|null}],
 runs: [run newest first, including active/queued],
 settings: {auto_test, auto_eject, headless, notifications_from_env, notifications: {provider: "none"|"discord"|"telegram",
 enabled, discord_configured, telegram_configured, discord_webhook: "",
 telegram_token: "", telegram_chat_id, notify_started, notify_ready}},
 system: {active_run_id, platform, capabilities: {}, release_in_progress, discovery_error, tools: {}, notification_error}
}
```
run: `{id, drive_id, drive: Drive, profile: "quick"|"extended"|"verify",
status: "queued"|"running"|"passed"|"warning"|"failed"|"incomplete"|"cancelled",
phase, progress:0..100, detail, created_at, started_at, finished_at,
results: {smart_before, self_test, benchmark, surface, smart_after}, logs:[{time,message}],
workflow_status: "testing"|"finishing"|"complete"|"interrupted",
lifecycle: {notification_status, eject_status, eject_detail}}`.
- `POST /api/runs`: `{drive_id, profile, confirmation?: "ERASE <serial>"}`.
  `verify` requires config allow_destructive and exact serial confirmation.
- `POST /api/drives/{id}/unmount` or `/eject`: refuse queued/running/finishing work; no forced unmount.
- `POST /api/runs/{id}/cancel`
- `GET /api/runs/{id}`; `GET /api/runs/{id}/report` downloads JSON
- `PUT /api/settings` with {auto_test, auto_eject, notifications:{...}};
  omitted or blank secrets preserve existing values; provider selection configurable.
- `POST /api/notifications/test`: explicitly sends a test to configured provider.
- `POST /api/scan`: rescan devices
- `POST /api/logout`
- `GET /api/events`: SSE snapshots; heartbeat; reconnect via EventSource.
Settings displayed include `allow_destructive` and `demo` read-only fields.

## UI direction
Workbench instrument panel: ink navy #182b45, chalk #f4f7fa, slate #64748b,
cobalt #235ed6, amber #a75a09, muted teal #167268. System sans (human labels)
with monospace only for serials/raw logs. Left aligned layout: narrow station
sidebar, prominent active-test lane with phase steps, drive inventory/table and
expandable history report. Mobile stacks; clear demo label, no misleading claims.

## Headless completion lifecycle
A terminal scan verdict is independent of release status. Finishing remains busy
until the result delivery wait and safe eject attempt complete. SQLite records
outbox delivery status and a separate confirmed-release notification. Startup
recovery marks interrupted release without retrying commands against a possibly
replaced device. Headless enforces auto-test/eject and a configured provider;
notification environment overrides are never returned to clients. Auto intake
requires eligibility and observed detach before repeat work.

macOS inventory uses diskutil plists and IORegistry identity. Read tests use the
raw character device and a portable fio engine. It continuously checks identity
and mount state, without Linux's O_EXCL mount exclusion; destructive verification
is disabled. Normal diskutil unmount/eject is allowed only on fresh matching
external devices. Linux release refuses a shared/unknown USB power-off scope.

## Station readiness notices
The CLI binds its HTTP socket before app startup. After successful discovery and
worker initialization, the engine persists one `startup:ready:<boot-id>` notice
when the configured provider is enabled and `notify_ready` is true. It uses the
normal outbox retry behavior. Undelivered startup notices from prior processes
are discarded on restart so an old process cannot announce readiness. Messages
report the boot timestamp and distinguish read testing from inventory/simulation.
