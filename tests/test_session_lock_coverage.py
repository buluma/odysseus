"""Per-session lock coverage for SessionManager (core/session_manager.py).

The lock added for the streaming-vs-delete race only protected the four
SessionManager mutators, but the primary streaming write path is
Session.add_message (core/models.py), and several other mutators of
``self.sessions`` (get_session hydration, update_session_name,
archive_session, mark_important, cleanup_empty_sessions) bypassed the lock
entirely — so the race stayed open. These tests pin that every one of those
paths serializes on the same per-session lock, and that cleanup re-checks
emptiness under the lock instead of trusting a stale row.

The blocking tests hold a session's lock on the test thread and assert the
operation under test waits on a worker thread until release. The per-session
locks are reentrant (RLock), so the holding thread itself can still mutate —
which is exactly what the cleanup re-check test exploits.
"""
import threading

import pytest

from tests.helpers.sqlite_db import make_temp_sqlite


def _make_manager(monkeypatch):
    import core.database as database
    import core.session_manager as sm_mod

    TestingSessionLocal, engine, tmpfile = make_temp_sqlite(database.Base.metadata)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(sm_mod, "SessionLocal", TestingSessionLocal)

    return sm_mod.SessionManager(), database, sm_mod


def _create_session(sm, sid):
    return sm.create_session(session_id=sid, name="t", endpoint_url="x",
                             model="m", rag=False, owner="u")


def _assert_blocks_until_released(sm, sid, fn, label):
    """Assert fn() waits on the session's lock and completes after release."""
    lock = sm._lock_for(sid)
    lock.acquire()
    done = threading.Event()

    def run():
        fn()
        done.set()

    t = threading.Thread(target=run, daemon=True)
    try:
        t.start()
        assert not done.wait(0.3), f"{label} did not wait for the session lock"
    finally:
        lock.release()
    assert done.wait(5), f"{label} never finished after the lock was released"


def test_model_add_message_blocks_on_session_lock(monkeypatch):
    """Streaming responses append via Session.add_message (the model method),
    not SessionManager.add_message — it must take the same per-session lock
    or the delete-while-streaming race stays open."""
    import core.models as models
    from core.models import ChatMessage

    sm, database, sm_mod = _make_manager(monkeypatch)
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", sm)
    sid = "stream-session"
    session = _create_session(sm, sid)

    _assert_blocks_until_released(
        sm, sid,
        lambda: session.add_message(ChatMessage("user", "hello")),
        "Session.add_message",
    )

    assert len(session.history) == 1
    db = database.SessionLocal()
    try:
        rows = db.query(database.ChatMessage).filter(
            database.ChatMessage.session_id == sid).count()
        assert rows == 1
    finally:
        db.close()


def test_model_add_message_without_manager_still_appends(monkeypatch):
    """A detached Session (no singleton set) keeps the old in-memory-only
    behavior — no lock, no persistence, no crash."""
    import core.models as models
    from core.models import ChatMessage, Session

    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", None)
    session = Session(id="detached", name="t", endpoint_url="x", model="m")
    session.add_message(ChatMessage("user", "hello"))
    assert len(session.history) == 1
    assert session.message_count == 1


def test_get_session_blocks_on_session_lock(monkeypatch):
    """get_session hydrates into self.sessions; unless it serializes with
    delete_session it can resurrect a just-deleted session (issue #1044)."""
    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = "hydrate-session"
    _create_session(sm, sid)

    _assert_blocks_until_released(
        sm, sid, lambda: sm.get_session(sid), "get_session")


@pytest.mark.parametrize("mutator", ["update_session_name", "archive_session", "mark_important"])
def test_metadata_mutators_block_on_session_lock(monkeypatch, mutator):
    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = f"{mutator}-session"
    _create_session(sm, sid)

    calls = {
        "update_session_name": lambda: sm.update_session_name(sid, "renamed"),
        "archive_session": lambda: sm.archive_session(sid),
        "mark_important": lambda: sm.mark_important(sid, True),
    }
    _assert_blocks_until_released(sm, sid, calls[mutator], mutator)


def test_locked_mutators_are_reentrant(monkeypatch):
    """SessionManager.add_message holds the session lock and calls
    get_session, which also takes it — same-thread reacquisition must not
    deadlock."""
    from core.models import ChatMessage

    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = "reentrant-session"
    _create_session(sm, sid)

    done = threading.Event()

    def run():
        sm.add_message(sid, ChatMessage("user", "hello"))
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(5), "add_message deadlocked on its own session lock"
    assert sm.get_session(sid).message_count == 1


def test_cleanup_blocks_on_session_lock(monkeypatch):
    """cleanup_empty_sessions deletes rows and evicts self.sessions entries;
    it must serialize per session with the other mutators."""
    from datetime import timedelta

    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = "empty-session"
    _create_session(sm, sid)

    db = database.SessionLocal()
    try:
        row = db.query(database.Session).filter(database.Session.id == sid).first()
        row.created_at = database.utcnow_naive() - timedelta(hours=2)
        db.commit()
    finally:
        db.close()

    _assert_blocks_until_released(
        sm, sid, lambda: sm.cleanup_empty_sessions(), "cleanup_empty_sessions")


def test_cleanup_spares_session_that_gained_messages_while_waiting(monkeypatch):
    """Lost-update regression: cleanup reads message_count==0, then waits on
    the session lock while a message lands. Once it acquires the lock it must
    re-check the row and spare the session instead of deleting fresh data."""
    from datetime import timedelta

    from core.models import ChatMessage

    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = "raced-session"
    _create_session(sm, sid)

    db = database.SessionLocal()
    try:
        row = db.query(database.Session).filter(database.Session.id == sid).first()
        row.created_at = database.utcnow_naive() - timedelta(hours=2)
        db.commit()
    finally:
        db.close()

    cleanup_done = threading.Event()
    scenario_done = threading.Event()
    stats = {}
    results = {}

    def run_cleanup():
        stats.update(sm.cleanup_empty_sessions())
        cleanup_done.set()

    def scenario():
        # The whole interleaving runs on this worker so a non-reentrant
        # per-session lock shows up as a clean pytest.fail on the deadline
        # below instead of hanging the test process: holding the lock, the
        # message lands (same-thread reacquire — locks must be RLock) while
        # cleanup is parked on the lock, exactly the race under test.
        lock = sm._lock_for(sid)
        lock.acquire()
        try:
            threading.Thread(target=run_cleanup, daemon=True).start()
            results["cleanup_waited"] = not cleanup_done.wait(0.3)
            sm.add_message(sid, ChatMessage("user", "hello"))
        finally:
            lock.release()
        scenario_done.set()

    threading.Thread(target=scenario, daemon=True).start()
    if not scenario_done.wait(10):
        pytest.fail("interleaving deadlocked — per-session lock is not reentrant")
    assert results["cleanup_waited"], "cleanup did not wait for the session lock"
    assert cleanup_done.wait(5), "cleanup never finished after the lock was released"

    assert stats["deleted_empty"] == 0
    assert sid in sm.sessions
    db = database.SessionLocal()
    try:
        assert db.query(database.Session).filter(database.Session.id == sid).count() == 1
        assert db.query(database.ChatMessage).filter(
            database.ChatMessage.session_id == sid).count() == 1
    finally:
        db.close()
