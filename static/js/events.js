/**
 * Events panel — Grafana/homelab incident events from /api/events.
 * Displays open events with ack/investigate/resolve/ignore actions.
 */

import * as Modals from './modalManager.js';

const API_BASE = window.location.origin;
const MODAL_ID = 'events-modal';
const RAIL_ID  = 'rail-events';
const POLL_MS  = 30_000;

let _open = false;
let _events = [];
let _tab = 'open';    // 'open' | 'all' | 'resolved'
let _pollTimer = null;
let _escHandler = null;

// ---- API ----

async function _fetchEvents(status = null) {
  const url = new URL(`${API_BASE}/api/events`);
  if (status) url.searchParams.set('status', status);
  url.searchParams.set('limit', '100');
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return data.events || [];
}

async function _action(eventId, verb) {
  const res = await fetch(`${API_BASE}/api/events/${eventId}/${verb}`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()).event;
}

// ---- Rail badge ----

function _updateRailBadge(events) {
  const openCount = events.filter(e => ['new', 'acknowledged', 'investigating'].includes(e.status)).length;
  const railBtn = document.getElementById(RAIL_ID);
  if (railBtn) {
    railBtn.classList.toggle('rail-notify', openCount > 0);
    railBtn.title = openCount > 0 ? `Events (${openCount} open)` : 'Events';
  }
  const dot = document.getElementById('events-notif-dot');
  if (dot) dot.style.display = openCount > 0 ? '' : 'none';
}

// ---- Render ----

const _SEV_COLOR = {
  critical: 'var(--accent-error, var(--red))',
  warning:  '#f59e0b',
  error:    'var(--accent-error, var(--red))',
  info:     'var(--accent-primary, #60a5fa)',
};

const _STATUS_LABEL = {
  new:           'NEW',
  acknowledged:  'ACK',
  investigating: 'WORK',
  resolved:      'DONE',
  ignored:       'IGN',
};

function _reltime(iso) {
  if (!iso) return '';
  const delta = Date.now() - new Date(iso).getTime();
  const s = Math.floor(delta / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function _filterEvents(all, tab) {
  if (tab === 'open')     return all.filter(e => ['new', 'acknowledged', 'investigating'].includes(e.status));
  if (tab === 'resolved') return all.filter(e => ['resolved', 'ignored'].includes(e.status));
  return all;
}

function _renderList(modal) {
  const body = modal.querySelector('#events-body');
  if (!body) return;
  const list = _filterEvents(_events, _tab);

  if (!list.length) {
    body.innerHTML = `<div style="text-align:center;opacity:0.45;padding:40px 0;font-size:13px;">No events</div>`;
    return;
  }

  body.innerHTML = list.map(ev => {
    const sevColor = _SEV_COLOR[ev.severity] || 'var(--fg)';
    const statusLabel = _STATUS_LABEL[ev.status] || ev.status;
    const isOpen = ['new', 'acknowledged', 'investigating'].includes(ev.status);
    const actions = isOpen
      ? `<button class="ev-action-btn" data-ev="${ev.id}" data-verb="ack" title="Acknowledge">ACK</button>
         <button class="ev-action-btn" data-ev="${ev.id}" data-verb="investigate" title="Investigating">WORK</button>
         <button class="ev-action-btn ev-action-resolve" data-ev="${ev.id}" data-verb="resolve" title="Resolve">DONE</button>
         <button class="ev-action-btn ev-action-ignore" data-ev="${ev.id}" data-verb="ignore" title="Ignore">IGN</button>`
      : '';
    return `
      <div class="ev-card" data-evid="${ev.id}">
        <div class="ev-card-header">
          <span class="ev-sev-badge" style="color:${sevColor};border-color:${sevColor};">${(ev.severity || 'info').toUpperCase()}</span>
          <span class="ev-status-chip">${statusLabel}</span>
          <span class="ev-service">${_esc(ev.service || '')}</span>
          ${ev.count > 1 ? `<span class="ev-count" title="Occurrences">×${ev.count}</span>` : ''}
          <span class="ev-time" title="${_esc(ev.last_seen || '')}">${_reltime(ev.last_seen)}</span>
        </div>
        <div class="ev-title">${_esc(ev.title || '')}</div>
        ${ev.summary ? `<div class="ev-summary">${_esc(ev.summary)}</div>` : ''}
        ${actions ? `<div class="ev-actions">${actions}</div>` : ''}
      </div>`;
  }).join('');

  body.querySelectorAll('.ev-action-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = btn.dataset.ev;
      const verb = btn.dataset.verb;
      btn.disabled = true;
      btn.style.opacity = '0.4';
      try {
        const updated = await _action(id, verb);
        const idx = _events.findIndex(ev => ev.id === id);
        if (idx !== -1 && updated) _events[idx] = updated;
        _updateRailBadge(_events);
        _renderList(modal);
        _renderTabs(modal);
      } catch (err) {
        console.error('Events action failed:', err);
        btn.disabled = false;
        btn.style.opacity = '';
      }
    });
  });
}

