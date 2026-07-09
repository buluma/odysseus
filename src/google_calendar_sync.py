"""Google Calendar -> local SQLite sync (pull) and account/token management.

Mirrors src/caldav_sync.py's shape for a second CalendarCal.source ("google"),
talking to the Google Calendar API v3 over HTTP instead of the CalDAV
protocol. src/google_calendar_writeback.py holds the push (local -> remote)
half, mirroring src/caldav_writeback.py.

Design notes (see also src/caldav_sync.py's own notes, which this mirrors):
- Accounts are DB-backed (CalendarGoogleAccount), not the prefs-JSON blob
  CalDAV accounts use — OAuth tokens rotate on every refresh, and a real
  column beats rewriting a JSON blob on every request (same reasoning as
  EmailAccount's oauth columns).
- Each account maps to exactly one local CalendarCal (source="google") for
  the account's "primary" calendar — no multi-calendar picker in this pass.
- Local CalendarEvent.uid is always the app-generated uuid4 (never mutated
  to match Google's id). The Google-side event id/etag live in the existing
  generic remote_href/remote_etag columns, exactly like the CalDAV path
  stores a resource URL there — same fields, different content by source.
- Recurring events are pulled with singleEvents=false (one row + a raw
  RRULE string), matching how the CalDAV path stores one VEVENT with an
  RRULE rather than expanding instances — the frontend expands RRULEs
  itself (see routes/calendar_routes.py::_expand_rrule). Instance-override
  rows (event bodies carrying recurringEventId) are skipped on pull, same
  fidelity level CalDAV already has (no per-instance remote sync).
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 365
_API_BASE = "https://www.googleapis.com/calendar/v3"


def _stable_cal_id(account_id: str, google_calendar_id: str) -> str:
    """Deterministic local CalendarCal id for a connected Google account's calendar."""
    key = f"{account_id}\n{google_calendar_id}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"google-{h}"


# ── Account CRUD (DB-backed) ──────────────────────────────────────────────

def _account_to_dict(row) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "google_email": row.google_email,
        "google_calendar_id": row.google_calendar_id,
        "enabled": row.enabled,
    }


def list_google_accounts(owner: str) -> list:
    from core.database import CalendarGoogleAccount, SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(CalendarGoogleAccount).filter(CalendarGoogleAccount.owner == owner).all()
        return [_account_to_dict(r) for r in rows]
    finally:
        db.close()


def delete_google_account(owner: str, account_id: str) -> bool:
    """Remove the account and its local calendar (events cascade via the
    CalendarCal relationship). Returns False if not found for this owner."""
    from core.database import CalendarCal, CalendarGoogleAccount, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(CalendarGoogleAccount).filter(
            CalendarGoogleAccount.id == account_id,
            CalendarGoogleAccount.owner == owner,
        ).first()
        if not row:
            return False
        cal_id = _stable_cal_id(row.id, row.google_calendar_id)
        cal = db.query(CalendarCal).filter(CalendarCal.id == cal_id, CalendarCal.owner == owner).first()
        if cal:
            db.delete(cal)
        db.delete(row)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── OAuth token refresh (mirrors routes/email_helpers.py's Google helpers,
# duplicated rather than imported so calendar routes stay independent of the
# email route module for an unrelated concern) ─────────────────────────────

def _refresh_google_calendar_token(account_id: str) -> str | None:
    """Exchange the stored refresh token for a new access token and persist it."""
    from core.database import CalendarGoogleAccount, SessionLocal
    from src.secret_storage import decrypt as _dec, encrypt as _enc
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    db = SessionLocal()
    try:
        row = db.get(CalendarGoogleAccount, account_id)
        if not row or not row.oauth_refresh_token:
            return None
        refresh_token = _dec(row.oauth_refresh_token or "")
        if not refresh_token:
            return None
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        access_token = data["access_token"]
        row.oauth_access_token = _enc(access_token)
        row.oauth_token_expiry = str(int(time.time()) + data.get("expires_in", 3600))
        db.commit()
        return access_token
    except Exception:
        logger.warning(f"Google Calendar token refresh failed for account {account_id}")
        return None
    finally:
        db.close()


def _get_valid_google_calendar_token(account_id: str) -> str | None:
    """Return a valid Google access token for the account, refreshing if expired or missing."""
    from core.database import CalendarGoogleAccount, SessionLocal
    from src.secret_storage import decrypt as _dec
    db = SessionLocal()
    try:
        row = db.get(CalendarGoogleAccount, account_id)
        if not row:
            return None
        access_token = _dec(row.oauth_access_token or "")
        expiry_str = row.oauth_token_expiry or ""
        if access_token and expiry_str:
            try:
                if int(expiry_str) - 60 > time.time():
                    return access_token
            except (ValueError, TypeError):
                pass
    finally:
        db.close()
    return _refresh_google_calendar_token(account_id)


# ── Pull (remote -> local) ─────────────────────────────────────────────────

def _to_utc_naive(date_str: str | None, datetime_str: str | None) -> tuple[datetime, bool, bool]:
    """Google's start/end object carries either 'date' (all-day) or
    'dateTime' (+ an offset, always tz-aware). Returns (dt, all_day, is_utc)
    with the same convention src/caldav_sync.py::_to_utc_naive uses."""
    if date_str:
        y, m, d = (int(p) for p in date_str.split("-"))
        return datetime(y, m, d), True, False
    dt = datetime.fromisoformat(datetime_str)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None), False, True
    return dt, False, False


