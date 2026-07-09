"""Google Calendar write-back: push local create/update/delete to the remote
Google Calendar API v3, mirroring src/caldav_writeback.py's shape for the
"google" CalendarCal.source.

The pure piece (``build_google_event_body``) takes its input by argument so
it unit-tests with no network. ``writeback_event`` is the orchestration used
by src/google_calendar_sync.py's push_event_create/update/delete.
"""

import logging
from datetime import timezone

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/calendar/v3"


def build_google_event_body(ev: dict) -> dict:
    """Serialize a local event dict to a Google Calendar API event body.

    ``ev`` keys: uid, summary, description, location, dtstart (datetime),
    dtend (datetime), all_day (bool), is_utc (bool), rrule (str).
    Mirrors how the pull path (_google_event_to_fields) interprets
    is_utc/all_day so a round-trip is stable. Naive "floating" local times
    (is_utc=False, all_day=False) are sent as UTC — Google requires either a
    date or an offset-bearing dateTime, and this app has no per-event
    timezone to offer instead.
    """
    body = {
        "summary": ev.get("summary") or "",
        "description": ev.get("description") or "",
        "location": ev.get("location") or "",
    }
    dtstart = ev["dtstart"]
    dtend = ev["dtend"]
    if ev.get("all_day"):
        body["start"] = {"date": dtstart.date().isoformat()}
        body["end"] = {"date": dtend.date().isoformat()}
    else:
        body["start"] = {
            "dateTime": dtstart.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeZone": "UTC",
        }
        body["end"] = {
            "dateTime": dtend.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeZone": "UTC",
        }
    if ev.get("rrule"):
        body["recurrence"] = [f"RRULE:{ev['rrule']}"]
    return body


def _load_account_for_calendar(owner: str, calendar_id: str):
    """Return (google_account_id, google_calendar_id) for a local CalendarCal
    id, or None if the calendar/account can't be resolved."""
    from core.database import CalendarCal, CalendarGoogleAccount, SessionLocal
    db = SessionLocal()
    try:
        cal = db.query(CalendarCal).filter(
            CalendarCal.id == calendar_id, CalendarCal.owner == owner,
        ).first()
        if not cal or not cal.account_id:
            return None
        acc = db.query(CalendarGoogleAccount).filter(
            CalendarGoogleAccount.id == cal.account_id, CalendarGoogleAccount.owner == owner,
        ).first()
        if not acc or not acc.enabled:
            return None
        return acc.id, acc.google_calendar_id
    finally:
        db.close()


def _persist_writeback_result(owner: str, uid: str, result: dict, *, delete: bool) -> None:
    from core.database import CalendarDeletedEvent, CalendarEvent, SessionLocal

    if not uid or not isinstance(result, dict):
        return

    db = SessionLocal()
    try:
        if delete:
            tombstone = db.query(CalendarDeletedEvent).filter(
                CalendarDeletedEvent.uid == uid,
                CalendarDeletedEvent.owner == owner,
                CalendarDeletedEvent.source == "google",
            ).first()
            if result.get("ok"):
                if tombstone:
                    db.delete(tombstone)
            elif tombstone:
                tombstone.last_error = str(result.get("error") or result)[:500]
            db.commit()
            return

        event = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        if event and result.get("ok"):
            if result.get("remote_href"):
                event.remote_href = result.get("remote_href")
            if result.get("remote_etag"):
                event.remote_etag = result.get("remote_etag")
            event.caldav_sync_pending = None
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Google Calendar write-back metadata persistence failed")
    finally:
        db.close()


async def writeback_event(owner: str, calendar_source: str, calendar_id: str,
                          ev: dict, *, delete: bool = False) -> dict:
    """Best-effort push of a local change to Google Calendar.

    No-ops (``{"skipped": ...}``) when the calendar isn't Google-backed or no
    account is configured. Never raises — a remote failure is logged and
    returned, the local DB remaining the source of truth.
    """
    if calendar_source != "google":
        return {"skipped": "not a google calendar"}

    uid = (ev or {}).get("uid") if isinstance(ev, dict) else None
    if not uid:
        return {"ok": False, "error": "event uid is required"}

    acc = _load_account_for_calendar(owner, calendar_id)
    if acc is None:
        return {"skipped": "google account not configured"}
    account_id, google_calendar_id = acc

    from src.google_calendar_sync import _get_valid_google_calendar_token
    token = _get_valid_google_calendar_token(account_id)
    if not token:
        result = {"ok": False, "error": "Google token unavailable — reconnect the account"}
        _persist_writeback_result(owner, uid, result, delete=delete)
        return result

    headers = {"Authorization": f"Bearer {token}"}
    remote_id = (ev.get("remote_href") or "").strip()
    events_url = f"{_API_BASE}/calendars/{google_calendar_id}/events"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if delete:
                if not remote_id:
                    result = {"ok": True, "note": "already absent on remote"}
                else:
                    resp = await client.delete(f"{events_url}/{remote_id}", headers=headers)
                    if resp.status_code in (204, 404, 410):
                        result = {"ok": True}
                    else:
                        resp.raise_for_status()
                        result = {"ok": True}
                _persist_writeback_result(owner, uid, result, delete=True)
                return result

            body = build_google_event_body(ev)
            if remote_id:
                resp = await client.put(f"{events_url}/{remote_id}", headers=headers, json=body)
            else:
                resp = await client.post(events_url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            result = {
                "ok": True,
                "remote_href": data.get("id") or remote_id,
                "remote_etag": str(data.get("etag") or "").strip('"'),
            }
            _persist_writeback_result(owner, uid, result, delete=False)
            return result
    except Exception as e:
        logger.warning("Google Calendar write-back failed for uid=%s: %s", uid, e)
        result = {"ok": False, "error": str(e)[:200]}
        _persist_writeback_result(owner, uid, result, delete=delete)
        return result
