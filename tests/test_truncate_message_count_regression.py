"""Regression: truncate_messages must not set message_count above the real
number of messages when keep_count exceeds the message total.

The AI tool layer (src/ai_interaction.py manage_session action='truncate')
defaults keep_count=10, so a short session (say 3 messages) gets truncated
with keep_count=10. The DB has only 3 rows left, but truncate_messages used to
write db_session.message_count = keep_count (=10), leaving the persisted count
inconsistent with the actual rows. get_session relies on message_count>0 to
decide whether to lazily hydrate from the DB, so an inflated count is a latent
correctness hazard.
"""
from tests.helpers.sqlite_db import make_temp_sqlite


def _make_manager(monkeypatch):
    import core.database as database
    import core.session_manager as sm_mod

    TestingSessionLocal, engine, tmpfile = make_temp_sqlite(database.Base.metadata)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(sm_mod, "SessionLocal", TestingSessionLocal)

    return sm_mod.SessionManager(), database, sm_mod


def test_truncate_keep_count_exceeds_total_does_not_inflate_count(monkeypatch):
    from core.models import ChatMessage

    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = "short-session"
    sm.create_session(session_id=sid, name="t", endpoint_url="x",
                      model="m", rag=False, owner="u")
    for i in range(3):
        sm.add_message(sid, ChatMessage("user", f"msg{i}"))

    # AI default keep_count is 10 — larger than the 3 real messages.
    assert sm.truncate_messages(sid, 10) is True

    db = database.SessionLocal()
    try:
        DbSession = database.Session
        DbChatMessage = database.ChatMessage
        rows = db.query(DbChatMessage).filter(
            DbChatMessage.session_id == sid).count()
        db_session = db.query(DbSession).filter(DbSession.id == sid).first()
        # Nothing should have been deleted (only 3 messages exist).
        assert rows == 3
        # message_count must reflect the real number of rows, not keep_count.
        assert db_session.message_count == 3, (
            f"message_count={db_session.message_count} but only {rows} rows exist"
        )
    finally:
        db.close()


def test_truncate_keeps_history_alias_for_context_messages(monkeypatch):
    from core.models import ChatMessage

    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = "alias-after-truncate"
    sm.create_session(session_id=sid, name="t", endpoint_url="x",
                      model="m", rag=False, owner="u")
    for i in range(3):
        sm.add_message(sid, ChatMessage("user", f"msg{i}"))

    assert sm.truncate_messages(sid, 2) is True

    session = sm.sessions[sid]
    assert session.history is session._history

    session.history.append(ChatMessage("user", "after direct mutation"))
    assert session.get_context_messages()[-1]["content"] == "after direct mutation"


def test_truncate_updates_in_memory_message_count(monkeypatch):
    """The cached session's message_count must track the truncated history —
    replace_messages updates it, truncate_messages must too, or list views
    and get_session's hydration heuristic read an inflated count."""
    from core.models import ChatMessage

    sm, database, sm_mod = _make_manager(monkeypatch)
    sid = "count-after-truncate"
    sm.create_session(session_id=sid, name="t", endpoint_url="x",
                      model="m", rag=False, owner="u")
    for i in range(5):
        sm.add_message(sid, ChatMessage("user", f"msg{i}"))

    assert sm.truncate_messages(sid, 2) is True

    session = sm.sessions[sid]
    assert len(session.history) == 2
    assert session.message_count == 2, (
        f"in-memory message_count={session.message_count}, history has 2"
    )


def test_truncate_missing_session_raises_keyerror_even_with_negative_keep(monkeypatch):
    """A missing session must surface as KeyError (the truncate route maps it
    to 404) regardless of keep_count; returning False for a bad keep_count
    must not mask 'session not found'."""
    import pytest

    sm, database, sm_mod = _make_manager(monkeypatch)

    with pytest.raises(KeyError):
        sm.truncate_messages("no-such-session", -1)