def _google_event_to_fields(event: dict) -> dict | None:
    """Map a Google Calendar API event resource to CalendarEvent column
    values, or None if this event should be skipped on pull (an
    instance-override of a recurring series, or missing a start time)."""
    if event.get("recurringEventId"):
        return None  # instance override — see module docstring
    start = event.get("start") or {}
    end = event.get("end") or {}
    if not (start.get("date") or start.get("dateTime")):
        return None

    start_dt, all_day, start_is_utc = _to_utc_naive(start.get("date"), start.get("dateTime"))
    if end.get("date") or end.get("dateTime"):
        end_dt, _, _ = _to_utc_naive(end.get("date"), end.get("dateTime"))
    elif all_day:
        end_dt = start_dt + timedelta(days=1)
    else:
        end_dt = start_dt + timedelta(hours=1)

    rrule = ""
    for line in event.get("recurrence") or []:
        if line.startswith("RRULE:"):
            rrule = line[len("RRULE:"):]
            break

    etag = str(event.get("etag") or "").strip('"')
    return {
        "summary": event.get("summary") or "",
        "description": event.get("description") or "",
        "location": event.get("location") or "",
        "dtstart": start_dt,
        "dtend": end_dt,
        "all_day": all_day,
        "is_utc": start_is_utc,
        "rrule": rrule,
        "status": "cancelled" if event.get("status") == "cancelled" else "confirmed",
        "remote_href": event.get("id") or "",
        "remote_etag": etag,
    }


