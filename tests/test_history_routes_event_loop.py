"""The async history/session route handlers call SessionManager's locked,
DB-committing sync methods. Run directly on the event loop, a contended
per-session lock (or a slow commit under it) stalls every request in the
process, not just the caller — the handlers must push those calls off the
loop (asyncio.to_thread).

The test holds a session's lock on a worker thread while the truncate
endpoint runs, and asserts a heartbeat coroutine keeps ticking: if the
handler blocks the loop, the heartbeat starves.
"""
import asyncio
import threading
import time

import pytest

from tests.helpers.sqlite_db import make_temp_sqlite


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _make_manager(monkeypatch):
    import core.database as database
    import core.session_manager as sm_mod

    TestingSessionLocal, engine, tmpfile = make_temp_sqlite(database.Base.metadata)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(sm_mod, "SessionLocal", TestingSessionLocal)
    return sm_mod.SessionManager(), database


def _endpoint(router, path_fragment, method):
    for route in router.routes:
        if path_fragment in getattr(route, "path", "") and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} route matching {path_fragment!r} not found")


@pytest.mark.asyncio
async def test_truncate_does_not_block_event_loop_on_session_lock(monkeypatch):
    from core.models import ChatMessage
    import routes.history.history_routes as mod

    sm, database = _make_manager(monkeypatch)
    monkeypatch.setattr(mod, "_verify_session_owner", lambda *a, **k: None)

    sid = "loop-session"
    sm.create_session(session_id=sid, name="t", endpoint_url="x",
                      model="m", rag=False, owner="u")
    for i in range(3):
        sm.add_message(sid, ChatMessage("user", f"msg{i}"))

    router = mod.setup_history_routes(sm)
    truncate = _endpoint(router, "/truncate", "POST")

    lock = sm._lock_for(sid)
    acquired = threading.Event()

    def hold_lock():
        with lock:
            acquired.set()
            time.sleep(0.8)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert acquired.wait(2), "lock holder never started"

    request_task = asyncio.ensure_future(
        truncate(_FakeRequest({"keep_count": 1}), sid))

    ticks = 0
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        ticks += 1

    result = await request_task
    holder.join(5)

    assert result["truncated"] is True
    assert ticks >= 6, (
        f"event loop starved while handler waited on the session lock "
        f"(only {ticks} heartbeats in 0.6s)"
    )
    assert sm.get_session(sid).message_count == 1
