"""Tests for TaskScheduler tick liveness tracking (module-level _last_tick_at).

Regression coverage for the Jul 4 incident: the scheduler's background loop
died silently while /api/health kept reporting healthy. src/readiness.py's
/api/ready check needs a real signal that the loop is still ticking.
"""
import sys, types, asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text
from sqlalchemy.orm import sessionmaker, declarative_base


def _test_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stub_heavy():
    for name in [
        "src.builtin_actions", "src.ai_interaction", "src.endpoint_resolver",
        "src.agent_loop", "src.session_manager",
    ]:
        sys.modules.setdefault(name, types.ModuleType(name))


def _setup_isolated_db(monkeypatch):
    import core.database as cd
    B = declarative_base()

    class ScheduledTask(B):
        __tablename__ = "scheduled_tasks"
        id = Column(String, primary_key=True)
        owner = Column(String)
        name = Column(String, default="t")
        prompt = Column(Text)
        task_type = Column(String, default="llm")
        next_run = Column(DateTime, index=True)
        last_run = Column(DateTime)
        status = Column(String, default="active")
        run_count = Column(Integer, default=0)

    class TaskRun(B):
        __tablename__ = "task_runs"
        id = Column(String, primary_key=True)
        task_id = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        status = Column(String, default="queued")
        error = Column(Text)

    eng = create_engine("sqlite:///:memory:")
    B.metadata.create_all(eng)
    monkeypatch.setattr(cd, "engine", eng)
    monkeypatch.setattr(cd, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    monkeypatch.setattr(cd, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(cd, "TaskRun", TaskRun)
    return cd, ScheduledTask, TaskRun


def _build_scheduler(monkeypatch):
    """Bypass __init__, same recipe as test_scheduler_restart_doublefire.py."""
    _stub_heavy()
    _setup_isolated_db(monkeypatch)

    from src.task_scheduler import TaskScheduler
    sch = TaskScheduler.__new__(TaskScheduler)
    sch._executing = set()
    sch._executing_lock = asyncio.Lock()
    sch._concurrency_cap = 1
    sch._run_semaphore = asyncio.Semaphore(1)
    sch._running = True
    sch._task = None
    return sch


def test_last_tick_at_is_none_before_any_loop_iteration(monkeypatch):
    import src.task_scheduler as ts
    monkeypatch.setattr(ts, "_last_tick_at", None)
    assert ts.get_last_tick_at() is None


def test_loop_updates_last_tick_at_each_iteration(monkeypatch):
    import src.task_scheduler as ts
    monkeypatch.setattr(ts, "_last_tick_at", None)

    sch = _build_scheduler(monkeypatch)

    calls = {"n": 0}

    async def _fake_check_due_tasks():
        calls["n"] += 1
        if calls["n"] >= 2:
            sch._running = False  # stop the loop after 2 ticks

    monkeypatch.setattr(sch, "_check_due_tasks", _fake_check_due_tasks)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(ts.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))

    asyncio.run(ts.TaskScheduler._loop(sch))

    assert calls["n"] == 2
    last_tick = ts.get_last_tick_at()
    assert last_tick is not None
    assert abs((last_tick - _test_utcnow()).total_seconds()) < 5


def test_loop_still_updates_last_tick_at_when_check_due_tasks_raises(monkeypatch):
    import src.task_scheduler as ts
    monkeypatch.setattr(ts, "_last_tick_at", None)

    sch = _build_scheduler(monkeypatch)

    calls = {"n": 0}

    async def _raising_check_due_tasks():
        calls["n"] += 1
        sch._running = False
        raise RuntimeError("boom")

    monkeypatch.setattr(sch, "_check_due_tasks", _raising_check_due_tasks)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(ts.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))

    asyncio.run(ts.TaskScheduler._loop(sch))

    assert calls["n"] == 1
    # The existing try/except in _loop() swallows the exception and logs it;
    # the tick timestamp is set before _check_due_tasks() runs, so a raise
    # inside it must not prevent the timestamp from having been recorded.
    assert ts.get_last_tick_at() is not None
