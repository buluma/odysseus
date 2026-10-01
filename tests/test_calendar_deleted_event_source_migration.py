"""caldav_deleted_events gains a `source` column on existing databases."""

import sqlite3

import core.database as database


def _columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(caldav_deleted_events)")]
    finally:
        conn.close()


def test_migration_adds_source_column_to_legacy_table_and_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE caldav_deleted_events (uid TEXT PRIMARY KEY, owner TEXT)")
    conn.execute("INSERT INTO caldav_deleted_events (uid, owner) VALUES ('u1', 'alice')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db_path}")

    assert "source" not in _columns(db_path)

    database._migrate_add_calendar_deleted_event_source()
    assert "source" in _columns(db_path)

    # Pre-existing tombstones keep their row and read as untagged (NULL), which
    # the CalDAV write-back treats as its own.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT uid, owner, source FROM caldav_deleted_events").fetchone()
    finally:
        conn.close()
    assert row == ("u1", "alice", None)

    database._migrate_add_calendar_deleted_event_source()  # second run is a no-op
    assert _columns(db_path).count("source") == 1
