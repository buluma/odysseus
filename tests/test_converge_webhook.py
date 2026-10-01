"""Tests for the Converge inbound webhook endpoint."""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from routes.openclaw_converge_webhook_routes import setup_converge_webhook_routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECRET = "test-webhook-secret-abc123"

_TICKET = {
    "id": "ticket-1",
    "redmineIssueId": 42,
    "subject": "Server is on fire",
    "description": "Critical alert",
    "projectName": "Homelab",
    "trackerName": "Bug",
    "statusName": "New",
    "priorityName": "High",
    "assignedToId": "user-1",
    "assignedToName": "Mike",
    "authorId": "user-1",
    "authorName": "Mike",
    "dueDate": None,
    "doneRatio": 0,
    "createdAt": "2026-06-14T01:00:00Z",
    "updatedAt": "2026-06-14T01:00:00Z",
}


def _payload(event: str, changes=None) -> dict:
    return {
        "id": "wh_123_abc",
        "event": event,
        "timestamp": "2026-06-14T01:00:00Z",
        "ticket": _TICKET,
        "changes": changes,
    }


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _endpoint(router: APIRouter, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _mock_request(body: bytes) -> MagicMock:
    req = MagicMock()
    async def _body():
        return body
    req.body = _body
    return req


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_signature_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("CONVERGE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))

    router = setup_converge_webhook_routes()
    handler = _endpoint(router, "/api/openclaw/converge/webhook", "POST")

    body = json.dumps(_payload("ticket.created")).encode()
    sig = _sign(body, SECRET)

    with patch("routes.openclaw_converge_webhook_routes.EventStore") as MockStore:
        instance = MockStore.return_value
        instance.record_event.return_value = {"id": "ev-1", "severity": "info"}
        result = await handler(request=_mock_request(body), x_webhook_signature=sig)

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_missing_signature_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("CONVERGE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))

    router = setup_converge_webhook_routes()
    handler = _endpoint(router, "/api/openclaw/converge/webhook", "POST")

    body = json.dumps(_payload("ticket.created")).encode()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await handler(request=_mock_request(body), x_webhook_signature=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_signature_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("CONVERGE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))

    router = setup_converge_webhook_routes()
    handler = _endpoint(router, "/api/openclaw/converge/webhook", "POST")

    body = json.dumps(_payload("ticket.created")).encode()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await handler(request=_mock_request(body), x_webhook_signature="sha256=deadbeef")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_no_secret_configured_rejects(monkeypatch, tmp_path):
    monkeypatch.delenv("CONVERGE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))

    router = setup_converge_webhook_routes()
    handler = _endpoint(router, "/api/openclaw/converge/webhook", "POST")

    body = json.dumps(_payload("ticket.created")).encode()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await handler(request=_mock_request(body), x_webhook_signature="sha256=anything")
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# Event routing — what gets stored
# ---------------------------------------------------------------------------

async def _call(event: str, changes=None, monkeypatch=None, tmp_path=None):
    monkeypatch.setenv("CONVERGE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))

    router = setup_converge_webhook_routes()
    handler = _endpoint(router, "/api/openclaw/converge/webhook", "POST")

    body = json.dumps(_payload(event, changes)).encode()
    sig = _sign(body, SECRET)

    with patch("routes.openclaw_converge_webhook_routes.EventStore") as MockStore, \
         patch("routes.openclaw_converge_webhook_routes.notify_new_event") as mock_notify:
        instance = MockStore.return_value
        instance.record_event.return_value = {"id": "ev-1", "severity": "info"}
        result = await handler(request=_mock_request(body), x_webhook_signature=sig)
        return result, instance, mock_notify


@pytest.mark.asyncio
async def test_ticket_created_stores_event(monkeypatch, tmp_path):
    result, store, _ = await _call("ticket.created", monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result["ok"] is True
    store.record_event.assert_called_once()
    call_kwargs = store.record_event.call_args
    assert "ticket.created" in str(call_kwargs) or "created" in str(call_kwargs)


@pytest.mark.asyncio
async def test_ticket_assigned_stores_event_and_notifies(monkeypatch, tmp_path):
    result, store, notify = await _call("ticket.assigned", monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result["ok"] is True
    store.record_event.assert_called_once()
    notify.assert_called_once()


@pytest.mark.asyncio
async def test_ticket_status_changed_stores_event(monkeypatch, tmp_path):
    changes = [{"field": "status", "oldValue": "New", "newValue": "In Progress"}]
    result, store, _ = await _call("ticket.status_changed", changes=changes, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result["ok"] is True
    store.record_event.assert_called_once()


@pytest.mark.asyncio
async def test_ticket_completed_stores_event(monkeypatch, tmp_path):
    result, store, _ = await _call("ticket.completed", monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result["ok"] is True
    store.record_event.assert_called_once()


@pytest.mark.asyncio
async def test_ticket_deleted_stores_warning_event(monkeypatch, tmp_path):
    result, store, _ = await _call("ticket.deleted", monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result["ok"] is True
    store.record_event.assert_called_once()
    args = store.record_event.call_args
    # severity should be warning for deletions
    assert "warning" in str(args)


@pytest.mark.asyncio
async def test_ticket_updated_skipped(monkeypatch, tmp_path):
    result, store, notify = await _call("ticket.updated", monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result["ok"] is True
    store.record_event.assert_not_called()
    notify.assert_not_called()


# ---------------------------------------------------------------------------
# Auth middleware exemption
# ---------------------------------------------------------------------------

def test_converge_webhook_path_is_auth_exempt():
    """Pin that /api/openclaw/converge/webhook is in AUTH_EXEMPT_EXACT.

    Converge cannot supply a session cookie; the HMAC signature in
    X-Webhook-Signature is the credential. Without this exemption
    AuthMiddleware rejects every delivery with 401 before the signature
    is ever checked.
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
    assert "/api/openclaw/converge/webhook" in body, (
        "/api/openclaw/converge/webhook must be in AUTH_EXEMPT_EXACT — "
        "Converge deliveries are rejected with 401 without it"
    )
