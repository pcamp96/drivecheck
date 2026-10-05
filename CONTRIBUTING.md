# Contributing to DriveCheck

DriveCheck uses Python 3.11+, FastAPI, SQLite, and a plain JavaScript dashboard.
Development runs against synthetic drives. Never point automated tests at real
storage or notification accounts.

## Development setup

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/), then:

```sh
git clone https://github.com/pcamp96/drivecheck.git
cd drivecheck
uv sync --locked --extra dev
uv run --locked drivecheck --demo
```

Open <http://127.0.0.1:8765> with the generated token from `data/access-token`.
Simulation never reads host disks, but manually configured notifications can send
real messages. Leave notification delivery disabled for development.

## Verification

```sh
uv run --extra dev pytest
uv run --extra dev ruff check .
node --check drivecheck/static/app.js
uv run --extra dev playwright install chromium
uv run --extra dev python scripts/verify-browser.py
```

JavaScript syntax checks need Node.js. The browser check uses an isolated
simulated station and writes ignored screenshots/reports into `artifacts/`.
Run checks appropriate to your change. Keep device tests separate and follow the
[hardware validation checklist](docs/HARDWARE-VALIDATION.md) on disposable hardware.

## Project layout

- `drivecheck/`: API, scheduler, hardware adapters, notifications, and report storage.
- `drivecheck/static/`: dashboard HTML, CSS, and JavaScript.
- `scripts/` and `deploy/`: native Linux installation, removal, and service definition.
- `tests/`: isolated unit and integration tests.
- `docs/`: installation, configuration, usage, and [architecture](docs/ARCHITECTURE.md).

## Changes and bug reports

Keep pull requests focused and describe the resulting behavior and verification.
Preserve the read-only automatic workflow, fresh device identity checks, and
explicit confirmation for destructive operations. Only one application process
may use a station data directory, and drive jobs must not overlap.

For bugs, include the OS, Python/tool versions, dock model, test profile, failure
message, and relevant report or log excerpt in a [GitHub issue](https://github.com/pcamp96/drivecheck/issues).
Review reports for private drive identities before posting. Remove access tokens,
notification credentials, recovery passwords, and account details. Do not commit
runtime state, `.env` files, virtual environments, or generated artifacts.
