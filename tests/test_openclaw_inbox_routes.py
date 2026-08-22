import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes.openclaw_inbox_routes import SubmitTicketRequest, setup_openclaw_inbox_routes


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


@pytest.fixture()
def inbox_state(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.openclaw_inbox_routes.DATA_DIR", str(tmp_path))
    state = {
        "total_unread": 3,
        "total_urgent": 2,
        "max_score": 3,
        "per_uid": {
            "acct-1:101": {
                "score": 3,
                "subject": "Production incident",
                "from": "Ops Team",
                "reason": "container down",
                "tags": ["work"],
            },
            "acct-1:102": {
                "score": 2,
                "subject": "Needs reply",
                "from": "Customer",
                "reason": "waiting on answer",
                "tags": ["work"],
            },
            "acct-1:103": {
                "score": 1,
                "subject": "FYI",
                "from": "Newsletter",
                "reason": "info",
                "tags": ["newsletter"],
            },
            "acct-1:104": {
                "score": 3,
                "subject": "",
                "from": "",
                "reason": "",
                "tags": [],
            },
        },
    }
    (tmp_path / "email_urgency_state_alice.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_triage_lists_urgent_items(inbox_state):
    triage = _endpoint(setup_openclaw_inbox_routes(), "/api/openclaw/inbox/triage", "GET")
    data = await triage(_request(["email:read"]))
    assert data["status"] == "ok"
    assert data["total_unread"] == 3
    assert len(data["items"]) == 3
    assert data["items"][0]["tier"] == "urgent"
    assert data["items"][0]["actions"][:2] == ["ack", "mute_sender_2h"]
    assert "forward_to_team" not in data["items"][0]["actions"]


@pytest.mark.asyncio
async def test_triage_requires_email_read_scope(inbox_state):
    triage = _endpoint(setup_openclaw_inbox_routes(), "/api/openclaw/inbox/triage", "GET")
    with pytest.raises(HTTPException) as exc:
        await triage(_request(["chat"]))
    assert exc.value.status_code == 403
    assert "email:read" in exc.value.detail


@pytest.mark.asyncio
async def test_ack_hides_item_by_default(inbox_state):
    router = setup_openclaw_inbox_routes()
    triage = _endpoint(router, "/api/openclaw/inbox/triage", "GET")
    ack = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/ack", "POST")
    before = await triage(_request(["email:read"]))
    item_id = before["items"][0]["id"]
    result = await ack(_request(["email:read"]), item_id)
    assert result["status"] == "ok"
    after = await triage(_request(["email:read"]))
    assert item_id not in [item["id"] for item in after["items"]]
    with_ack = await triage(_request(["email:read"]), include_acknowledged=True)
    assert item_id in [item["id"] for item in with_ack["items"]]


@pytest.mark.asyncio
async def test_mute_sender_hides_matching_sender(inbox_state):
    router = setup_openclaw_inbox_routes()
    triage = _endpoint(router, "/api/openclaw/inbox/triage", "GET")
    mute = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/mute-sender", "POST")
    before = await triage(_request(["email:read"]))
    item_id = next(item["id"] for item in before["items"] if item["from"] == "Customer")
    result = await mute(_request(["email:read"]), item_id, None)
    assert result["status"] == "ok"
    after = await triage(_request(["email:read"]))
    assert "Customer" not in [item["from"] for item in after["items"]]
    with_muted = await triage(_request(["email:read"]), include_muted=True)
    assert "Customer" in [item["from"] for item in with_muted["items"]]


@pytest.mark.asyncio
async def test_summary_returns_cached_triage_context(inbox_state):
    router = setup_openclaw_inbox_routes()
    triage = _endpoint(router, "/api/openclaw/inbox/triage", "GET")
    summary = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/summary", "GET")
    before = await triage(_request(["email:read"]))
    item_id = next(item["id"] for item in before["items"] if item["subject"] == "Production incident")
    result = await summary(_request(["email:read"]), item_id)
    assert result["status"] == "ok"
    assert result["requires_approval"] is False
    assert "Production incident" in result["summary"]
    assert "container down" in result["summary"]


@pytest.mark.asyncio
async def test_redmine_ticket_draft_requires_approval_and_does_not_submit(inbox_state):
    router = setup_openclaw_inbox_routes()
    triage = _endpoint(router, "/api/openclaw/inbox/triage", "GET")
    draft = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/draft", "POST")
    before = await triage(_request(["email:read"]))
    item_id = next(item["id"] for item in before["items"] if item["subject"] == "Production incident")
    result = await draft(_request(["email:read"]), item_id)
    assert result["status"] == "ok"
    assert result["requires_approval"] is True
    assert result["draft"]["subject"] == "Production incident"
    assert result["draft"]["source"] == "openclaw_inbox_triage"
    assert "This is a draft only" in result["draft"]["description"]


@pytest.mark.asyncio
async def test_submit_refuses_unparseable_email(inbox_state, monkeypatch):
    """Regression: a message whose subject/body/sender could not be extracted
    (empty subject, no sender, no reason/body signal) must never be auto-
    submitted as a Redmine ticket, even though it scored high enough to
    appear in the urgent triage list. This is the second line of defense
    behind Converge's own 422 rejection of "(no subject)" tickets."""
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.example")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")

    async def _unexpected_post(*args, **kwargs):
        raise AssertionError("must not call Converge for an unparseable message")

    monkeypatch.setattr("httpx.AsyncClient.post", _unexpected_post)

    router = setup_openclaw_inbox_routes()
    triage = _endpoint(router, "/api/openclaw/inbox/triage", "GET")
    submit = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/submit", "POST")
    before = await triage(_request(["email:read", "converge:write"]))
    item_id = next(item["id"] for item in before["items"] if item["subject"] == "(no subject)")

    with pytest.raises(HTTPException) as exc_info:
        await submit(
            _request(["email:read", "converge:write"]),
            item_id,
            SubmitTicketRequest(confirm=True),
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_submit_allows_explicit_subject_override_for_low_signal_item(inbox_state, monkeypatch):
    """A human/agent that explicitly supplies real subject+description content
    for a low-signal item should still be able to submit — the guard only
    blocks the fully-unattended, fully-empty case."""
    monkeypatch.setenv("CONVERGE_TICKET_CREATE_PATH", "/api/external/tickets")
    monkeypatch.setenv("CONVERGE_BASE_URL", "http://converge.example")
    monkeypatch.setenv("CONVERGE_API_KEY", "test-key")

    class _Resp:
        status_code = 200

        def json(self):
            return {"issue": {"id": 42, "url": "http://converge.example/issues/42"}}

    async def _fake_post(self, url, headers=None, json=None):
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    router = setup_openclaw_inbox_routes()
    triage = _endpoint(router, "/api/openclaw/inbox/triage", "GET")
    submit = _endpoint(router, "/api/openclaw/inbox/triage/{item_id}/redmine-ticket/submit", "POST")
    before = await triage(_request(["email:read", "converge:write"]))
    item_id = next(item["id"] for item in before["items"] if item["subject"] == "(no subject)")

    result = await submit(
        _request(["email:read", "converge:write"]),
        item_id,
        SubmitTicketRequest(confirm=True, subject="Reviewed manually: follow up needed", description="Human reviewed this and confirmed action is needed."),
    )
    assert result["status"] == "ok"
    assert result["issue_id"] == 42
