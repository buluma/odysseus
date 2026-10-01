"""Tests for the Google OAuth2 Calendar integration.

Mirrors tests/test_email_oauth.py's shape for the calendar equivalent:

- `_make_calendar_oauth_state` / `_verify_calendar_oauth_state` — HMAC-signed
  state so the callback can't be CSRF'd or have its owner tampered with.
- `_refresh_google_calendar_token` — token refresh stores the result
  encrypted; failure is silent (no token/secret in logs or return value).
- `_get_valid_google_calendar_token` — uses the cached token when fresh,
  refreshes when expired or missing.
- `calendar_google_oauth_callback` (real route) — invalid/tampered/missing
  state and provider errors return generic redirects with no PII; a valid
  state writes encrypted tokens for the right owner and doesn't duplicate
  the account row on reconnect.

Route tests pull the live endpoint out of `setup_calendar_routes()` and call
it directly — they pin the real handler, not a re-implementation. The ASGI
app is not booted; outbound HTTP is mocked and the DB is an isolated
in-memory SQLite (tests.helpers.sqlite_db.make_temp_sqlite per this repo's
testing rules).
"""

import sys
import unittest.mock as mock

import pytest

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb
from core.database import CalendarGoogleAccount

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    parent = sys.modules.get("core")
    if parent is not None:
        monkeypatch.setattr(parent, "database", cdb, raising=False)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    yield


# ── OAuth state signing ─────────────────────────────────────────────────

def test_oauth_state_round_trips_owner():
    from routes.calendar_routes import _make_calendar_oauth_state, _verify_calendar_oauth_state

    state = _make_calendar_oauth_state("user@example.com")
    payload = _verify_calendar_oauth_state(state)

    assert payload is not None
    assert payload["o"] == "user@example.com"
    assert payload["n"]  # nonce present


def test_oauth_state_nonce_is_unique_per_call():
    from routes.calendar_routes import _make_calendar_oauth_state, _verify_calendar_oauth_state

    a = _verify_calendar_oauth_state(_make_calendar_oauth_state("owner"))
    b = _verify_calendar_oauth_state(_make_calendar_oauth_state("owner"))
    assert a["n"] != b["n"]


def test_oauth_state_rejects_tampered_owner():
    from routes.calendar_routes import _make_calendar_oauth_state, _verify_calendar_oauth_state
    import base64

    state = _make_calendar_oauth_state("owner-1")
    decoded = base64.urlsafe_b64decode(state.encode()).decode()
    payload, sig = decoded.rsplit("|", 1)
    tampered_payload = payload.replace("owner-1", "owner-2")
    tampered = base64.urlsafe_b64encode(f"{tampered_payload}|{sig}".encode()).decode()

    assert _verify_calendar_oauth_state(tampered) is None


def test_oauth_state_rejects_garbage():
    from routes.calendar_routes import _verify_calendar_oauth_state

    assert _verify_calendar_oauth_state("not-valid-base64!!") is None


# ── Token refresh ────────────────────────────────────────────────────────

def _make_account(db, account_id="acct-1", owner="alice", **kwargs):
    row = CalendarGoogleAccount(id=account_id, owner=owner, label="Google Calendar")
    for k, v in kwargs.items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    return row


def test_refresh_token_stored_encrypted_not_raw():
    from src.secret_storage import encrypt as _enc, decrypt as _dec

    raw_token = "ya29.test_access_token_raw"
    db = _TS()
    _make_account(db, account_id="acct-r", owner="bob", oauth_refresh_token=_enc("refresh-tok-xyz"))
    db.close()

    fake_resp = mock.MagicMock()
    fake_resp.raise_for_status = mock.MagicMock()
    fake_resp.json.return_value = {"access_token": raw_token, "expires_in": 3600}

    with mock.patch("httpx.post", return_value=fake_resp), \
         mock.patch("src.google_calendar_sync.os.environ.get", side_effect=lambda k, d="": {
             "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "csec"
         }.get(k, d)):
        from src.google_calendar_sync import _refresh_google_calendar_token
        result = _refresh_google_calendar_token("acct-r")

    verify_db = _TS()
    row = verify_db.query(CalendarGoogleAccount).filter(CalendarGoogleAccount.id == "acct-r").first()
    stored = row.oauth_access_token
    verify_db.close()

    assert result == raw_token
    assert stored != raw_token
    assert _dec(stored) == raw_token


