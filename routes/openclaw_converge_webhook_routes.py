"""Inbound Converge webhook handler.

Converge (Redmine Dashboard) POSTs ticket lifecycle events here when
a subscription is registered. Each delivery is HMAC-SHA256 signed with
the shared secret in CONVERGE_WEBHOOK_SECRET.

Endpoint: POST /api/openclaw/converge/webhook
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Annotated, Optional

from fastapi import APIRouter, Header, HTTPException, Request

from src.event_store import EventStore
from src.slack_notify import notify_new_event

logger = logging.getLogger(__name__)

# Events that create a homelab event entry and what severity they carry.
_EVENT_SEVERITY: dict[str, str] = {
    "ticket.created":        "info",
    "ticket.assigned":       "info",
    "ticket.status_changed": "info",
    "ticket.completed":      "info",
    "ticket.deleted":        "warning",
    # ticket.updated is intentionally absent — too noisy, no action created
}

# Events that also fire a Slack/ntfy notification via notify_new_event.
_NOTIFY_EVENTS = {"ticket.assigned"}


def _verify_signature(body: bytes, header: str | None, secret: str) -> None:
    if not header:
        raise HTTPException(401, "Missing X-Webhook-Signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(401, "Invalid webhook signature")


def _title_for(event: str, ticket: dict) -> str:
    subject = ticket.get("subject") or "unknown"
    issue_id = ticket.get("redmineIssueId")
    ref = f"#{issue_id}" if issue_id else ticket.get("id", "")
    labels = {
        "ticket.created":        f"Ticket created: {subject} ({ref})",
        "ticket.assigned":       f"Ticket assigned: {subject} ({ref})",
        "ticket.status_changed": f"Ticket status changed: {subject} ({ref})",
        "ticket.completed":      f"Ticket completed: {subject} ({ref})",
        "ticket.deleted":        f"Ticket deleted: {subject} ({ref})",
    }
    return labels.get(event, f"Ticket event {event}: {subject} ({ref})")


def _summary_for(event: str, ticket: dict, changes: list | None) -> str:
    parts: list[str] = []

    project = ticket.get("projectName")
    if project:
        parts.append(f"Project: {project}")

    assignee = ticket.get("assignedToName")
    status = ticket.get("statusName")
    priority = ticket.get("priorityName")

    if event == "ticket.assigned" and assignee:
        parts.append(f"Assigned to: {assignee}")
    if status:
        parts.append(f"Status: {status}")
    if priority:
        parts.append(f"Priority: {priority}")

    if changes:
        change_parts = []
        for ch in changes:
            field = ch.get("field", "?")
            old = ch.get("oldValue") or "—"
            new = ch.get("newValue") or "—"
            change_parts.append(f"{field}: {old} → {new}")
        if change_parts:
            parts.append("Changes: " + "; ".join(change_parts))

    return " | ".join(parts) if parts else ""


def setup_converge_webhook_routes() -> APIRouter:
    router = APIRouter()

    @router.post("/api/openclaw/converge/webhook")
    async def converge_webhook(
        request: Request,
        x_webhook_signature: Annotated[Optional[str], Header()] = None,
    ) -> dict:
        """Receive and process a Converge ticket lifecycle webhook."""
        body = await request.body()
        secret = (os.getenv("CONVERGE_WEBHOOK_SECRET") or "").strip()
        if not secret:
            logger.error("CONVERGE_WEBHOOK_SECRET not configured — webhook rejected")
            raise HTTPException(503, "Webhook receiver not configured")

        _verify_signature(body, x_webhook_signature, secret)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON payload")

        event: str = payload.get("event", "")
        ticket: dict = payload.get("ticket") or {}
        changes: list | None = payload.get("changes")
        delivery_id: str = payload.get("id", "")

        if event not in _EVENT_SEVERITY:
            # ticket.updated and unknown events: ack and ignore
            logger.debug("Converge webhook %r ignored (event=%s)", delivery_id, event)
            return {"ok": True, "action": "ignored", "event": event}

        severity = _EVENT_SEVERITY[event]
        title = _title_for(event, ticket)
        summary = _summary_for(event, ticket, changes)
        issue_id = ticket.get("redmineIssueId") or ticket.get("id", "")
        dedupe_key = f"converge:{event}:{issue_id}"

        store = EventStore()
        new_event = store.record_event(
            source="converge",
            service=ticket.get("projectName") or "converge",
            severity=severity,
            title=title,
            summary=summary,
            dedupe_key=dedupe_key,
            metadata={
                "event": event,
                "ticket_id": ticket.get("id"),
                "redmine_issue_id": issue_id,
                "status": ticket.get("statusName"),
                "assignee": ticket.get("assignedToName"),
                "delivery_id": delivery_id,
            },
            suggested_actions=["ack", "investigate", "resolve"],
        )

        if event in _NOTIFY_EVENTS:
            notify_new_event({**new_event, "severity": "info"})

        logger.info("Converge webhook processed: event=%s ticket=%s", event, issue_id)
        return {"ok": True, "action": "recorded", "event": event, "event_id": new_event.get("id")}

    return router
