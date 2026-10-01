"""Converge integration routes — /api/converge/*.

Small HTTP surface for the Converge (redmine-dashboard) calendar-timelog
bridge (SHA-172). Reuses the existing calendar router's handler and
enforces the ``calendar:read`` scope before touching user data, the same
pattern as routes/codex_routes.py's calendar block.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user

CALENDAR_READ_SCOPES = {"calendar:read", "calendar:write"}


def _find_endpoint(router: APIRouter | None, method: str, path: str):
    if router is None:
        return None
    for route in getattr(router, "routes", []):
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    return None


def _scope_owner(request: Request, allowed: set[str]) -> str:
    """Return the data owner if the caller is allowed for this Converge action."""
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if not scopes.intersection(allowed):
            required = " or ".join(sorted(allowed))
            raise HTTPException(403, f"API token missing required scope: {required}")
        owner = getattr(request.state, "api_token_owner", None)
        if not owner:
            raise HTTPException(403, "API token has no owner")
        return owner
    return require_user(request)


async def _as_owner(request: Request, owner: str, fn, *args, **kwargs):
    """Run an existing route handler with request.state.current_user temporarily
    set to ``owner`` so its internal get_current_user/require_user calls see
    the scope-gated owner (not the "api" pseudo-user the bearer middleware sets).
    Restores the original value when done. Works for sync and async handlers."""
    orig = getattr(request.state, "current_user", None)
    orig_api_token = getattr(request.state, "api_token", None)
    request.state.current_user = owner
    request.state.api_token = False
    try:
        result = fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    finally:
        request.state.current_user = orig
        if orig_api_token is None:
            try:
                delattr(request.state, "api_token")
            except AttributeError:
                pass
        else:
            request.state.api_token = orig_api_token


def setup_converge_calendar_routes(
    calendar_router: APIRouter | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/converge", tags=["converge"])
    calendar_list_events = _find_endpoint(calendar_router, "GET", "/api/calendar/events")

    @router.get("/calendar/events")
    async def converge_calendar_list(request: Request, start: str, end: str):
        owner = _scope_owner(request, CALENDAR_READ_SCOPES)
        if calendar_list_events is None:
            raise HTTPException(503, "Calendar integration is not available")
        return await _as_owner(request, owner, calendar_list_events, request, start, end, "")

    return router