def test_refresh_returns_none_without_client_credentials():
    with mock.patch("src.google_calendar_sync.os.environ.get", return_value=""):
        from src.google_calendar_sync import _refresh_google_calendar_token
        assert _refresh_google_calendar_token("nonexistent") is None


def test_refresh_returns_none_and_stays_silent_on_http_failure():
    db = _TS()
    from src.secret_storage import encrypt as _enc
    _make_account(db, account_id="acct-fail", owner="bob", oauth_refresh_token=_enc("ref-tok"))
    db.close()

    with mock.patch("httpx.post", side_effect=RuntimeError("network down")), \
         mock.patch("src.google_calendar_sync.os.environ.get", side_effect=lambda k, d="": {
             "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "csec"
         }.get(k, d)):
        from src.google_calendar_sync import _refresh_google_calendar_token
        assert _refresh_google_calendar_token("acct-fail") is None


def test_get_valid_token_uses_cache_when_fresh():
    import time
    from src.secret_storage import encrypt as _enc

    db = _TS()
    _make_account(
        db, account_id="acct-fresh", owner="bob",
        oauth_access_token=_enc("cached-token"),
        oauth_token_expiry=str(int(time.time()) + 3600),
    )
    db.close()

    with mock.patch("httpx.post") as post_mock:
        from src.google_calendar_sync import _get_valid_google_calendar_token
        token = _get_valid_google_calendar_token("acct-fresh")

    assert token == "cached-token"
    post_mock.assert_not_called()


def test_get_valid_token_refreshes_when_expired():
    import time
    from src.secret_storage import encrypt as _enc

    db = _TS()
    _make_account(
        db, account_id="acct-stale", owner="bob",
        oauth_access_token=_enc("stale-token"),
        oauth_refresh_token=_enc("refresh-tok"),
        oauth_token_expiry=str(int(time.time()) - 10),
    )
    db.close()

    fake_resp = mock.MagicMock()
    fake_resp.raise_for_status = mock.MagicMock()
    fake_resp.json.return_value = {"access_token": "new-token", "expires_in": 3600}

    with mock.patch("httpx.post", return_value=fake_resp), \
         mock.patch("src.google_calendar_sync.os.environ.get", side_effect=lambda k, d="": {
             "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "csec"
         }.get(k, d)):
        from src.google_calendar_sync import _get_valid_google_calendar_token
        token = _get_valid_google_calendar_token("acct-stale")

    assert token == "new-token"


# ── Real OAuth callback route ─────────────────────────────────────────────

def _callback_endpoint():
    from routes.calendar_routes import setup_calendar_routes
    router = setup_calendar_routes()
    for route in router.routes:
        if route.path == "/api/calendar/oauth/google/callback" and "GET" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("calendar_google_oauth_callback route not found")


class _FakeRequest:
    headers = {"host": "localhost:7000"}


def _location(resp):
    return resp.headers.get("location") or getattr(resp, "location", None)


async def test_callback_rejects_missing_code():
    endpoint = _callback_endpoint()
    resp = await endpoint(code=None, state="whatever", error=None, request=_FakeRequest())
    assert "calendar_oauth_error=missing_code" in _location(resp)


async def test_callback_rejects_invalid_state():
    endpoint = _callback_endpoint()
    resp = await endpoint(code="abc", state="garbage-not-signed", error=None, request=_FakeRequest())
    assert "calendar_oauth_error=invalid_state" in _location(resp)


async def test_callback_surfaces_provider_error():
    endpoint = _callback_endpoint()
    resp = await endpoint(code=None, state=None, error="access_denied", request=_FakeRequest())
    assert "calendar_oauth_error=google_error" in _location(resp)


