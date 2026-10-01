"""Tests for n8n mobile control layer — workflows, rerun, pause, allowlist."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from routes.openclaw_n8n_routes import setup_openclaw_n8n_routes, _workflow_allowed
from src.n8n_client import N8nClient, N8nClientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request(scopes=None, owner="alice"):
    return SimpleNamespace(state=SimpleNamespace(
        api_token=True,
        api_token_scopes=scopes or [],
        api_token_owner=owner,
    ))


def _endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _mock_client(
    *,
    configured=True,
    workflows=None,
    executions=None,
    get_workflow_data=None,
    retry_result=None,
    deactivate_result=None,
):
    client = MagicMock()
    client.configured = configured
    client.list_workflows = AsyncMock(return_value=workflows or [])
    client.get_workflow_executions = AsyncMock(return_value=executions or [])
    client.get_workflow = AsyncMock(return_value=get_workflow_data or {})
    client.retry_execution = AsyncMock(return_value=retry_result or {"status": "ok"})
    client.deactivate_workflow = AsyncMock(return_value=deactivate_result or {"id": "wf-1", "active": False})
    return client


# ---------------------------------------------------------------------------
# N8nClient unit tests — new methods
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_get_workflow_success(monkeypatch):
    class MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"id": "wf-1", "name": "My Flow", "active": True}

    class MockClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw): return MockResp()

    monkeypatch.setenv("N8N_BASE_URL", "http://n8n.local")
    monkeypatch.setenv("N8N_API_KEY", "key")
    monkeypatch.setattr("src.n8n_client.httpx.AsyncClient", MockClient)
    client = N8nClient()
    result = await client.get_workflow("wf-1")
    assert result["id"] == "wf-1"
    assert result["active"] is True


@pytest.mark.asyncio
async def test_client_get_workflow_unconfigured(monkeypatch):
    monkeypatch.setenv("N8N_BASE_URL", "")
    client = N8nClient()
    result = await client.get_workflow("wf-1")
    assert result == {}


@pytest.mark.asyncio
async def test_client_get_workflow_executions_success(monkeypatch):
    class MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "exec-1", "workflowId": "wf-1", "status": "success"}]}

    class MockClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw): return MockResp()

    monkeypatch.setenv("N8N_BASE_URL", "http://n8n.local")
    monkeypatch.setenv("N8N_API_KEY", "key")
    monkeypatch.setattr("src.n8n_client.httpx.AsyncClient", MockClient)
    client = N8nClient()
    result = await client.get_workflow_executions("wf-1", limit=5)
    assert len(result) == 1
    assert result[0]["id"] == "exec-1"


@pytest.mark.asyncio
async def test_client_retry_execution_success(monkeypatch):
    class MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"executionId": "exec-2"}

    class MockClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw): return MockResp()

    monkeypatch.setenv("N8N_BASE_URL", "http://n8n.local")
    monkeypatch.setenv("N8N_API_KEY", "key")
    monkeypatch.setattr("src.n8n_client.httpx.AsyncClient", MockClient)
    client = N8nClient()
    result = await client.retry_execution("exec-1")
    assert result.get("executionId") == "exec-2"


@pytest.mark.asyncio
async def test_client_deactivate_workflow_success(monkeypatch):
    class MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"id": "wf-1", "active": False}

    class MockClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw): return MockResp()

    monkeypatch.setenv("N8N_BASE_URL", "http://n8n.local")
    monkeypatch.setenv("N8N_API_KEY", "key")
    monkeypatch.setattr("src.n8n_client.httpx.AsyncClient", MockClient)
    client = N8nClient()
    result = await client.deactivate_workflow("wf-1")
    assert result["active"] is False


# ---------------------------------------------------------------------------
# _workflow_allowed allowlist helper
# ---------------------------------------------------------------------------

def test_workflow_allowed_star_allows_all(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "*")
    assert _workflow_allowed("my-flow") is True
    assert _workflow_allowed("wf-123") is True


def test_workflow_allowed_matches_name(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "backup-n8n,deploy-prod")
    assert _workflow_allowed("backup-n8n") is True
    assert _workflow_allowed("deploy-prod") is True
    assert _workflow_allowed("unknown-flow") is False


def test_workflow_allowed_matches_id(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "wf-abc123,wf-xyz")
    assert _workflow_allowed("wf-abc123") is True
    assert _workflow_allowed("wf-not-there") is False


def test_workflow_allowed_empty_denies_all(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "")
    assert _workflow_allowed("any-workflow") is False


def test_workflow_not_in_allowlist(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "safe-flow")
    assert _workflow_allowed("dangerous-flow") is False


# ---------------------------------------------------------------------------
# GET /workflows — list all workflows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_workflows_returns_all(monkeypatch):
    workflows = [
        {"id": "wf-1", "name": "Backup Flow", "active": True},
        {"id": "wf-2", "name": "Deploy Flow", "active": False},
    ]
    client = _mock_client(workflows=workflows)
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/workflows", "GET")
        result = await ep(_request(["n8n:read"]))
    assert result["status"] == "ok"
    assert len(result["workflows"]) == 2
    assert result["workflows"][0]["id"] == "wf-1"


@pytest.mark.asyncio
async def test_list_workflows_requires_n8n_read_scope():
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=_mock_client()):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/workflows", "GET")
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["chat"]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_workflows_unconfigured():
    client = _mock_client(configured=False)
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/workflows", "GET")
        result = await ep(_request(["n8n:read"]))
    assert result["status"] == "ok"
    assert result["workflows"] == []


# ---------------------------------------------------------------------------
# GET /workflows/{workflow_id}/last-execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_execution_returns_most_recent(monkeypatch):
    executions = [
        {"id": "exec-2", "status": "success", "startedAt": "2025-01-02T10:00:00Z"},
        {"id": "exec-1", "status": "error", "startedAt": "2025-01-01T10:00:00Z"},
    ]
    client = _mock_client(executions=executions)
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/workflows/{workflow_id}/last-execution", "GET")
        result = await ep(_request(["n8n:read"]), "wf-1")
    assert result["status"] == "ok"
    assert result["execution"]["id"] == "exec-2"


@pytest.mark.asyncio
async def test_last_execution_no_executions_returns_404(monkeypatch):
    client = _mock_client(executions=[])
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/workflows/{workflow_id}/last-execution", "GET")
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:read"]), "wf-1")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_last_execution_requires_n8n_read_scope():
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=_mock_client()):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/workflows/{workflow_id}/last-execution", "GET")
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["chat"]), "wf-1")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# POST /ops/n8n-rerun — rerun last failed execution of an allowlisted workflow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_n8n_rerun_success(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "backup-n8n")
    workflows = [{"id": "wf-1", "name": "backup-n8n", "active": True}]
    executions = [{"id": "exec-7", "status": "error", "startedAt": "2025-01-01T00:00:00Z"}]
    client = _mock_client(workflows=workflows, executions=executions, retry_result={"executionId": "exec-8"})
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-rerun", "POST")
        from routes.openclaw_n8n_routes import N8nRerunRequest
        result = await ep(_request(["n8n:write"]), N8nRerunRequest(workflow="backup-n8n", confirm=True))
    assert result["status"] == "ok"
    assert "rerun" in result.get("message", "").lower() or "retry" in result.get("message", "").lower()


@pytest.mark.asyncio
async def test_n8n_rerun_requires_confirm(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "*")
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=_mock_client()):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-rerun", "POST")
        from routes.openclaw_n8n_routes import N8nRerunRequest
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:write"]), N8nRerunRequest(workflow="wf-1", confirm=False))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_n8n_rerun_workflow_not_in_allowlist(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "safe-flow")
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=_mock_client()):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-rerun", "POST")
        from routes.openclaw_n8n_routes import N8nRerunRequest
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:write"]), N8nRerunRequest(workflow="dangerous-flow", confirm=True))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_n8n_rerun_workflow_not_found(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "*")
    client = _mock_client(workflows=[])
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-rerun", "POST")
        from routes.openclaw_n8n_routes import N8nRerunRequest
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:write"]), N8nRerunRequest(workflow="no-such-workflow", confirm=True))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_n8n_rerun_no_failed_execution(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "*")
    workflows = [{"id": "wf-1", "name": "healthy-flow", "active": True}]
    client = _mock_client(workflows=workflows, executions=[])
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-rerun", "POST")
        from routes.openclaw_n8n_routes import N8nRerunRequest
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:write"]), N8nRerunRequest(workflow="healthy-flow", confirm=True))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# POST /ops/n8n-pause — deactivate an allowlisted workflow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_n8n_pause_success(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "backup-n8n")
    workflows = [{"id": "wf-1", "name": "backup-n8n", "active": True}]
    client = _mock_client(workflows=workflows, deactivate_result={"id": "wf-1", "active": False})
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-pause", "POST")
        from routes.openclaw_n8n_routes import N8nPauseRequest
        result = await ep(_request(["n8n:write"]), N8nPauseRequest(workflow="backup-n8n", confirm=True))
    assert result["status"] == "ok"
    assert result.get("active") is False


@pytest.mark.asyncio
async def test_n8n_pause_requires_confirm(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "*")
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=_mock_client()):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-pause", "POST")
        from routes.openclaw_n8n_routes import N8nPauseRequest
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:write"]), N8nPauseRequest(workflow="wf-1", confirm=False))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_n8n_pause_not_in_allowlist(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "safe-flow")
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=_mock_client()):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-pause", "POST")
        from routes.openclaw_n8n_routes import N8nPauseRequest
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:write"]), N8nPauseRequest(workflow="blocked-flow", confirm=True))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_n8n_pause_workflow_not_found(monkeypatch):
    monkeypatch.setenv("N8N_WORKFLOW_ALLOWLIST", "*")
    client = _mock_client(workflows=[])
    with patch("routes.openclaw_n8n_routes.N8nClient", return_value=client):
        router = setup_openclaw_n8n_routes()
        ep = _endpoint(router, "/api/openclaw/n8n/ops/n8n-pause", "POST")
        from routes.openclaw_n8n_routes import N8nPauseRequest
        with pytest.raises(HTTPException) as exc:
            await ep(_request(["n8n:write"]), N8nPauseRequest(workflow="ghost-flow", confirm=True))
    assert exc.value.status_code == 404
