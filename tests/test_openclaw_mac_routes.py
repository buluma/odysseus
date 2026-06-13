"""Tests for OpenClaw Mac automation routes."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from routes.openclaw_mac_routes import (
    _run_shortcut,
    _read_focus_state,
    _write_focus_state,
    setup_openclaw_mac_routes,
    MAC_CONTROL_SCOPES,
)
from routes.api_token_routes import ALLOWED_SCOPES, TOKEN_PROFILES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request(*, api_token=True, scopes=None, owner="alice"):
    state = SimpleNamespace(
        api_token=api_token,
        api_token_scopes=scopes or [],
        api_token_owner=owner,
        current_user="browser-user",
    )
    return SimpleNamespace(state=state)


def _endpoint(router, path: str, method: str = "GET"):
    for route in router.routes:
        if route.path == path and method in [m.upper() for m in route.methods]:
            return route.endpoint
    raise KeyError(f"{method} {path} not found in router")


# ---------------------------------------------------------------------------
# Scope registration
# ---------------------------------------------------------------------------

def test_mac_control_scope_in_allowed_scopes():
    assert "mac:control" in ALLOWED_SCOPES


def test_openclaw_bridge_profile_has_mac_control():
    assert "mac:control" in TOKEN_PROFILES["openclaw_bridge"]


# ---------------------------------------------------------------------------
# _run_shortcut
# ---------------------------------------------------------------------------

def test_run_shortcut_success(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run_shortcut("Focus On")  # should not raise


def test_run_shortcut_not_found_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Shortcut not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(HTTPException) as exc_info:
        _run_shortcut("Focus On")
    assert exc_info.value.status_code == 503


def test_run_shortcut_os_error_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("shortcuts not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(HTTPException) as exc_info:
        _run_shortcut("Focus On")
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# _read_focus_state / _write_focus_state
# ---------------------------------------------------------------------------

def test_focus_state_roundtrip(tmp_path, monkeypatch):
    state_file = tmp_path / "mac_focus.json"
    monkeypatch.setenv("MAC_FOCUS_STATE_FILE", str(state_file))
    _write_focus_state(True)
    assert _read_focus_state() is True
    _write_focus_state(False)
    assert _read_focus_state() is False


def test_read_focus_state_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_FOCUS_STATE_FILE", str(tmp_path / "nonexistent.json"))
    assert _read_focus_state() is None


def test_write_focus_state_creates_parent_dirs(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "mac_focus.json"
    monkeypatch.setenv("MAC_FOCUS_STATE_FILE", str(nested))
    _write_focus_state(True)
    assert nested.exists()


# ---------------------------------------------------------------------------
# /focus endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_focus_requires_mac_control_scope():
    router = setup_openclaw_mac_routes()
    ep = _endpoint(router, "/api/openclaw/mac/focus", "POST")
    req = _request(scopes=["chat"])
    with pytest.raises(HTTPException) as exc_info:
        await ep(req)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_focus_mode_on(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_FOCUS_STATE_FILE", str(tmp_path / "mac_focus.json"))
    monkeypatch.setenv("MAC_DND_ON_SHORTCUT", "DND On")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    router = setup_openclaw_mac_routes()
    ep = _endpoint(router, "/api/openclaw/mac/focus", "POST")
    req = _request(scopes=["mac:control"])
    result = await ep(req)

    assert result["status"] == "ok"
    assert result["focus"] is True
    assert _read_focus_state() is True
    assert any("DND On" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# /standup endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_standup_mode_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_FOCUS_STATE_FILE", str(tmp_path / "mac_focus.json"))
    monkeypatch.setenv("MAC_DND_OFF_SHORTCUT", "DND Off")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    router = setup_openclaw_mac_routes()
    ep = _endpoint(router, "/api/openclaw/mac/standup", "POST")
    req = _request(scopes=["mac:control"])
    result = await ep(req)

    assert result["status"] == "ok"
    assert result["focus"] is False
    assert _read_focus_state() is False
    assert any("DND Off" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# /status endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_returns_known_state(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_FOCUS_STATE_FILE", str(tmp_path / "mac_focus.json"))
    _write_focus_state(True)

    router = setup_openclaw_mac_routes()
    ep = _endpoint(router, "/api/openclaw/mac/status", "GET")
    req = _request(scopes=["mac:control"])
    result = await ep(req)

    assert result["status"] == "ok"
    assert result["focus"] is True


@pytest.mark.asyncio
async def test_status_unknown_when_no_state_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_FOCUS_STATE_FILE", str(tmp_path / "nonexistent.json"))

    router = setup_openclaw_mac_routes()
    ep = _endpoint(router, "/api/openclaw/mac/status", "GET")
    req = _request(scopes=["mac:control"])
    result = await ep(req)

    assert result["status"] == "ok"
    assert result["focus"] is None
