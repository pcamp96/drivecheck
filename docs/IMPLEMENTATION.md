# Implementation checkpoint

Objective: local, committed Raspberry Pi drive-testing app, live dashboard,
Discord/Telegram notifications, safe automatic read-only intake; no publication.

Implemented: authenticated API and sessions, SSE snapshots, drive discovery and
identity checks, profiles, one-drive scheduler, cancellation, SQLite history,
JSON report export, settings and secret handling, notification providers/outbox,
responsive dashboard, synthetic demo mode, native systemd installer.

Physical validation remains owner testing on Monday, October 5, 2026. Local
checks do not establish Raspberry Pi performance or USB-adapter compatibility.
See the acceptance test plan. No actual disks or notification accounts were used
for development verification.

Original v0.1 local verification:
- 49 tests passed on both Python 3.11 and Python 3.13.
- Browser acceptance passed: authenticated two-tab SSE, full simulated report
  and JSON export, cancellation, erase confirmation, both notification settings
  and secret removal, reconnect, mobile layout and logout.
- Ruff lint/format, JavaScript syntax, shell syntax, and Git whitespace checks passed.
- Python wheel/source package built; packaged dashboard assets verified.
- Hardware safety review addressed stale SMART logs, command status bitmasks,
  exact coverage/alignment, device identity and exclusive claims, cancellation,
  persistent worker recovery, CSRF and secret lifecycle.

Known verification boundary: Linux exclusive-claim/fio coexistence, actual drive
SMART variants, physical I/O performance/coverage, and real provider delivery
remain Monday acceptance items. No actual notifications were sent; provider
requests were tested with mocked transports and disabled UI configurations.

## Native macOS and unattended intake update — October 2, 2026

Implemented native macOS discovery/read-only testing and normal unmount/eject,
platform capability diagnostics, Pi headless environment configuration, automatic
result-message-to-eject lifecycle, separate release confirmation, and durable
restart handling. Linux USB power-off refuses shared or unknown enclosure scope.
Unsupported macOS destructive verification stays blocked. Each run retains its
test verdict independently from delivery/release outcomes.

Live read-only inventory on this Mac matched the attached 4 TB WD USB drive to
its actual serial through its ASM105x USB registry ancestor. The dashboard was
verified against that inventory; no actual SMART self-tests, raw read/write I/O,
unmount, eject, or provider messages were executed during development. fio was
installed locally for owner testing. Raw tests require a privileged launch.

Verification: 77 unit/integration tests passed on Python 3.11 and 3.13, including
notification outage, confirmed release, cancellation, identity/mount safety,
restart interruption and the Python 3.11 notifier shutdown race. The browser
acceptance flow passed, plus real macOS inventory display and disabled test
controls without root. Ruff, formatting, JavaScript/shell syntax and whitespace
checks passed. Updated Pi source archive/wheel are available under ignored dist/.
Physical Pi/dock behavior and real provider delivery remain acceptance items.

## Linux deployment and station readiness — October 3, 2026

Added a configurable startup-ready notice using the existing durable outbox,
with boot time and actual capabilities. Failed startup never announces readiness;
old pending boot notices are replaced after a restart. The CLI binds its listener
before startup so a port conflict cannot announce a working station.

Added a generic Debian/Ubuntu installer and guarded uninstall command. Default
uninstall preserves reports, settings and token; explicit --purge-data removes
those retained directories. Shared system packages remain installed, and the
manifest preserves the originally added package list across reinstalls.

Deployment on Ubuntu 26.04.1 with Python 3.14.4 was verified through authenticated
HTTP, SSE, a real browser, system-storage refusal, and a complete uninstall/reinstall
cycle preserving the database, settings and sign-in token. The service is enabled
at boot. Existing inference, Docker and Nginx services remained active. No USB
test drive was connected; no physical benchmark, self-test or eject was run.
Notification provider credentials still require owner configuration, so readiness
and result delivery were verified with mocked providers rather than a real chat.

90 tests passed on Python 3.11 and 3.14; Ruff/format, shell and JavaScript syntax,
secret scan, packaging and browser checks passed. Linux fixture tests cover
install/reinstall, preservation, purge, unsafe paths and failed-build recovery.