def _sync_account_blocking(owner: str, account_id: str, access_token: str,
                            google_calendar_id: str) -> dict:
    """The actual pull for one account — synchronous, run in a threadpool."""
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from routes.calendar_routes import _ensure_positive_duration

    result = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}

    window_start = datetime.utcnow() - timedelta(days=_LOOKBACK_DAYS)
    window_end = datetime.utcnow() + timedelta(days=_LOOKAHEAD_DAYS)
    time_min = window_start.isoformat() + "Z"
    time_max = window_end.isoformat() + "Z"

    items = []
    page_token = None
    try:
        with httpx.Client(timeout=15) as client:
            while True:
                params = {
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "false",
                    "maxResults": 250,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = client.get(
                    f"{_API_BASE}/calendars/{google_calendar_id}/events",
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                resp.raise_for_status()
                data = resp.json()
                items.extend(data.get("items") or [])
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
    except Exception as e:
        result["errors"].append(f"Google Calendar fetch failed: {e}")
        return result

    cal_id = _stable_cal_id(account_id, google_calendar_id)
    db = SessionLocal()
    try:
        local_cal = db.query(CalendarCal).filter(
            CalendarCal.id == cal_id, CalendarCal.owner == owner,
        ).first()
        if not local_cal:
            local_cal = CalendarCal(
                id=cal_id, owner=owner, name="Google Calendar",
                color="#4285f4", source="google", account_id=account_id,
            )
            db.add(local_cal)
            db.commit()
        result["calendars"] += 1

        seen_uids = set()
        pending: dict = {}
        for event in items:
            fields = _google_event_to_fields(event)
            if fields is None:
                continue
            uid_val = event.get("id") or ""
            if not uid_val:
                continue
            seen_uids.add(uid_val)
            fields["dtend"] = _ensure_positive_duration(fields["dtstart"], fields["dtend"], fields["all_day"])

            existing = pending.get(uid_val) or db.query(CalendarEvent).filter(
                CalendarEvent.uid == uid_val, CalendarEvent.calendar_id == local_cal.id,
            ).first()
            if existing:
                if existing.caldav_sync_pending in {"create", "update"}:
                    result["events"] += 1
                    continue
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.calendar_id = local_cal.id
                existing.origin = "google"
                existing.caldav_sync_pending = None
            else:
                new_ev = CalendarEvent(uid=uid_val, calendar_id=local_cal.id, origin="google", **fields)
                db.add(new_ev)
                pending[uid_val] = new_ev
            result["events"] += 1
        db.commit()

        # Only prune within the fetched [window_start, window_end] range — an
        # event outside it is absent from `items` (and thus `seen_uids`)
        # simply because it wasn't fetched, not because it was deleted on
        # Google's side. Without this bound, any event further than 90 days
        # in the past or 365 days out gets deleted locally on every sync.
        stale = db.query(CalendarEvent).filter(
            CalendarEvent.calendar_id == local_cal.id,
            CalendarEvent.origin == "google",
            CalendarEvent.dtstart >= window_start,
            CalendarEvent.dtstart <= window_end,
            CalendarEvent.remote_href.isnot(None),
            CalendarEvent.caldav_sync_pending.is_(None),
            ~CalendarEvent.uid.in_(seen_uids) if seen_uids else CalendarEvent.uid.isnot(None),
        ).all()
        for ev in stale:
            db.delete(ev)
        result["deleted"] += len(stale)
        db.commit()
    except Exception as e:
        logger.exception("Google Calendar sync failed for account %s", account_id)
        result["errors"].append(str(e)[:200])
        db.rollback()
    finally:
        db.close()

    return result


async def sync_google_calendar(owner: str) -> dict:
    """Pull Google Calendar state into local DB for `owner` across all
    connected accounts. Returns aggregated counts + per-account errors."""
    import asyncio

    accounts = list_google_accounts(owner)
    if not accounts:
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": []}

    totals: dict = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    for acc in accounts:
        if not acc.get("enabled", True):
            continue
        label = acc.get("label") or acc.get("google_email") or acc["id"]
        token = _get_valid_google_calendar_token(acc["id"])
        if not token:
            totals["errors"].append(f"{label}: Google token unavailable — reconnect the account")
            continue
        result = await asyncio.to_thread(
            _sync_account_blocking, owner, acc["id"], token, acc["google_calendar_id"],
        )
        totals["calendars"] += result.get("calendars", 0)
        totals["events"] += result.get("events", 0)
        totals["deleted"] += result.get("deleted", 0)
        for err in result.get("errors", []):
            totals["errors"].append(f"{label}: {err}")
    return totals


# ── Push (local -> remote) plumbing — mirrors src/caldav_sync.py's push_* ──

def _event_payload(ev) -> dict:
    return {
        "uid": ev.uid,
        "summary": ev.summary,
        "description": ev.description,
        "location": ev.location,
        "dtstart": ev.dtstart,
        "dtend": ev.dtend,
        "all_day": ev.all_day,
        "is_utc": ev.is_utc,
        "rrule": ev.rrule or "",
        "remote_href": ev.remote_href or "",
    }


def _load_event_for_writeback(owner: str, uid: str) -> tuple[str, str, dict] | None:
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    db = SessionLocal()
    try:
        ev = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "google":
            return None
        return ev.calendar.source, ev.calendar.id, _event_payload(ev)
    finally:
        db.close()


def _load_delete_for_writeback(owner: str, uid: str) -> tuple[str, str, dict] | None:
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal
    db = SessionLocal()
    try:
        tombstone = db.query(CalendarDeletedEvent).filter(
            CalendarDeletedEvent.uid == uid,
            CalendarDeletedEvent.owner == owner,
            CalendarDeletedEvent.source == "google",
        ).first()
        if tombstone:
            return "google", tombstone.calendar_id, {"uid": uid, "remote_href": tombstone.remote_href or ""}

        ev = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "google":
            return None
        return ev.calendar.source, ev.calendar.id, {"uid": uid, "remote_href": ev.remote_href or ""}
    finally:
        db.close()


def _pending_writeback_uids(owner: str) -> tuple[list, list]:
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal
    db = SessionLocal()
    try:
        rows = (
            db.query(CalendarEvent.uid)
            .join(CalendarCal)
            .filter(
                CalendarCal.owner == owner,
                CalendarCal.source == "google",
                CalendarEvent.status != "cancelled",
                (
                    (CalendarEvent.caldav_sync_pending.isnot(None))
                    | (CalendarEvent.remote_href.is_(None))
                ),
            )
            .all()
        )
        delete_rows = (
            db.query(CalendarDeletedEvent.uid)
            .filter(CalendarDeletedEvent.owner == owner, CalendarDeletedEvent.source == "google")
            .all()
        )
        return [row[0] for row in rows], [row[0] for row in delete_rows]
    finally:
        db.close()


async def push_event_create(owner: str, uid: str) -> dict:
    loaded = _load_event_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    source, calendar_id, payload = loaded
    from src.google_calendar_writeback import writeback_event
    return await writeback_event(owner, source, calendar_id, payload)


async def push_event_update(owner: str, uid: str) -> dict:
    return await push_event_create(owner, uid)


async def push_event_delete(owner: str, uid: str) -> dict:
    loaded = _load_delete_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    source, calendar_id, payload = loaded
    from src.google_calendar_writeback import writeback_event
    return await writeback_event(owner, source, calendar_id, payload, delete=True)


async def sync_google_calendar_direction(owner: str, direction: str = "pull") -> dict:
    """Mirrors src/caldav_sync.py::sync_caldav_direction's pull/push/both shape."""
    direction = (direction or "pull").strip().lower()
    if direction == "pull":
        return await sync_google_calendar(owner)
    if direction == "push":
        return await push_pending_events(owner)
    if direction == "both":
        pushed = await push_pending_events(owner)
        pulled = await sync_google_calendar(owner)
        return {"push": pushed, "pull": pulled}
    return {
        "calendars": 0, "events": 0, "deleted": 0,
        "errors": [f"Unsupported Google Calendar sync direction: {direction}"],
    }


async def push_pending_events(owner: str) -> dict:
    result = {"events": 0, "errors": []}
    uids, delete_uids = _pending_writeback_uids(owner)
    for event_uid in uids:
        try:
            out = await push_event_update(owner, event_uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{event_uid}: {str(out.get('error') or out)[:160]}")
        except Exception as e:
            logger.warning("Google Calendar pending push failed for uid=%s: %s", event_uid, e)
            result["errors"].append(f"{event_uid}: {str(e)[:160]}")
    for event_uid in delete_uids:
        try:
            out = await push_event_delete(owner, event_uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{event_uid}: {str(out.get('error') or out)[:160]}")
        except Exception as e:
            logger.warning("Google Calendar pending delete failed for uid=%s: %s", event_uid, e)
            result["errors"].append(f"{event_uid}: {str(e)[:160]}")
    return result
