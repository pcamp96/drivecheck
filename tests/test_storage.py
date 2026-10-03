import sqlite3

from drivecheck.storage import Store


def test_attachment_migration_preserves_legacy_outbox_and_report_across_restart(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE outbox (id TEXT PRIMARY KEY, message TEXT, attempts INTEGER DEFAULT 0, due REAL DEFAULT 0, delivered INTEGER DEFAULT 0, error TEXT)"
    )
    db.execute("INSERT INTO outbox(id,message) VALUES ('old','Legacy notice')")
    db.commit()
    db.close()
    store = Store(path)
    assert store.pending_notices(0) == [("old", "Legacy notice", 0)]
    assert store.notice_attachment("old") is None
    attachment = {"filename": "report.txt", "text": "Read failure at LBA 622,728."}
    store.enqueue_notice("failed:finished", "Detailed failure", attachment)
    store.notice_failed("failed:finished", 1, 10, "Network down")
    store.close()
    reopened = Store(path)
    assert reopened.notice_attachment("failed:finished") == attachment
    assert ("failed:finished", "Detailed failure", 1) in reopened.pending_notices(10)
    reopened.delivered("failed:finished")
    assert reopened.notice_state("failed:finished")["delivered"]
    assert reopened.notice_attachment("failed:finished") == attachment
    reopened.close()
