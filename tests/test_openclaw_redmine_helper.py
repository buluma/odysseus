"""Tests for Redmine helper endpoints — create ticket, add note, digest, inbox submit."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes.openclaw_bridge_routes import setup_openclaw_bridge_routes
from routes.openclaw_inbox_routes import _encode_key, setup_openclaw_inbox_routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bridge_request(scopes, owner="alice"):
    state = SimpleNamespace(
        api_token=True,
        api_token_scopes=scopes or [],
        api_token_owner=owner,
        current_user="api",
    )
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)))


def _inbox_request(scopes=None, owner="alice"):
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


class FakeSessionManager:
    def get_session(self, session_id):
        raise KeyError(session_id)

    def create_session(self, **kwargs):
        return SimpleNamespace(id="s1", headers={}, history=[], endpoint_url="http://llm", model="m")

    def save_sessions(self):
        pass


class FakeChatHandler:
    async def handle_memory_command(self, sess, message):
        return None

    async def preprocess_message(self, message, att_ids, sess, **kwargs):
        return message, message, message, [], []

    def validate_and_extract_preset(self, preset_id):
        return 0.1, 100, None, None

    def update_session_name_if_needed(self, sess, text):
        return None


class FakeChatProcessor:
    def build_context_preface(self, **kwargs):
        return [], [], []


class FakeResearchHandler:
    async def call_research_service(self, *args, **kwargs):
        raise RuntimeError("offline")


def _bridge_router():
    return setup_openclaw_bridge_routes(
        FakeSessionManager(),
        FakeChatHandler(),
        FakeChatProcessor(),
        memory_manager=None,
        research_handler=FakeResearchHandler(),
    )


class _MockResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class _MockAsyncClient:
    def __init__(self, responses=None, **kwargs):
        self._responses = list(responses or [])
        self._idx = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def _next(self):
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return _MockResponse(200, {})

    async def get(self, url, **kwargs):
        return self._next()

    async def post(self, url, **kwargs):
        return self._next()


# ---------------------------------------------------------------------------
# POST /tickets — create ticket from Slack
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_ticket_success(monkeypatch):
    import httpx
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _MockAsyncClient(
        responses=[_MockResponse(201, {"issue": {"id": 42, "url": "http://converge/issues/42"}})]
    ))

    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets", "POST")
    from routes.openclaw_bridge_routes import CreateTicketRequest
    result = await ep(
        _bridge_request(["converge:write"]),
        CreateTicketRequest(subject="Test ticket", description="Details here", confirm=True),
    )
    assert result["status"] == "ok"
    assert result["issue_id"] == 42
    assert "42" in result.get("url", "http://converge/issues/42")


@pytest.mark.asyncio
async def test_create_ticket_requires_converge_write_scope(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets", "POST")
    from routes.openclaw_bridge_routes import CreateTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:read"]), CreateTicketRequest(subject="x", description="y", confirm=True))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_ticket_requires_confirm(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets", "POST")
    from routes.openclaw_bridge_routes import CreateTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:write"]), CreateTicketRequest(subject="x", description="y", confirm=False))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_ticket_converge_not_configured(monkeypatch):
    monkeypatch.delenv("CONVERGE_BASE_URL", raising=False)
    monkeypatch.delenv("CONVERGE_API_KEY", raising=False)
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets", "POST")
    from routes.openclaw_bridge_routes import CreateTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:write"]), CreateTicketRequest(subject="x", description="y", confirm=True))
    assert exc.value.status_code in (501, 503)


@pytest.mark.asyncio
async def test_create_ticket_converge_error_propagates(monkeypatch):
    import httpx
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _MockAsyncClient(
        responses=[_MockResponse(500, {"error": "db down"})]
    ))
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets", "POST")
    from routes.openclaw_bridge_routes import CreateTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:write"]), CreateTicketRequest(subject="x", description="y", confirm=True))
    assert exc.value.status_code == 500


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/notes — add incident thread as note
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_note_success(monkeypatch):
    import httpx
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _MockAsyncClient(
        responses=[_MockResponse(200, {"note_id": 7, "status": "added"})]
    ))
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets/{ticket_id}/notes", "POST")
    from routes.openclaw_bridge_routes import AddTicketNoteRequest
    result = await ep(
        _bridge_request(["converge:write"]),
        "123",
        AddTicketNoteRequest(body="Incident resolved at 14:22 UTC.", confirm=True),
    )
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_add_note_requires_converge_write_scope(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets/{ticket_id}/notes", "POST")
    from routes.openclaw_bridge_routes import AddTicketNoteRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:read"]), "123", AddTicketNoteRequest(body="note", confirm=True))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_add_note_requires_confirm(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets/{ticket_id}/notes", "POST")
    from routes.openclaw_bridge_routes import AddTicketNoteRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:write"]), "123", AddTicketNoteRequest(body="note", confirm=False))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_add_note_converge_error_propagates(monkeypatch):
    import httpx
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _MockAsyncClient(
        responses=[_MockResponse(500, {"error": "not found"})]
    ))
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets/{ticket_id}/notes", "POST")
    from routes.openclaw_bridge_routes import AddTicketNoteRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:write"]), "99", AddTicketNoteRequest(body="note", confirm=True))
    assert exc.value.status_code == 500


# ---------------------------------------------------------------------------
# GET /tickets/digest — open / stale / assigned digest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_digest_returns_open_stale_assigned(monkeypatch):
    import httpx
    import datetime
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")

    old_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tickets = [
        {"id": 1, "subject": "Stale issue", "status": "open", "updated_on": old_date, "assigned_to": None},
        {"id": 2, "subject": "Fresh issue", "status": "open", "updated_on": fresh_date, "assigned_to": None},
        {"id": 3, "subject": "My issue", "status": "open", "updated_on": fresh_date, "assigned_to": "alice"},
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _MockAsyncClient(
        responses=[_MockResponse(200, {"tickets": tickets, "total": 3})]
    ))
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets/digest", "GET")
    result = await ep(_bridge_request(["converge:read"], owner="alice"))
    assert result["status"] == "ok"
    assert len(result["open"]) == 3
    stale_ids = [t["id"] for t in result["stale"]]
    assert 1 in stale_ids
    assert 2 not in stale_ids
    assigned_ids = [t["id"] for t in result["assigned"]]
    assert 3 in assigned_ids


@pytest.mark.asyncio
async def test_digest_requires_converge_read_scope(monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets/digest", "GET")
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["chat"]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_digest_converge_not_configured(monkeypatch):
    monkeypatch.delenv("CONVERGE_BASE_URL", raising=False)
    monkeypatch.delenv("CONVERGE_API_KEY", raising=False)
    router = _bridge_router()
    ep = _endpoint(router, "/api/openclaw/tickets/digest", "GET")
    with pytest.raises(HTTPException) as exc:
        await ep(_bridge_request(["converge:read"]))
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# POST /triage/{item_id}/redmine-ticket/submit — inbox routes submit
# ---------------------------------------------------------------------------

@pytest.fixture()
def inbox_state(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.openclaw_inbox_routes.DATA_DIR", str(tmp_path))
    state = {
        "total_unread": 1,
        "total_urgent": 1,
        "max_score": 3,
        "per_uid": {
            "acct-1:101": {
                "score": 3,
                "subject": "Server down",
                "from": "Ops Team <ops@example.com>",
                "reason": "critical incident",
                "tags": ["work"],
            }
        },
    }
    (tmp_path / "email_urgency_state_alice.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_triage_submit_success(inbox_state, monkeypatch):
    import httpx
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _MockAsyncClient(
        responses=[_MockResponse(201, {"issue": {"id": 55, "url": "http://converge/issues/55"}})]
    ))
    router = setup_openclaw_inbox_routes()
    ep = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/submit", "POST")
    from routes.openclaw_inbox_routes import SubmitTicketRequest
    result = await ep(
        _inbox_request(["email:read", "converge:write"]),
        _encode_key("acct-1:101"),
        SubmitTicketRequest(confirm=True),
    )
    assert result["status"] == "ok"
    assert result["issue_id"] == 55


@pytest.mark.asyncio
async def test_triage_submit_requires_confirm(inbox_state, monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    router = setup_openclaw_inbox_routes()
    ep = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/submit", "POST")
    from routes.openclaw_inbox_routes import SubmitTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_inbox_request(["email:read", "converge:write"]), "acct-1:101", SubmitTicketRequest(confirm=False))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_triage_submit_item_not_found(inbox_state, monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    router = setup_openclaw_inbox_routes()
    ep = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/submit", "POST")
    from routes.openclaw_inbox_routes import SubmitTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_inbox_request(["email:read", "converge:write"]), _encode_key("acct-1:999"), SubmitTicketRequest(confirm=True))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_triage_submit_converge_not_configured(inbox_state, monkeypatch):
    monkeypatch.delenv("CONVERGE_BASE_URL", raising=False)
    monkeypatch.delenv("CONVERGE_API_KEY", raising=False)
    router = setup_openclaw_inbox_routes()
    ep = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/submit", "POST")
    from routes.openclaw_inbox_routes import SubmitTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_inbox_request(["email:read", "converge:write"]), "acct-1:101", SubmitTicketRequest(confirm=True))
    assert exc.value.status_code in (501, 503)


@pytest.mark.asyncio
async def test_triage_submit_requires_converge_write_scope(inbox_state, monkeypatch):
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge")
    monkeypatch.setenv("CONVERGE_API_KEY", "secret")
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    router = setup_openclaw_inbox_routes()
    ep = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/submit", "POST")
    from routes.openclaw_inbox_routes import SubmitTicketRequest
    with pytest.raises(HTTPException) as exc:
        await ep(_inbox_request(["email:read"]), "acct-1:101", SubmitTicketRequest(confirm=True))
    assert exc.value.status_code == 403
