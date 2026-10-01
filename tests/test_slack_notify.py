"""Tests for slack_notify helpers."""

from __future__ import annotations

import json
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from src.slack_notify import notify_new_event


def _event(severity="critical", count=1):
    return {
        "id": "abc-123",
        "severity": severity,
        "service": "odysseus",
        "title": "Odysseus is down",
        "summary": "HTTP health check failed",
        "count": count,
    }


def test_no_webhook_configured_is_silent(monkeypatch):
    monkeypatch.delenv("SLACK_EVENTS_WEBHOOK_URL", raising=False)
    # Should not raise even with no webhook
    notify_new_event(_event())


def test_non_critical_severity_skipped(monkeypatch):
    monkeypatch.setenv("SLACK_EVENTS_WEBHOOK_URL", "https://hooks.slack.com/fake")
    calls = []
    with patch("urllib.request.urlopen") as mock_open:
        notify_new_event(_event(severity="info"))
        notify_new_event(_event(severity="warning"))
    mock_open.assert_not_called()


def test_critical_severity_posts(monkeypatch):
    monkeypatch.setenv("SLACK_EVENTS_WEBHOOK_URL", "https://hooks.slack.com/fake")
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        notify_new_event(_event(severity="critical"))
    mock_open.assert_called_once()
    req = mock_open.call_args[0][0]
    payload = json.loads(req.data.decode())
    assert "CRITICAL" in payload["text"]
    assert "odysseus" in payload["text"]


def test_error_severity_also_posts(monkeypatch):
    monkeypatch.setenv("SLACK_EVENTS_WEBHOOK_URL", "https://hooks.slack.com/fake")
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        notify_new_event(_event(severity="error"))
    mock_open.assert_called_once()


def test_url_error_does_not_raise(monkeypatch):
    monkeypatch.setenv("SLACK_EVENTS_WEBHOOK_URL", "https://hooks.slack.com/fake")
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        notify_new_event(_event())  # must not raise
