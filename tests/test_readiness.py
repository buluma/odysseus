"""Tests for the readiness / integrity self-check (src/readiness.py)."""

from src.readiness import check_readiness


def test_readiness_reports_core_subsystems(monkeypatch):
    # Pure unit tests never drive TaskScheduler._loop(), so give it a fresh
    # tick here — this test is about database/data_dir/local_first, not
    # scheduler liveness (that's covered by the test_scheduler_check_* tests
    # below), and a real running instance will have ticked by the time an
    # orchestrator probes /api/ready.
    import src.task_scheduler as ts
    from datetime import datetime, timezone
    monkeypatch.setattr(ts, "_last_tick_at", datetime.now(timezone.utc).replace(tzinfo=None))

    result = check_readiness()

    assert {"ready", "version", "checks", "timestamp"}.issubset(result.keys())
    checks = result["checks"]
    for name in ("database", "data_dir", "local_first", "scheduler"):
        assert name in checks, f"missing check: {name}"

    # In the dev/test environment the local SQLite DB and data dir are present,
    # so the critical checks must pass and overall readiness must be True.
    assert checks["database"]["ok"] is True, checks["database"]
    assert checks["data_dir"]["ok"] is True, checks["data_dir"]
    assert result["ready"] is True, result


def test_local_first_check_is_informational_never_fatal():
    result = check_readiness()
    lf = result["checks"]["local_first"]
    # local_first reports whether storage stays on-host but must never gate
    # readiness — a remote database is a valid deployment.
    assert lf["ok"] is True
    assert "local" in lf


def test_scheduler_check_ok_when_tick_is_fresh(monkeypatch):
    import src.task_scheduler as ts
    from datetime import datetime, timezone
    fresh = datetime.now(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(ts, "_last_tick_at", fresh)

    result = check_readiness()

    assert result["checks"]["scheduler"]["ok"] is True
    assert result["checks"]["scheduler"]["last_tick_seconds_ago"] < 5
    assert result["ready"] is True


def test_scheduler_check_fails_when_tick_is_stale(monkeypatch):
    import src.task_scheduler as ts
    from datetime import datetime, timezone, timedelta
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=301)
    monkeypatch.setattr(ts, "_last_tick_at", stale)

    result = check_readiness()

    assert result["checks"]["scheduler"]["ok"] is False
    assert result["checks"]["scheduler"]["last_tick_seconds_ago"] > 300
    assert result["ready"] is False


def test_scheduler_check_fails_when_never_ticked(monkeypatch):
    import src.task_scheduler as ts
    monkeypatch.setattr(ts, "_last_tick_at", None)

    result = check_readiness()

    assert result["checks"]["scheduler"]["ok"] is False
    assert result["ready"] is False
