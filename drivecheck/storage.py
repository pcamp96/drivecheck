"""Durable reports and notification outbox, with no secrets in reports."""

import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT, data TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, message TEXT, "
            "attempts INTEGER DEFAULT 0, due REAL DEFAULT 0, delivered INTEGER DEFAULT 0, error TEXT)"
        )
        self.db.commit()

    def save(self, run: dict) -> None:
        self.db.execute(
            "INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (run["id"], run["created_at"], json.dumps(run)),
        )
        self.db.commit()

    def runs(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT data FROM runs ORDER BY created DESC LIMIT ?", (limit,))
        return [json.loads(row[0]) for row in rows]

    def get(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recover(self) -> None:
        # Jobs never silently resume after a restart; particularly not erase jobs.
        rows = self.db.execute("SELECT data FROM runs").fetchall()
        for (data,) in rows:
            run = json.loads(data)
            if run["status"] in {"running", "queued"}:
                from drivecheck.engine import now

                run.update(
                    status="incomplete",
                    finished_at=now(),
                    detail="Service restarted before this test finished. Start a new test to retry.",
                )
                self.save(run)

    def enqueue_notice(self, notice_id: str, message: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO outbox(id,message) VALUES (?,?)", (notice_id, message)
        )
        self.db.commit()

    def pending_notices(self, timestamp: float) -> list[tuple]:
        return self.db.execute(
            "SELECT id,message,attempts FROM outbox WHERE delivered=0 AND attempts<6 AND due<=? LIMIT 10",
            (timestamp,),
        ).fetchall()

    def delivered(self, notice_id: str) -> None:
        self.db.execute("UPDATE outbox SET delivered=1,error=NULL WHERE id=?", (notice_id,))
        self.db.commit()

    def notice_failed(self, notice_id: str, attempts: int, due: float, error: str) -> None:
        self.db.execute(
            "UPDATE outbox SET attempts=?,due=?,error=? WHERE id=?",
            (attempts, due, error, notice_id),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
