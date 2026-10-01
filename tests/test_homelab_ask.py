"""Tests for POST /api/openclaw/homelab/ask — context-injection Q&A endpoint."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.openclaw_homelab_routes import setup_openclaw_homelab_routes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_client(monkeypatch, scopes: list[str]):
    def _mock_scope_owner(request, allowed):
        if not getattr(request.state, 'api_token', False):
            return 'alice'
        token_scopes = set(getattr(request.state, 'api_token_scopes', []) or [])
        if not token_scopes.intersection(allowed):
            from fastapi import HTTPException
            raise HTTPException(403, 'missing scope')
        return 'alice'

    monkeypatch.setattr('routes.openclaw_homelab_routes._scope_owner', _mock_scope_owner)

    class _AuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                return await self.app(scope, receive, send)
            scope['state'] = {
                'api_token': True,
                'api_token_scopes': scopes,
                'api_token_owner': 'alice',
            }
            return await self.app(scope, receive, send)

    app = FastAPI()
    app.add_middleware(_AuthMiddleware)
    app.include_router(setup_openclaw_homelab_routes())
    return TestClient(app)


@pytest.fixture()
def mock_snapshot(monkeypatch):
    """Patch all snapshot-gathering helpers to return fast stubs."""
    monkeypatch.setattr(
        'routes.openclaw_homelab_routes.execute_health_checks',
        AsyncMock(return_value=(
            [{'name': 'immich', 'status': 'ok'}, {'name': 'caddy', 'status': 'ok'}],
            [],
            'ok',
        )),
    )
    monkeypatch.setattr(
        'routes.openclaw_homelab_routes._disk_usage_summary',
        lambda: {'status': 'ok', 'filesystems': [{'mount': '/', 'use_percent': 42, 'avail': '50G'}], 'high_usage': []},
    )
    monkeypatch.setattr(
        'routes.openclaw_homelab_routes._tailscale_status',
        lambda: {'status': 'ok', 'tailscale': {'peer_count': 3}},
    )

    store = MagicMock()
    store.get_events.return_value = [{'title': 'caddy down', 'severity': 'critical'}]
    monkeypatch.setattr('routes.openclaw_homelab_routes.EventStore', lambda: store)

    n8n_client = MagicMock()
    n8n_client.configured = True
    n8n_client.get_failed_executions_summary = AsyncMock(
        return_value={'configured': True, 'failed_count': 1, 'executions': [{'workflow_name': 'backup-n8n', 'error': 'timeout'}]}
    )
    monkeypatch.setattr('routes.openclaw_homelab_routes.N8nClient', lambda: n8n_client)

    return store


@pytest.fixture()
def mock_llm(monkeypatch):
    """Patch LLM endpoint resolution and call."""
    monkeypatch.setattr(
        'routes.openclaw_homelab_routes.resolve_endpoint',
        lambda kind, owner=None: ('http://llm.local/v1', 'test-model', {}),
    )
    monkeypatch.setattr(
        'routes.openclaw_homelab_routes.llm_call_async',
        AsyncMock(return_value='Immich is healthy. Disk at 42%.'),
    )


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------

def test_ask_requires_homelab_read(monkeypatch, mock_snapshot, mock_llm):
    c = _make_client(monkeypatch, scopes=['wrong:scope'])
    resp = c.post('/api/openclaw/homelab/ask', json={'question': 'is immich ok?'})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_ask_rejects_empty_question(monkeypatch, mock_snapshot, mock_llm):
    c = _make_client(monkeypatch, scopes=['homelab:read'])
    resp = c.post('/api/openclaw/homelab/ask', json={'question': ''})
    assert resp.status_code == 422


def test_ask_rejects_missing_question(monkeypatch, mock_snapshot, mock_llm):
    c = _make_client(monkeypatch, scopes=['homelab:read'])
    resp = c.post('/api/openclaw/homelab/ask', json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_ask_returns_answer(monkeypatch, mock_snapshot, mock_llm):
    c = _make_client(monkeypatch, scopes=['homelab:read'])
    resp = c.post('/api/openclaw/homelab/ask', json={'question': 'why is immich slow?'})
    assert resp.status_code == 200
    data = resp.json()
    assert data['status'] == 'ok'
    assert 'answer' in data
    assert data['answer'] == 'Immich is healthy. Disk at 42%.'
    assert data['question'] == 'why is immich slow?'


def test_ask_echoes_question(monkeypatch, mock_snapshot, mock_llm):
    c = _make_client(monkeypatch, scopes=['homelab:read'])
    q = 'what containers restarted today?'
    resp = c.post('/api/openclaw/homelab/ask', json={'question': q})
    assert resp.status_code == 200
    assert resp.json()['question'] == q


# ---------------------------------------------------------------------------
# Snapshot content reaches the LLM prompt
# ---------------------------------------------------------------------------

def test_snapshot_injected_into_prompt(monkeypatch, mock_snapshot):
    """System prompt passed to LLM must contain snapshot data."""
    captured_messages = []

    async def _fake_llm(url, model, messages, **kw):
        captured_messages.extend(messages)
        return 'ok'

    monkeypatch.setattr(
        'routes.openclaw_homelab_routes.resolve_endpoint',
        lambda kind, owner=None: ('http://llm.local/v1', 'test-model', {}),
    )
    monkeypatch.setattr('routes.openclaw_homelab_routes.llm_call_async', _fake_llm)

    c = _make_client(monkeypatch, scopes=['homelab:read'])
    c.post('/api/openclaw/homelab/ask', json={'question': 'disk usage?'})

    system_content = next(
        (m['content'] for m in captured_messages if m.get('role') == 'system'), ''
    )
    assert 'immich' in system_content.lower() or 'ok' in system_content.lower()
    assert '42' in system_content


def test_open_events_in_prompt(monkeypatch, mock_snapshot):
    """Open events must appear in the system prompt."""
    captured = []

    async def _fake_llm(url, model, messages, **kw):
        captured.extend(messages)
        return 'ok'

    monkeypatch.setattr(
        'routes.openclaw_homelab_routes.resolve_endpoint',
        lambda kind, owner=None: ('http://llm.local/v1', 'test-model', {}),
    )
    monkeypatch.setattr('routes.openclaw_homelab_routes.llm_call_async', _fake_llm)

    c = _make_client(monkeypatch, scopes=['homelab:read'])
    c.post('/api/openclaw/homelab/ask', json={'question': 'any incidents?'})

    system_content = next(
        (m['content'] for m in captured if m.get('role') == 'system'), ''
    )
    assert 'caddy down' in system_content


# ---------------------------------------------------------------------------
# LLM not configured
# ---------------------------------------------------------------------------

def test_ask_503_when_no_endpoint(monkeypatch, mock_snapshot):
    def _no_endpoint(kind, owner=None):
        raise Exception('No model configured')

    monkeypatch.setattr('routes.openclaw_homelab_routes.resolve_endpoint', _no_endpoint)

    c = _make_client(monkeypatch, scopes=['homelab:read'])
    resp = c.post('/api/openclaw/homelab/ask', json={'question': 'is tailscale up?'})
    assert resp.status_code == 503
    assert 'model' in resp.json()['detail'].lower() or 'endpoint' in resp.json()['detail'].lower() or 'configured' in resp.json()['detail'].lower()


# ---------------------------------------------------------------------------
# n8n not configured — snapshot degrades gracefully
# ---------------------------------------------------------------------------

def test_ask_works_when_n8n_not_configured(monkeypatch, mock_snapshot, mock_llm):
    n8n_client = MagicMock()
    n8n_client.configured = False
    monkeypatch.setattr('routes.openclaw_homelab_routes.N8nClient', lambda: n8n_client)

    c = _make_client(monkeypatch, scopes=['homelab:read'])
    resp = c.post('/api/openclaw/homelab/ask', json={'question': 'n8n status?'})
    assert resp.status_code == 200
