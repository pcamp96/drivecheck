"""Refuse maintenance while a station still has queued tests or release work."""

import json
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as database:
        for (raw,) in database.execute("SELECT data FROM runs"):
            run = json.loads(raw)
            if run.get("status") in {"queued", "running"} or run.get("workflow_status") in {
                "finishing", "awaiting_action"
            }:
                raise SystemExit(
                    "DriveCheck has unfinished work. Finish testing and safe release before updating."
                )
