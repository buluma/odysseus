"""Google Calendar OAuth integration — pull mapping, write-back body
building, and account/tombstone scoping.

Mirrors the existing CalDAV test files' shape (tests/test_caldav_writeback.py,
tests/test_caldav_sync_uid_scope.py): pure-function tests need no network or
DB; account/tombstone tests bind an in-memory SQLite DB via
tests.helpers.sqlite_db.make_temp_sqlite per this repo's testing rules.
"""

import sys
import uuid
from datetime import datetime, timezone

import pytest

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb
from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, CalendarGoogleAccount

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    parent = sys.modules.get("core")
    if parent is not None:
        monkeypatch.setattr(parent, "database", cdb, raising=False)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    yield


# ── Pure mapping: pull side ─────────────────────────────────────────────

def test_google_event_to_fields_timed_utc():
    from src.google_calendar_sync import _google_event_to_fields

    event = {
        "id": "abc123",
        "summary": "Standup",
        "description": "daily",
        "location": "Zoom",
        "start": {"dateTime": "2026-06-10T14:00:00-07:00"},
        "end": {"dateTime": "2026-06-10T14:30:00-07:00"},
        "etag": '"e1"',
    }
    fields = _google_event_to_fields(event)
    assert fields["all_day"] is False
    assert fields["is_utc"] is True
    assert fields["dtstart"] == datetime(2026, 6, 10, 21, 0)
    assert fields["dtend"] == datetime(2026, 6, 10, 21, 30)
    assert fields["remote_href"] == "abc123"
    assert fields["remote_etag"] == "e1"


def test_google_event_to_fields_all_day():
    from src.google_calendar_sync import _google_event_to_fields

    event = {
        "id": "day-1",
        "summary": "Holiday",
        "start": {"date": "2026-07-04"},
        "end": {"date": "2026-07-05"},
    }
    fields = _google_event_to_fields(event)
    assert fields["all_day"] is True
    assert fields["is_utc"] is False
    assert fields["dtstart"] == datetime(2026, 7, 4)


def test_google_event_to_fields_extracts_rrule():
    from src.google_calendar_sync import _google_event_to_fields

    event = {
        "id": "series-1",
        "summary": "Weekly sync",
        "start": {"dateTime": "2026-06-10T14:00:00Z"},
        "end": {"dateTime": "2026-06-10T14:30:00Z"},
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=WE"],
    }
    fields = _google_event_to_fields(event)
    assert fields["rrule"] == "FREQ=WEEKLY;BYDAY=WE"


def test_google_event_to_fields_skips_instance_override():
    from src.google_calendar_sync import _google_event_to_fields

    event = {
        "id": "series-1_20260617T140000Z",
        "recurringEventId": "series-1",
        "start": {"dateTime": "2026-06-17T15:00:00Z"},
        "end": {"dateTime": "2026-06-17T15:30:00Z"},
    }
    assert _google_event_to_fields(event) is None


def test_google_event_to_fields_skips_missing_start():
    from src.google_calendar_sync import _google_event_to_fields

    assert _google_event_to_fields({"id": "x", "summary": "no start"}) is None


def test_stable_cal_id_deterministic_and_scoped():
    from src.google_calendar_sync import _stable_cal_id

    a = _stable_cal_id("acc-1", "primary")
    b = _stable_cal_id("acc-1", "primary")
    c = _stable_cal_id("acc-2", "primary")
    assert a == b
    assert a != c
    assert a.startswith("google-")


# ── Pure mapping: push side ─────────────────────────────────────────────

def _ev(**over):
    base = dict(
        uid="evt-1", summary="Dentist", description="bring x-rays",
        location="Clinic", dtstart=datetime(2026, 6, 10, 14, 0),
        dtend=datetime(2026, 6, 10, 15, 0), all_day=False, is_utc=True,
        rrule="", remote_href="",
    )
    base.update(over)
    return base


