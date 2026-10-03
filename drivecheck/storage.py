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
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        for column in ("attachment_name", "attachment_text"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {column} TEXT")
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
            lifecycle = run.setdefault(
                "lifecycle",
                {
                    "notification_status": "disabled",
                    "eject_status": "not_requested",
                    "eject_detail": "",
                },
            )
            if run.get("workflow_status") == "finishing":
                run["workflow_status"] = "interrupted"
                lifecycle["eject_status"] = "failed"
                lifecycle["eject_detail"] = (
                    "Service restarted before safe release completed. Inspect the connected "
                    "drive and start a new intake after physically reconnecting it."
                )
                self.save(run)
                continue
            if run["status"] in {"running", "queued"}:
                from drivecheck.engine import now

                run.update(
                    status="incomplete",
                    workflow_status="interrupted",
                    finished_at=now(),
                    detail="Service restarted before this test finished. Start a new test to retry.",
                )
                lifecycle["eject_status"] = "not_requested"
                lifecycle["eject_detail"] = "The interrupted test was not automatically released."
                self.save(run)

    def enqueue_notice(self, notice_id: str, message: str, attachment: dict | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO outbox(id,message,attachment_name,attachment_text) VALUES (?,?,?,?)",
            (
                notice_id,
                message,
                (attachment or {}).get("filename"),
                (attachment or {}).get("text"),
            ),
        )
        self.db.commit()

    def notice_attachment(self, notice_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT attachment_name,attachment_text FROM outbox WHERE id=?", (notice_id,)
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return {"filename": row[0], "text": row[1]}

    def discard_pending_startup_notices(self) -> None:
        """Remove boot notices that would describe a previous process as newly ready."""
        self.db.execute("DELETE FROM outbox WHERE id LIKE 'startup:ready:%' AND delivered=0")
        self.db.commit()

    def pending_notices(self, timestamp: float) -> list[tuple]:
        return self.db.execute(
            "SELECT id,message,attempts FROM outbox WHERE delivered=0 AND attempts<6 AND due<=? LIMIT 10",
            (timestamp,),
        ).fetchall()

    def delivered(self, notice_id: str) -> None:
        self.db.execute("UPDATE outbox SET delivered=1,error=NULL WHERE id=?", (notice_id,))
        self.db.commit()

    def notice_state(self, notice_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT attempts,delivered,error FROM outbox WHERE id=?", (notice_id,)
        ).fetchone()
        if row is None:
            return None
        return {"attempts": row[0], "delivered": bool(row[1]), "error": row[2]}

    def notice_failed(self, notice_id: str, attempts: int, due: float, error: str) -> None:
        self.db.execute(
            "UPDATE outbox SET attempts=?,due=?,error=? WHERE id=?",
            (attempts, due, error, notice_id),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
