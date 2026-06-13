"""Slack notification helpers for operational alerts."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any

logger = logging.getLogger(__name__)

_CRITICAL_SEVERITIES = {"critical", "error"}


def _events_webhook() -> str | None:
    return (os.getenv("SLACK_EVENTS_WEBHOOK_URL") or "").strip() or None


def _post(url: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status not in (200, 204):
                logger.warning("Slack webhook returned HTTP %s", resp.status)
    except urllib.error.URLError as exc:
        logger.warning("Slack webhook delivery failed: %s", exc)


def notify_new_event(event: dict[str, Any]) -> None:
    """Post a Slack alert for a newly created (not deduplicated) event.

    Only fires for critical/error severity. Silently skips if
    SLACK_EVENTS_WEBHOOK_URL is not configured.
    """
    webhook = _events_webhook()
    if not webhook:
        return
    if event.get("severity") not in _CRITICAL_SEVERITIES:
        return

    sev = (event.get("severity") or "").upper()
    service = event.get("service") or "unknown"
    title = event.get("title") or ""
    summary = event.get("summary") or ""
    event_id = event.get("id") or ""

    text = f"*[{sev}] {service}* — {title}"
    if summary:
        text += f"\n{summary}"
    if event_id:
        text += f"\n_ID: `{event_id}`_ · ack / investigate / resolve via `/events`"

    _post(webhook, {"text": text})