def test_build_google_event_body_timed():
    from src.google_calendar_writeback import build_google_event_body

    body = build_google_event_body(_ev())
    assert body["summary"] == "Dentist"
    assert body["start"] == {"dateTime": "2026-06-10T14:00:00Z", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2026-06-10T15:00:00Z", "timeZone": "UTC"}


def test_build_google_event_body_all_day():
    from src.google_calendar_writeback import build_google_event_body

    body = build_google_event_body(_ev(all_day=True, is_utc=False))
    assert body["start"] == {"date": "2026-06-10"}
    assert body["end"] == {"date": "2026-06-10"}


def test_build_google_event_body_includes_rrule():
    from src.google_calendar_writeback import build_google_event_body

    body = build_google_event_body(_ev(rrule="FREQ=WEEKLY;BYDAY=MO"))
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]


def test_writeback_event_skips_non_google_calendar():
    from src.google_calendar_writeback import writeback_event
    import asyncio

    result = asyncio.run(writeback_event("owner-1", "caldav", "cal-1", _ev()))
    assert result.get("skipped")


def test_writeback_event_skips_when_account_not_configured():
    from src.google_calendar_writeback import writeback_event
    import asyncio

    db = _TS()
    db.add(CalendarCal(id="cal-nogoogle", owner="owner-1", name="Google Calendar", source="google"))
    db.commit()
    db.close()

    result = asyncio.run(writeback_event("owner-1", "google", "cal-nogoogle", _ev()))
    assert result.get("skipped")


# ── Account CRUD ─────────────────────────────────────────────────────────

def test_list_google_accounts_scoped_to_owner():
    from src.google_calendar_sync import list_google_accounts

    db = _TS()
    db.add(CalendarGoogleAccount(id="acc-a", owner="owner-1", label="Work", google_email="a@x.com"))
    db.add(CalendarGoogleAccount(id="acc-b", owner="owner-2", label="Other", google_email="b@x.com"))
    db.commit()
    db.close()

    accounts = list_google_accounts("owner-1")
    assert [a["id"] for a in accounts] == ["acc-a"]


def test_delete_google_account_removes_calendar_and_returns_false_for_wrong_owner():
    from src.google_calendar_sync import _stable_cal_id, delete_google_account

    cal_id = _stable_cal_id("acc-del", "primary")
    db = _TS()
    db.add(CalendarGoogleAccount(id="acc-del", owner="owner-1", label="Work", google_calendar_id="primary"))
    db.add(CalendarCal(id=cal_id, owner="owner-1", name="Google Calendar", source="google", account_id="acc-del"))
    db.commit()
    db.close()

    assert delete_google_account("owner-2", "acc-del") is False

    assert delete_google_account("owner-1", "acc-del") is True
    db = _TS()
    assert db.get(CalendarGoogleAccount, "acc-del") is None
    assert db.get(CalendarCal, cal_id) is None
    db.close()


# ── Delete-tombstone source scoping (#google-calendar-oauth) ───────────────

def test_caldav_and_google_delete_loaders_only_see_their_own_source_tombstones():
    from src.caldav_sync import _load_delete_for_writeback as caldav_load
    from src.google_calendar_sync import _load_delete_for_writeback as google_load

    db = _TS()
    db.add(CalendarDeletedEvent(uid="uid-caldav", owner="owner-1", calendar_id="cal-c", source="caldav"))
    db.add(CalendarDeletedEvent(uid="uid-google", owner="owner-1", calendar_id="cal-g", source="google", remote_href="gid-1"))
    db.commit()
    db.close()

    assert caldav_load("owner-1", "uid-caldav") == ("caldav", "cal-c", {"uid": "uid-caldav"})
    assert caldav_load("owner-1", "uid-google") is None

    assert google_load("owner-1", "uid-google") == ("google", "cal-g", {"uid": "uid-google", "remote_href": "gid-1"})
    assert google_load("owner-1", "uid-caldav") is None


def test_legacy_tombstone_with_no_source_is_treated_as_caldav():
    from src.caldav_sync import _load_delete_for_writeback as caldav_load
    from src.google_calendar_sync import _load_delete_for_writeback as google_load

    db = _TS()
    db.add(CalendarDeletedEvent(uid="uid-legacy", owner="owner-1", calendar_id="cal-c", source=None))
    db.commit()
    db.close()

    assert caldav_load("owner-1", "uid-legacy") == ("caldav", "cal-c", {"uid": "uid-legacy"})
    assert google_load("owner-1", "uid-legacy") is None
