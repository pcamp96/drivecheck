# DriveCheck
Python 3.11+ / FastAPI / SQLite / vanilla browser JavaScript. Linux hardware mode,
macOS read-only hardware mode, portable demo mode. Preserve other contributors' changes. Never execute hardware
tests against local devices during development. All physical-drive tests require
identity and safety checks; destructive tests are manual, never auto-triggered.
Run `uv run --extra dev pytest` and `uv run --extra dev ruff check .` before handoff.
Use one process/one Uvicorn worker; device jobs must never run concurrently for the
same drive. Do not commit credentials, runtime state, or generated artifacts.
