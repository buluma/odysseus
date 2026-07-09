"""Regression coverage for the calendar sidebar notif-dot (static/js/calendar.js
_upcomingEvents/_markBadgeSeen/_updateBadge): lit for any visible upcoming
(not-yet-ended) event whose uid isn't in the last-seen set, cleared once
_markBadgeSeen() snapshots the currently-upcoming uids.

calendar.js pulls in the app's whole UI module graph (modalManager, tileManager,
colorPicker, theme, ...) as a side effect of import, so this stubs just enough
of `document`/`window`/`localStorage`/`fetch` for that graph to load without
throwing — the calendar-specific behavior under test still runs unmocked.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CALENDAR_JS = _REPO / "static" / "js" / "calendar.js"
_HAS_NODE = shutil.which("node") is not None

_STUB_PREFIX = """
globalThis.HTMLInputElement = class {};
globalThis.HTMLElement = class {};
globalThis.Node = class {};
globalThis.window = {
  innerWidth: 1200, innerHeight: 800,
  addEventListener() {}, removeEventListener() {},
  location: { origin: 'http://localhost:7000' },
  dispatchEvent() {},
};
globalThis.localStorage = (() => {
  const store = {};
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
})();
const _dot = { style: {} };
globalThis.document = {
  readyState: 'complete',
  body: { appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} } },
  documentElement: {
    style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
  },
  addEventListener() {}, removeEventListener() {},
  getElementById(id) { return id === 'calendar-notif-dot' ? _dot : null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() {
    return {
      style: {}, classList: { add() {}, remove() {}, contains() { return false; } },
      addEventListener() {}, remove() {}, appendChild() {}, setAttribute() {},
    };
  },
};
globalThis.fetch = async () => { throw new Error('no network in test'); };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.MutationObserver = class { observe() {} disconnect() {} };
"""


def _run(js_body: str):
    script = _STUB_PREFIX + textwrap.dedent(f"""
        const mod = await import('{_CALENDAR_JS.as_posix()}');
        {js_body}
        // calendar.js pulls in tileManager.js, which self-reschedules a
        // requestAnimationFrame sidebar-watch loop; our rAF stub uses
        // setTimeout so it doesn't recurse synchronously, but that keeps a
        // live timer forever and Node's event loop never drains on its own.
        process.exit(0);
    """)
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _iso(offset_seconds: float) -> str:
    # Compute in JS at run time rather than embedding a Python-side timestamp,
    # so "now" is consistent with the module's own Date.now() calls.
    return f"new Date(Date.now() + {offset_seconds * 1000}).toISOString()"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_dot_hidden_with_no_events():
    result = _run("""
        mod._setEventsForTests([]);
        mod._updateBadgeForTests();
        console.log(JSON.stringify({ display: document.getElementById('calendar-notif-dot').style.display }));
    """)
    assert result["display"] == "none"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_dot_shown_for_unseen_upcoming_event():
    result = _run(f"""
        mod._setEventsForTests([
          {{ uid: 'evt-1', dtstart: {_iso(3600)}, dtend: {_iso(7200)}, all_day: false }},
        ]);
        mod._updateBadgeForTests();
        console.log(JSON.stringify({{ display: document.getElementById('calendar-notif-dot').style.display }}));
    """)
    assert result["display"] == ""


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_dot_hidden_for_past_event():
    result = _run(f"""
        mod._setEventsForTests([
          {{ uid: 'evt-past', dtstart: {_iso(-7200)}, dtend: {_iso(-3600)}, all_day: false }},
        ]);
        mod._updateBadgeForTests();
        console.log(JSON.stringify({{ display: document.getElementById('calendar-notif-dot').style.display }}));
    """)
    assert result["display"] == "none"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_dot_clears_after_mark_seen_then_reappears_for_new_event():
    result = _run(f"""
        mod._setEventsForTests([
          {{ uid: 'evt-1', dtstart: {_iso(3600)}, dtend: {_iso(7200)}, all_day: false }},
        ]);
        mod._updateBadgeForTests();
        const beforeSeen = document.getElementById('calendar-notif-dot').style.display;

        mod._markBadgeSeenForTests();
        mod._updateBadgeForTests();
        const afterSeen = document.getElementById('calendar-notif-dot').style.display;

        // A second, previously-unseen event gets added later the same day —
        // the dot must re-trigger instead of staying cleared until a new day.
        mod._setEventsForTests([
          {{ uid: 'evt-1', dtstart: {_iso(3600)}, dtend: {_iso(7200)}, all_day: false }},
          {{ uid: 'evt-2', dtstart: {_iso(10800)}, dtend: {_iso(14400)}, all_day: false }},
        ]);
        mod._updateBadgeForTests();
        const afterNewEvent = document.getElementById('calendar-notif-dot').style.display;

        console.log(JSON.stringify({{ beforeSeen, afterSeen, afterNewEvent }}));
    """)
    assert result["beforeSeen"] == ""
    assert result["afterSeen"] == "none"
    assert result["afterNewEvent"] == ""
