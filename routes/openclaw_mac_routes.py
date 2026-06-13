"""OpenClaw Mac automation routes — DND / Focus mode control."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from routes.homelab_routes import _has_scope, _scope_owner

logger = logging.getLogger(__name__)

MAC_CONTROL_SCOPES = {"mac:control"}

_DEFAULT_STATE_FILE = os.path.expanduser("~/.openclaw/state/mac_focus.json")
_DEFAULT_DND_ON_SHORTCUT = "Do Not Disturb On"
_DEFAULT_DND_OFF_SHORTCUT = "Do Not Disturb Off"


def _state_file_path() -> Path:
    return Path(os.getenv("MAC_FOCUS_STATE_FILE") or _DEFAULT_STATE_FILE)


def _read_focus_state() -> bool | None:
    try:
        data = json.loads(_state_file_path().read_text())
        return bool(data.get("focus"))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def _write_focus_state(active: bool) -> None:
    path = _state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"focus": active}))


def _run_shortcut(name: str) -> None:
    try:
        result = subprocess.run(
            ["shortcuts", "run", name],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(503, f"shortcuts CLI unavailable: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[:300]
        raise HTTPException(503, f"Shortcut '{name}' failed (exit {result.returncode}): {detail}")


def setup_openclaw_mac_routes() -> APIRouter:
    router = APIRouter(prefix="/api/openclaw/mac", tags=["openclaw-mac"])

    @router.post("/focus")
    async def focus(request: Request) -> dict[str, Any]:
        """Enable macOS DND / Focus mode."""
        _scope_owner(request, MAC_CONTROL_SCOPES)
        shortcut = os.getenv("MAC_DND_ON_SHORTCUT") or _DEFAULT_DND_ON_SHORTCUT
        _run_shortcut(shortcut)
        _write_focus_state(True)
        logger.info("Mac focus mode enabled via shortcut '%s'", shortcut)
        return {"status": "ok", "focus": True, "message": "Focus mode on. DND enabled."}

    @router.post("/standup")
    async def standup(request: Request) -> dict[str, Any]:
        """Disable macOS DND / Focus mode."""
        _scope_owner(request, MAC_CONTROL_SCOPES)
        shortcut = os.getenv("MAC_DND_OFF_SHORTCUT") or _DEFAULT_DND_OFF_SHORTCUT
        _run_shortcut(shortcut)
        _write_focus_state(False)
        logger.info("Mac focus mode disabled via shortcut '%s'", shortcut)
        return {"status": "ok", "focus": False, "message": "Focus mode off. DND disabled."}

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        """Return last known DND state. None if never set this session."""
        _scope_owner(request, MAC_CONTROL_SCOPES)
        active = _read_focus_state()
        label = "on" if active is True else ("off" if active is False else "unknown")
        return {"status": "ok", "focus": active, "message": f"Focus mode {label}."}

    return router