function _renderTabs(modal) {
  const open   = _filterEvents(_events, 'open').length;
  const all    = _events.length;
  const closed = _filterEvents(_events, 'resolved').length;

  modal.querySelector('#ev-tab-open span')?.replaceWith(Object.assign(document.createElement('span'), { textContent: open ? ` ${open}` : '' }));
  const tabs = {
    open:     modal.querySelector('#ev-tab-open'),
    all:      modal.querySelector('#ev-tab-all'),
    resolved: modal.querySelector('#ev-tab-resolved'),
  };
  Object.entries(tabs).forEach(([t, el]) => {
    if (!el) return;
    el.classList.toggle('active', t === _tab);
  });

  const countOpen = modal.querySelector('#ev-count-open');
  const countAll  = modal.querySelector('#ev-count-all');
  const countRes  = modal.querySelector('#ev-count-resolved');
  if (countOpen) countOpen.textContent = open > 0 ? open : '';
  if (countAll)  countAll.textContent  = all > 0  ? all  : '';
  if (countRes)  countRes.textContent  = closed > 0 ? closed : '';
}

function _setTabActive(modal, tab) {
  _tab = tab;
  modal.querySelectorAll('.ev-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  _renderList(modal);
}

function _esc(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ---- Polling ----

function _startPoll(modal) {
  _stopPoll();
  _pollTimer = setInterval(async () => {
    try {
      _events = await _fetchEvents();
      _updateRailBadge(_events);
      if (_open) {
        _renderList(modal);
        _renderTabs(modal);
      }
    } catch (_) {}
  }, POLL_MS);
}

function _stopPoll() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ---- Open / Close ----

export async function openEvents() {
  if (_open) return;
  _open = true;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = MODAL_ID;
  modal.innerHTML = `
    <div class="modal-content tasks-modal-content" style="max-width:680px;">
      <div class="modal-header">
        <h4 style="position:relative;top:-2px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px;">
            <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
            <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
          </svg>Events
        </h4>
        <span id="ev-last-refresh" style="font-size:11px;opacity:0.4;margin-left:8px;"></span>
        <span style="flex:1"></span>
        <button class="close-btn" id="events-close">✖</button>
      </div>

      <div class="memory-tabs" role="tablist" style="border-bottom:1px solid var(--border);padding:0 12px;">
        <button class="memory-tab ev-tab active" id="ev-tab-open" data-tab="open" role="tab">
          Open <span class="memory-count" id="ev-count-open" style="font-size:0.8em;opacity:0.6;margin-left:4px;"></span>
        </button>
        <button class="memory-tab ev-tab" id="ev-tab-all" data-tab="all" role="tab">
          All <span class="memory-count" id="ev-count-all" style="font-size:0.8em;opacity:0.6;margin-left:4px;"></span>
        </button>
        <button class="memory-tab ev-tab" id="ev-tab-resolved" data-tab="resolved" role="tab">
          Resolved <span class="memory-count" id="ev-count-resolved" style="font-size:0.8em;opacity:0.6;margin-left:4px;"></span>
        </button>
        <span style="flex:1"></span>
        <button id="ev-refresh-btn" class="section-header-btn" title="Refresh" style="font-size:11px;padding:0 6px;height:22px;margin:auto 0;">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="vertical-align:-1px;"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-.08-4"/></svg>
        </button>
      </div>

      <div id="events-body" class="modal-body" style="padding:10px 12px;overflow-y:auto;max-height:55vh;display:flex;flex-direction:column;gap:8px;">
        <div style="text-align:center;opacity:0.45;padding:40px 0;font-size:13px;">Loading…</div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  modal.querySelectorAll('.ev-tab').forEach(btn => {
    btn.addEventListener('click', () => _setTabActive(modal, btn.dataset.tab));
  });

  modal.querySelector('#events-close').addEventListener('click', closeEvents);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeEvents(); });

  const refreshBtn = modal.querySelector('#ev-refresh-btn');
  refreshBtn?.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    try {
      _events = await _fetchEvents();
      _updateRailBadge(_events);
      _renderList(modal);
      _renderTabs(modal);
      const ts = modal.querySelector('#ev-last-refresh');
      if (ts) ts.textContent = 'refreshed just now';
    } catch (err) {
      console.error('Events refresh failed:', err);
    } finally {
      refreshBtn.disabled = false;
    }
  });

  _escHandler = (e) => { if (e.key === 'Escape') closeEvents(); };
  document.addEventListener('keydown', _escHandler);

  Modals.register(MODAL_ID, {
    railBtnId: RAIL_ID,
    closeFn: () => closeEvents(),
    restoreFn: () => {},
  });

  try {
    _events = await _fetchEvents();
    _updateRailBadge(_events);
    _renderList(modal);
    _renderTabs(modal);
    const ts = modal.querySelector('#ev-last-refresh');
    if (ts) ts.textContent = 'live · 30s';
  } catch (err) {
    const body = modal.querySelector('#events-body');
    if (body) body.innerHTML = `<div style="text-align:center;color:var(--red);padding:24px;font-size:13px;">Failed to load events: ${_esc(String(err))}</div>`;
  }

  _startPoll(modal);
}

export function closeEvents() {
  if (!_open) return;
  _open = false;
  _stopPoll();
  if (_escHandler) {
    document.removeEventListener('keydown', _escHandler);
    _escHandler = null;
  }
  Modals.unregister(MODAL_ID);
  const modal = document.getElementById(MODAL_ID);
  if (modal) modal.remove();
}

export function isEventsOpen() { return _open; }

export async function pollRailBadge() {
  if (_pollTimer) return;
  try {
    const evs = await _fetchEvents();
    _updateRailBadge(evs);
  } catch (_) {}
}

// Seed the rail badge on module load without opening the panel
pollRailBadge();

export default { openEvents, closeEvents, isEventsOpen, pollRailBadge };