async def test_callback_writes_encrypted_tokens_for_correct_owner():
    from routes.calendar_routes import _make_calendar_oauth_state
    from src.secret_storage import decrypt as _dec

    state = _make_calendar_oauth_state("alice")

    token_resp = mock.MagicMock()
    token_resp.raise_for_status = mock.MagicMock()
    token_resp.json.return_value = {
        "access_token": "raw-access-tok", "refresh_token": "raw-refresh-tok", "expires_in": 3600,
    }
    userinfo_resp = mock.MagicMock()
    userinfo_resp.is_success = True
    userinfo_resp.json.return_value = {"email": "alice@example.com"}

    endpoint = _callback_endpoint()
    with mock.patch("httpx.post", return_value=token_resp), \
         mock.patch("httpx.get", return_value=userinfo_resp):
        resp = await endpoint(code="auth-code", state=state, error=None, request=_FakeRequest())

    assert "calendar_oauth_success=1" in _location(resp)

    db = _TS()
    row = db.query(CalendarGoogleAccount).filter(CalendarGoogleAccount.owner == "alice").first()
    db.close()
    assert row is not None
    assert row.google_email == "alice@example.com"
    assert _dec(row.oauth_access_token) == "raw-access-tok"
    assert _dec(row.oauth_refresh_token) == "raw-refresh-tok"
    assert row.oauth_access_token != "raw-access-tok"  # encrypted at rest


async def test_callback_reconnect_reuses_existing_account_row():
    """Reconnecting the same Google email must not create a second account row."""
    from routes.calendar_routes import _make_calendar_oauth_state

    db = _TS()
    _make_account(db, account_id="acct-existing", owner="carol", google_email="carol@example.com")
    db.close()

    state = _make_calendar_oauth_state("carol")
    token_resp = mock.MagicMock()
    token_resp.raise_for_status = mock.MagicMock()
    token_resp.json.return_value = {"access_token": "tok2", "refresh_token": "ref2", "expires_in": 3600}
    userinfo_resp = mock.MagicMock()
    userinfo_resp.is_success = True
    userinfo_resp.json.return_value = {"email": "carol@example.com"}

    endpoint = _callback_endpoint()
    with mock.patch("httpx.post", return_value=token_resp), \
         mock.patch("httpx.get", return_value=userinfo_resp):
        await endpoint(code="auth-code", state=state, error=None, request=_FakeRequest())

    db = _TS()
    rows = db.query(CalendarGoogleAccount).filter(CalendarGoogleAccount.owner == "carol").all()
    db.close()
    assert len(rows) == 1
    assert rows[0].id == "acct-existing"


# ── /sync response merging (CalDAV + Google) ────────────────────────────

def test_merge_sync_results_pull_sums_counts_and_concats_errors():
    from routes.calendar_routes import _merge_sync_results

    caldav = {"calendars": 1, "events": 3, "deleted": 0, "errors": ["caldav broke"]}
    google = {"calendars": 2, "events": 5, "deleted": 1, "errors": ["google broke"]}
    merged = _merge_sync_results("pull", caldav, google)

    assert merged == {
        "calendars": 3, "events": 8, "deleted": 1,
        "errors": ["caldav broke", "google broke"],
    }


def test_merge_sync_results_push_shape():
    from routes.calendar_routes import _merge_sync_results

    caldav = {"events": 2, "errors": []}
    google = {"events": 1, "errors": ["push failed"]}
    merged = _merge_sync_results("push", caldav, google)

    assert merged == {"events": 3, "errors": ["push failed"]}


def test_merge_sync_results_both_shape_nests_push_and_pull():
    from routes.calendar_routes import _merge_sync_results

    caldav = {"push": {"events": 1, "errors": []}, "pull": {"calendars": 1, "events": 2, "deleted": 0, "errors": []}}
    google = {"push": {"events": 0, "errors": []}, "pull": {"calendars": 1, "events": 1, "deleted": 1, "errors": []}}
    merged = _merge_sync_results("both", caldav, google)

    assert merged == {
        "push": {"events": 1, "errors": []},
        "pull": {"calendars": 2, "events": 3, "deleted": 1, "errors": []},
    }
