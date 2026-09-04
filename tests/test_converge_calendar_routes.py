"""Tests for GET /api/converge/calendar/events (SHA-172)."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from routes.converge_calendar_routes import setup_converge_calendar_routes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_calendar_router(events=None, error=None):
    """Build a stand-in calendar_router exposing GET /api/calendar/events,
    the same shape converge_calendar_routes looks up via _find_endpoint."""
    router = APIRouter()

    @router.get("/api/calendar/events")
    async def list_events(request: Request, start: str, end: str, calendar: str = ""):
        if error is not None:
            raise error
        return {"events": events or []}

    return router


def _make_client(scopes: list[str], calendar_router=None, owner="alice"):
    class _AuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            scope["state"] = {
                "api_token": True,
                "api_token_scopes": scopes,
                "api_token_owner": owner,
            }
            return await self.app(scope, receive, send)

    app = FastAPI()
    app.add_middleware(_AuthMiddleware)
    app.include_router(setup_converge_calendar_routes(calendar_router=calendar_router))
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/converge/calendar/events
# ---------------------------------------------------------------------------

def test_requires_calendar_read_scope():
    c = _make_client(scopes=["wrong:scope"], calendar_router=_make_calendar_router())
    resp = c.get("/api/converge/calendar/events?start=2026-09-01&end=2026-09-02")
    assert resp.status_code == 403


def test_calendar_write_scope_also_allowed():
    events = [{"uid": "e1", "summary": "1:1 sync", "dtstart": "2026-09-04T10:00:00", "dtend": "2026-09-04T10:30:00"}]
    c = _make_client(scopes=["calendar:write"], calendar_router=_make_calendar_router(events=events))
    resp = c.get("/api/converge/calendar/events?start=2026-09-01&end=2026-09-02")
    assert resp.status_code == 200
    assert resp.json()["events"] == events


def test_returns_events_from_calendar_router():
    events = [
        {"uid": "e1", "summary": "Standup", "dtstart": "2026-09-04T09:00:00", "dtend": "2026-09-04T09:15:00"},
        {"uid": "e2", "summary": "Planning", "dtstart": "2026-09-04T11:00:00", "dtend": "2026-09-04T12:00:00"},
    ]
    c = _make_client(scopes=["calendar:read"], calendar_router=_make_calendar_router(events=events))
    resp = c.get("/api/converge/calendar/events?start=2026-09-01&end=2026-09-05")
    assert resp.status_code == 200
    assert resp.json() == {"events": events}


def test_unavailable_when_calendar_router_not_wired():
    c = _make_client(scopes=["calendar:read"], calendar_router=None)
    resp = c.get("/api/converge/calendar/events?start=2026-09-01&end=2026-09-02")
    assert resp.status_code == 503


def test_propagates_calendar_router_errors():
    c = _make_client(
        scopes=["calendar:read"],
        calendar_router=_make_calendar_router(error=HTTPException(500, "boom")),
    )
    resp = c.get("/api/converge/calendar/events?start=2026-09-01&end=2026-09-02")
    assert resp.status_code == 500
