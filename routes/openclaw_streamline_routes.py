"""OpenClaw Streamline sync watcher routes.

Provides read/write access to Streamline log digest and sync trigger
via the Converge external API.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes.homelab_routes import _scope_owner

BASE_URL = "/api/openclaw/streamline"

CONVERGE_READ_SCOPES = {"converge:read"}
CONVERGE_WRITE_SCOPES = {"converge:write"}


class StreamlineSyncRequest(BaseModel):
    confirm: bool = False


def _converge_config() -> tuple[str, str]:
    base_url = (os.getenv("CONVERGE_BASE_URL") or "").strip().rstrip("/")
    api_key = (os.getenv("CONVERGE_API_KEY") or "").strip()
    if not base_url or not api_key:
        raise HTTPException(503, "Converge is not configured (CONVERGE_BASE_URL / CONVERGE_API_KEY missing)")
    return base_url, api_key


def setup_openclaw_streamline_routes() -> APIRouter:
    router = APIRouter(prefix=BASE_URL, tags=["openclaw-streamline"])

    @router.get("/digest")
    async def streamline_digest(request: Request) -> dict[str, Any]:
        """Return Streamline log digest from Converge external API. Requires: converge:read."""
        _scope_owner(request, CONVERGE_READ_SCOPES)
        base_url, api_key = _converge_config()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base_url}/api/external/logs/digest",
                headers={"X-API-Key": api_key},
            )

        if resp.status_code >= 400:
            raise HTTPException(502, f"Converge returned HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        mbu = data.get("mbu_logs", {})
        ssr = data.get("server_side_rules_log", {})

        by_status = ssr.get("by_status", {})
        error_count = by_status.get("error", 0)

        return {
            "status": "ok",
            "message": (
                f"mbu_logs: {mbu.get('total', 0)} total. "
                f"server_side_rules_log: {ssr.get('total', 0)} total, {error_count} error(s)."
            ),
            "mbu_logs": {
                "total": mbu.get("total"),
                "by_level": mbu.get("by_level", {}),
                "recent_errors": mbu.get("recent_errors", []),
                "last_ingested_at": mbu.get("last_ingested_at"),
            },
            "server_side_rules_log": {
                "total": ssr.get("total"),
                "by_status": by_status,
                "error_count": error_count,
                "recent_errors": ssr.get("recent_errors", []),
                "last_ingested_at": ssr.get("last_ingested_at"),
            },
        }

    @router.get("/sync-status")
    async def streamline_sync_status(request: Request) -> dict[str, Any]:
        """Return latest Converge sync job status. Requires: converge:read."""
        _scope_owner(request, CONVERGE_READ_SCOPES)
        base_url, api_key = _converge_config()

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base_url}/api/external/sync",
                headers={"X-API-Key": api_key},
            )

        if resp.status_code >= 400:
            raise HTTPException(502, f"Converge returned HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return {
            "status": "ok",
            "latest_job": data.get("latest_job"),
            "state": data.get("state"),
        }

    @router.post("/sync")
    async def streamline_sync(request: Request, body: StreamlineSyncRequest) -> dict[str, Any]:
        """Trigger a full Converge sync. Requires: converge:write + confirm=true."""
        _scope_owner(request, CONVERGE_WRITE_SCOPES)
        if not body.confirm:
            raise HTTPException(400, "Write action requires confirm=true")

        base_url, api_key = _converge_config()

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base_url}/api/external/sync",
                headers={"X-API-Key": api_key},
            )

        if resp.status_code >= 400:
            raise HTTPException(502, f"Converge returned HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return {
            "status": "ok",
            "message": f"Sync triggered. Job ID: {data.get('job_id')}.",
            "job_id": data.get("job_id"),
            "job": data.get("job"),
            "requires_approval": False,
        }

    return router
