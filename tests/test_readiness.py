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


def test_ready_path_is_auth_exempt():
    """Pin that /api/ready is in AUTH_EXEMPT_EXACT.

    Its own docstring says it's "suitable for an orchestrator readiness
    probe" / external uptime monitoring, same as /api/health — but unlike
    /api/health it wasn't actually exempted, so AuthMiddleware rejected every
    probe with 401 before check_readiness() ever ran. Same text-pin technique
    as test_converge_webhook_path_is_auth_exempt (importing app.py directly
    pulls in the full FastAPI app graph, which tests/conftest.py avoids).
    """
    import os
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app.py",
    )
    with open(app_path, encoding="utf-8") as fh:
        src = fh.read()

    start = src.find("AUTH_EXEMPT_EXACT")
    assert start != -1, "AUTH_EXEMPT_EXACT not declared in app.py"
    lb = src.find("{", start)
    assert lb != -1
    depth = 0
    end = -1
    for i in range(lb, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end != -1, "could not find closing brace for AUTH_EXEMPT_EXACT"
    body = src[lb + 1 : end]
    assert "/api/ready" in body, (
        "/api/ready must be in AUTH_EXEMPT_EXACT — orchestrator/watchdog "
        "probes are rejected with 401 without it"
    )
