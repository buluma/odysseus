"""Tests for GET /api/openclaw/streamline/digest and POST /api/openclaw/streamline/sync."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.openclaw_streamline_routes import setup_openclaw_streamline_routes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_client(monkeypatch, scopes: list[str]):
    def _mock_scope_owner(request, allowed):
        token_scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if not token_scopes.intersection(allowed):
            from fastapi import HTTPException
            raise HTTPException(403, "missing scope")
        return "alice"

    monkeypatch.setattr("routes.openclaw_streamline_routes._scope_owner", _mock_scope_owner)

    class _AuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            scope["state"] = {
                "api_token": True,
                "api_token_scopes": scopes,
                "api_token_owner": "alice",
            }
            return await self.app(scope, receive, send)

    app = FastAPI()
    app.add_middleware(_AuthMiddleware)
    app.include_router(setup_openclaw_streamline_routes())
    return TestClient(app)


DIGEST_PAYLOAD = {
    "mbu_logs": {
        "total": 127,
        "by_level": {"INFO": 76, "TRACE": 51},
        "recent_errors": [],
        "last_ingested_at": "2026-06-13T12:00:00.000Z",
    },
    "server_side_rules_log": {
        "total": 103,
        "by_status": {"completed": 97, "error": 6},
        "recent_errors": [
            {
                "id": "1",
                "script_name": "118. Smartsheet Pull",
                "status": "error",
                "duration_s": 2.222,
                "error_descr": "TypeError: Cannot read property",
                "created_at": "2026-06-13T11:00:00.000Z",
                "host": "streamline.staging",
                "environment": "staging",
            }
        ],
        "last_ingested_at": "2026-06-13T12:00:00.000Z",
    },
}

SYNC_STATUS_PAYLOAD = {
    "latest_job": {
        "id": "job-1",
        "job_type": "full_manual",
        "status": "success",
        "started_at": "2026-06-13T10:00:00.000Z",
        "ended_at": "2026-06-13T10:00:30.000Z",
        "duration_ms": 30000,
        "error": None,
        "created_at": "2026-06-13T10:00:00.000Z",
    },
    "state": {
        "last_sync_status": "success",
        "last_error": None,
        "running_job_id": None,
        "last_full_sync_at": "2026-06-13T10:00:30.000Z",
        "last_incremental_sync_at": "2026-06-13T10:00:30.000Z",
    },
}


class _MockResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        import json
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _MockAsyncClient:
    def __init__(self, get_payload=None, post_payload=None, status_code=200):
        self._get_payload = get_payload or {}
        self._post_payload = post_payload or {}
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return _MockResponse(self._get_payload, self._status_code)

    async def post(self, *args, **kwargs):
        return _MockResponse(self._post_payload, self._status_code)


# ---------------------------------------------------------------------------
# GET /api/openclaw/streamline/digest
# ---------------------------------------------------------------------------

def test_digest_requires_converge_read(monkeypatch):
    c = _make_client(monkeypatch, scopes=["wrong:scope"])
    resp = c.get("/api/openclaw/streamline/digest")
    assert resp.status_code == 403


def test_digest_returns_formatted_summary(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.local")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "routes.openclaw_streamline_routes.httpx.AsyncClient",
        lambda **kw: _MockAsyncClient(get_payload=DIGEST_PAYLOAD),
    )
    c = _make_client(monkeypatch, scopes=["converge:read"])
    resp = c.get("/api/openclaw/streamline/digest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["mbu_logs"]["total"] == 127
    assert data["mbu_logs"]["by_level"]["INFO"] == 76
    assert data["server_side_rules_log"]["total"] == 103
    assert data["server_side_rules_log"]["error_count"] == 6
    assert len(data["server_side_rules_log"]["recent_errors"]) == 1


def test_digest_not_configured_returns_503(monkeypatch):
    monkeypatch.delenv("CONVERGE_BASE_URL", raising=False)
    monkeypatch.delenv("CONVERGE_API_KEY", raising=False)
    c = _make_client(monkeypatch, scopes=["converge:read"])
    resp = c.get("/api/openclaw/streamline/digest")
    assert resp.status_code == 503


def test_digest_propagates_converge_error(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.local")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "routes.openclaw_streamline_routes.httpx.AsyncClient",
        lambda **kw: _MockAsyncClient(get_payload={"error": "Unauthorized"}, status_code=401),
    )
    c = _make_client(monkeypatch, scopes=["converge:read"])
    resp = c.get("/api/openclaw/streamline/digest")
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/openclaw/streamline/sync
# ---------------------------------------------------------------------------

def test_sync_requires_converge_write(monkeypatch):
    c = _make_client(monkeypatch, scopes=["converge:read"])
    resp = c.post("/api/openclaw/streamline/sync", json={"confirm": True})
    assert resp.status_code == 403


def test_sync_requires_confirm(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.local")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")
    c = _make_client(monkeypatch, scopes=["converge:write"])
    resp = c.post("/api/openclaw/streamline/sync", json={"confirm": False})
    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"]


def test_sync_triggers_and_returns_job(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.local")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "routes.openclaw_streamline_routes.httpx.AsyncClient",
        lambda **kw: _MockAsyncClient(
            post_payload={"ok": True, "job_id": "new-job-1", "job": {"id": "new-job-1", "status": "pending"}},
        ),
    )
    c = _make_client(monkeypatch, scopes=["converge:write"])
    resp = c.post("/api/openclaw/streamline/sync", json={"confirm": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["job_id"] == "new-job-1"


def test_sync_not_configured_returns_503(monkeypatch):
    monkeypatch.delenv("CONVERGE_BASE_URL", raising=False)
    monkeypatch.delenv("CONVERGE_API_KEY", raising=False)
    c = _make_client(monkeypatch, scopes=["converge:write"])
    resp = c.post("/api/openclaw/streamline/sync", json={"confirm": True})
    assert resp.status_code == 503


def test_sync_propagates_converge_error(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.local")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "routes.openclaw_streamline_routes.httpx.AsyncClient",
        lambda **kw: _MockAsyncClient(post_payload={"error": "Internal server error"}, status_code=500),
    )
    c = _make_client(monkeypatch, scopes=["converge:write"])
    resp = c.post("/api/openclaw/streamline/sync", json={"confirm": True})
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET /api/openclaw/streamline/sync-status
# ---------------------------------------------------------------------------

def test_sync_status_requires_converge_read(monkeypatch):
    c = _make_client(monkeypatch, scopes=["wrong:scope"])
    resp = c.get("/api/openclaw/streamline/sync-status")
    assert resp.status_code == 403


def test_sync_status_returns_job_and_state(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.local")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")
    monkeypatch.setattr(
        "routes.openclaw_streamline_routes.httpx.AsyncClient",
        lambda **kw: _MockAsyncClient(get_payload=SYNC_STATUS_PAYLOAD),
    )
    c = _make_client(monkeypatch, scopes=["converge:read"])
    resp = c.get("/api/openclaw/streamline/sync-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["latest_job"]["id"] == "job-1"
    assert data["state"]["last_sync_status"] == "success"
