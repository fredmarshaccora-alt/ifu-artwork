
const $ = (id) => document.getElementById(id);

// WYSIWYG line weights (non-scaling-stroke).  Default ON: the on-screen
// drawing renders strokes at the SAME constant thickness the exported
// SVG uses, so the editor preview matches the IFU output and zooming
// doesn't fatten lines.  One flag drives both the on-screen body class
// (CSS rule in viewer.css) and the export (window._nonScalingStroke).
(function _initWysiwygWeights() {
  let off = false;
  try { off = localStorage.getItem('ifu:nonScalingStroke') === '0'; }
  catch (_e) {}
  window._nonScalingStroke = !off;
  if (document.body && !off) document.body.classList.add('wysiwyg-weights');
})();
window._setNonScalingStroke = (on) => {
  window._nonScalingStroke = !!on;
  if (document.body)
    document.body.classList.toggle('wysiwyg-weights', !!on);
  try { localStorage.setItem('ifu:nonScalingStroke', on ? '1' : '0'); }
  catch (_e) {}
};

// API_BASE was previously declared only in the bottom module script.
// Script 1 (this block) runs first and the routes-on-load path inside
// renderRoute() fetches IMMEDIATELY on initial paint -- before the
// module script gets a chance to run.  Without this hoisted
// declaration, `fetch(API_BASE + path)` throws ReferenceError and the
// editor silently fails to mount because the EditorScreen's try/catch
// swallows it.  Found via tests/smoke_figure_render.py via the new
// /api/debug/client_log overlay.
// API origin.  When the front-end is served from the SAME host as the
// API (local dev, or a single Render service) this stays '' (same
// origin).  For a split deploy -- static UI on Vercel, compute API on
// Render -- a tiny config.js sets window.IFU_API_BASE to the Render URL
// BEFORE this script runs, and every fetch(API_BASE + path) targets it.
const API_BASE = (typeof window !== 'undefined' && window.IFU_API_BASE)
  ? window.IFU_API_BASE
  : ((location.protocol === 'http:' || location.protocol === 'https:')
      ? ''
      : 'http://localhost:5000');
window.API_BASE = API_BASE;

// =====================================================================
// F.2 -- App shell: hash router + AppState + screen mount lifecycle
// =====================================================================
//
// The new product shape has four screens (Home / Project / Editor /
// Settings) navigated by URL hash.  F.2 ships the infra only; F.3+
// progressively replaces the legacy editor below with screen modules.
//
// Until F.5 lands, an empty hash falls through to the legacy editor
// (the <header> + <main> above).  Any recognised hash hides them and
// mounts the named screen into #app-root.

// Tiny hyperscript helper: h('div.card#x', {onClick}, [...])
function h(spec, attrs, children) {
  let tag = 'div', id = '', classes = [];
  if (typeof spec === 'string') {
    const m = spec.match(/^([a-z0-9]+)?((?:[.#][a-zA-Z0-9_-]+)*)$/);
    if (m) {
      tag = m[1] || 'div';
      (m[2] || '').split(/(?=[.#])/).forEach(part => {
        if (part.startsWith('#')) id = part.slice(1);
        else if (part.startsWith('.')) classes.push(part.slice(1));
      });
    } else {
      tag = spec;
    }
  }
  const el = document.createElementNS(
    tag === 'svg' || tag === 'path' || tag === 'g'
      ? 'http://www.w3.org/2000/svg'
      : 'http://www.w3.org/1999/xhtml', tag);
  if (id) el.id = id;
  if (classes.length) el.setAttribute('class', classes.join(' '));
  // Handle args overloads: h(spec, children), h(spec, attrs, children)
  if (attrs && (Array.isArray(attrs) || typeof attrs === 'string'
                 || attrs instanceof Node)) {
    children = attrs;
    attrs = null;
  }
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'onClick' || k === 'onclick') {
        el.addEventListener('click', v);
      } else if (k === 'style' && typeof v === 'object') {
        for (const [sk, sv] of Object.entries(v)) el.style[sk] = sv;
      } else if (v === false || v == null) {
        /* skip */
      } else if (v === true) {
        el.setAttribute(k, '');
      } else {
        el.setAttribute(k, v);
      }
    }
  }
  const kids = children == null ? []
    : (Array.isArray(children) ? children : [children]);
  for (const c of kids) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === 'string' || typeof c === 'number'
      ? document.createTextNode(String(c))
      : c);
  }
  return el;
}

// Single source of truth replacing today's scattered globals.  Screens
// read from it and dispatch via setRoute() / selectProject() etc. so
// future undo / persist / sync layers have a stable hook surface.
const AppState = {
  route: '#/',
  routeParams: {},
  currentProjectId: null,
  currentFigureId: null,
  settings: null,        // populated lazily by Settings screen
};

// Map of route patterns to screen modules.  Each module exports
// mount(container, params) -> teardownFn(optional).
const _routes = [];
let _currentTeardown = null;

function registerRoute(pattern, mountFn) {
  _routes.push({ pattern, mountFn });
  window.IFU_APP_ROUTES_REG = _routes.length;
}

function _matchRoute(hash) {
  for (const { pattern, mountFn } of _routes) {
    const m = hash.match(pattern);
    if (m) return { mountFn, params: m.slice(1) };
  }
  return null;
}

async function renderRoute() {
  const hash = location.hash || '';
  AppState.route = hash;
  const appRoot = document.getElementById('app-root');
  const header = document.querySelector('header');
  const main = document.querySelector('main');
  // TEMP DIAGNOSTIC: mirror route resolution to the server log so the
  // smoke test can see exactly which screen is mounting.
  try {
    (window._reportClientError || function(){})({
      level: 'info', op: 'route.enter',
      msg: 'hash=' + hash + ' routes=' + (window.IFU_APP_ROUTES_REG || '?')
            + ' appRootExists=' + !!appRoot,
    });
  } catch (_e) {}

  // Empty hash -> Home.  Post-Phase-3 the legacy editor is project-
  // scoped and only meaningful inside a figure route; landing on it
  // without a route is the "old page" symptom users hit after a
  // refresh.  Redirect to '#/' so the Home screen is the default.
  // Use replaceState-equivalent (location.hash = '#/') so the back
  // button doesn't loop through the empty-hash entry.
  if (!hash || hash === '#') {
    if (_currentTeardown) {
      try { _currentTeardown(); } catch (_e) {}
      _currentTeardown = null;
    }
    location.hash = '#/';
    return;   // the hashchange listener will re-enter and render Home
  }

  const matched = _matchRoute(hash);
  try {
    (window._reportClientError || function(){})({
      level: 'info', op: 'route.match',
      msg: 'matched=' + (matched ? 'YES (' + (matched.mountFn?.name
            || '?') + ') params=' + JSON.stringify(matched.params)
            : 'NO'),
    });
  } catch (_e) {}

  // Tear down the previous screen before mounting the next one.
  // mountFn can be async, so its returned value might be a Promise
  // resolving to either undefined or a teardown function.  Teardowns
  // can be async too (EditorScreen flushes auto-saves before letting
  // the route change) -- await so AppState.currentFigureId stays
  // valid until the flush completes.
  if (_currentTeardown) {
    try {
      if (typeof _currentTeardown === 'function') {
        const tdResult = _currentTeardown();
        if (tdResult && typeof tdResult.then === 'function') {
          await tdResult;
        }
      }
    } catch (_e) {}
    _currentTeardown = null;
  }

  if (!matched) {
    if (appRoot) {
      appRoot.style.display = '';
      appRoot.innerHTML = '';
      appRoot.appendChild(h('div', [
        h('h1', `Unknown route: ${hash}`),
        h('p', [
          'Try ',
          h('a', { href: '#/' }, '#/ (Home)'),
          '.',
        ]),
      ]));
    }
    if (header) header.style.display = 'none';
    if (main) main.style.display = 'none';
    return;
  }

  // EditorScreen wants the LEGACY editor visible; other screens want
  // the app-root visible.  Let the screen decide.  We pre-set the
  // common case (app-root visible, legacy hidden); EditorScreen
  // overrides on mount.
  if (header) header.style.display = 'none';
  if (main) main.style.display = 'none';
  if (appRoot) {
    appRoot.style.display = '';
    appRoot.innerHTML = '';
    AppState.routeParams = matched.params;
    // mountFn may be async -- await so the teardown captured is the
    // real function, not a Promise.
    const result = matched.mountFn(appRoot, matched.params);
    _currentTeardown = (result && typeof result.then === 'function')
      ? (await result) || null
      : (result || null);
  }
}

window.addEventListener('hashchange', renderRoute);
window.IFU_APP = { AppState, h, registerRoute, renderRoute };

// Mirror unhandled browser errors to the server's structured log so
// the user (without F12 access) can grab a copy from the log overlay
// or /api/debug/log.  Sent via fetch keepalive so even fatal-script
// errors deliver.
function _reportClientError(detail) {
  try {
    fetch((window.API_BASE || '') + '/api/debug/client_log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      keepalive: true,
      body: JSON.stringify(detail),
    }).catch(() => {});
  } catch (_e) {}
}
window.addEventListener('error', (ev) => {
  _reportClientError({
    level: 'err', op: 'window.error',
    msg: String(ev.message || ''),
    source: String(ev.filename || '').slice(-80),
    line: ev.lineno || 0, col: ev.colno || 0,
    stack: (ev.error && ev.error.stack || '').slice(0, 600),
  });
});
window.addEventListener('unhandledrejection', (ev) => {
  const r = ev.reason || {};
  _reportClientError({
    level: 'err', op: 'unhandledrejection',
    msg: String(r.message || r),
    stack: (r.stack || '').slice(0, 600),
  });
});
window._reportClientError = _reportClientError;
// ===== end F.2 app shell =====


// =====================================================================
// G.0 -- Design system: tokens, primitives, modal + toast
// =====================================================================
//
// Onshape-style product shell.  Light gray bg, white surface cards,
// brand-teal primary actions, subtle shadows, consistent spacing.
// All the new-shell screens (Home / Project / Settings / wizards)
// use these tokens; the legacy editor is unchanged.

const _DESIGN_CSS = `
:root {
  --space-1: 4px;  --space-2: 8px;  --space-3: 12px;
  --space-4: 16px; --space-5: 24px; --space-6: 32px;
  --space-7: 48px;
  --t-meta: 11px;   /* labels, badges */
  --t-body: 13px;   /* inputs, table cells */
  --t-strong: 14px; /* body emphasis */
  --t-card-title: 15px;
  --t-section: 12px;  /* uppercase section heads */
  --t-page-title: 22px;
  --c-accora: #00836a;
  --c-accora-dark: #006953;
  --c-accora-pale: #e8f3f0;
  --c-bg: #f5f5f7;
  --c-surface: #ffffff;
  --c-line: #e5e5e7;
  --c-text: #1d1d1f;
  --c-text-muted: #6e6e73;
  --c-danger: #c44;
  --shadow-1: 0 1px 2px rgba(0,0,0,0.04);
  --shadow-2: 0 2px 8px rgba(0,0,0,0.08);
  --radius-1: 4px;
  --radius-2: 6px;
  --radius-3: 10px;
}
.app-shell {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  color: var(--c-text);
  background: var(--c-bg);
  min-height: 100vh;
  margin: 0; padding: 0;
}
.app-topbar {
  background: #ffffff;
  border-bottom: 1px solid var(--c-line);
  height: 48px; padding: 0 var(--space-5);
  display: flex; align-items: center; gap: var(--space-4);
  box-shadow: var(--shadow-1);
  position: sticky; top: 0; z-index: 10;
}
.app-topbar .logo {
  font-weight: 600; font-size: 15px; color: var(--c-accora);
  text-decoration: none; letter-spacing: -0.01em;
  display: flex; align-items: center; gap: 8px;
}
.app-topbar .logo::before {
  content: ""; width: 18px; height: 18px;
  background: var(--c-accora);
  border-radius: 50%;
  /* concentric arc motif from the Accora brand */
  box-shadow:
    inset 0 0 0 2px #fff,
    inset 0 0 0 4px var(--c-accora);
}
.app-topbar .crumbs {
  display: flex; align-items: center; gap: var(--space-2);
  font-size: var(--t-strong); color: var(--c-text-muted);
}
.app-topbar .crumbs a {
  color: var(--c-text-muted); text-decoration: none;
}
.app-topbar .crumbs a:hover { color: var(--c-accora); }
.app-topbar .crumbs .sep { color: var(--c-line); }
.app-topbar .crumbs .current { color: var(--c-text); font-weight: 500; }
.app-topbar .spacer { flex: 1; }
.app-topbar .nav-link {
  color: var(--c-text-muted); text-decoration: none;
  font-size: var(--t-strong); padding: 4px 8px;
  border-radius: var(--radius-1);
}
.app-topbar .nav-link:hover { background: var(--c-bg); color: var(--c-text); }

.app-main { max-width: 1200px; margin: 0 auto;
  padding: var(--space-6) var(--space-5); }

.section-title {
  font-size: var(--t-section); color: var(--c-text-muted);
  text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600;
  margin: var(--space-6) 0 var(--space-3) 0;
}
.section-title:first-child { margin-top: 0; }

/* Card primitives */
.card-grid {
  display: grid; gap: var(--space-4);
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
}
.card {
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--radius-2); padding: var(--space-4);
  cursor: pointer; transition: border-color 0.12s, box-shadow 0.12s, transform 0.12s;
  display: flex; flex-direction: column; gap: var(--space-2);
  min-height: 96px;
}
.card:hover {
  border-color: var(--c-accora);
  box-shadow: var(--shadow-2);
  transform: translateY(-1px);
}
.card .card-title {
  font-size: var(--t-card-title); font-weight: 600;
  color: var(--c-text); line-height: 1.25;
}
.card .card-meta {
  font-size: var(--t-meta); color: var(--c-text-muted);
  display: flex; gap: var(--space-2); flex-wrap: wrap;
}
.card .badge {
  display: inline-block; font-size: var(--t-meta);
  padding: 1px 6px; border-radius: 10px;
  background: var(--c-bg); color: var(--c-text-muted);
}
.card .badge.ok { background: var(--c-accora-pale); color: var(--c-accora); }
.card .badge.warn { background: #fff3e0; color: #c70; }
.card.placeholder {
  background: transparent; border-style: dashed;
  align-items: center; justify-content: center;
  color: var(--c-text-muted); cursor: pointer;
  font-size: var(--t-strong);
}
.card.figure-card {
  min-height: 160px;
}
.card.figure-card .card-title {
  margin-top: auto;
}
.card.project-card {
  min-height: 230px;
}
.card.placeholder.project-new {
  min-height: 230px;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 8px;
}
/* The "+ New view" placeholder is matched to the view cards, which
   are now shorter -- otherwise the new-view tile is twice the height
   of every other card and the grid looks lopsided. */
.card-grid .card.placeholder:not(.project-new) {
  min-height: 160px;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 6px;
}
.card.placeholder:hover {
  background: var(--c-surface); border-style: solid;
  color: var(--c-accora);
}
.card { position: relative; }
.card .card-menu-btn {
  position: absolute; top: 6px; right: 6px;
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--c-line); background: var(--c-surface);
  cursor: pointer;
  color: var(--c-text-muted); border-radius: var(--radius-1);
  /* Always visible (was opacity:0 / hover-only, which hid Delete +
     Rename entirely -- undiscoverable, and invisible on touch).  Subtle
     by default, full strength on hover/focus. */
  opacity: 0.6; transition: opacity 0.12s, background 0.12s, color 0.12s;
  font-size: 18px; line-height: 1;
}
.card:hover .card-menu-btn,
.card .card-menu-btn:focus-visible,
.card .card-menu-btn.open { opacity: 1; }
.card .card-menu-btn:hover {
  background: var(--c-bg); color: var(--c-text);
  border-color: var(--c-accora);
}
.card-menu {
  position: absolute; min-width: 160px;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--radius-2); box-shadow: var(--shadow-2);
  padding: 4px; z-index: 100;
  font-size: var(--t-body);
}
.card-menu .item {
  padding: 6px 10px; border-radius: var(--radius-1);
  cursor: pointer; color: var(--c-text);
  display: flex; align-items: center; gap: var(--space-2);
}
.card-menu .item:hover { background: var(--c-bg); }
.card-menu .item.danger { color: var(--c-danger); }
.card-menu .item.danger:hover { background: #fdf0f0; }
.card-menu .sep {
  height: 1px; background: var(--c-line); margin: 4px 2px;
}

/* Buttons */
.btn {
  display: inline-flex; align-items: center; gap: var(--space-2);
  border-radius: var(--radius-1); border: 1px solid var(--c-line);
  background: var(--c-surface); color: var(--c-text);
  font-size: var(--t-strong); font-family: inherit;
  padding: 6px 12px; cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.btn:hover { background: var(--c-bg); border-color: var(--c-text-muted); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.primary {
  background: var(--c-accora); color: #fff; border-color: var(--c-accora);
}
.btn.primary:hover {
  background: var(--c-accora-dark); border-color: var(--c-accora-dark);
}
.btn.danger {
  color: var(--c-danger); border-color: #e5b5b5;
}
.btn.danger:hover { background: #fdf0f0; }
.btn.ghost { border: none; background: transparent; }
.btn.ghost:hover { background: var(--c-bg); }

/* Inputs */
.input, .select {
  font-family: inherit; font-size: var(--t-body);
  padding: 6px 10px; border-radius: var(--radius-1);
  border: 1px solid var(--c-line); background: #fff;
  color: var(--c-text);
}
.input:focus, .select:focus {
  outline: none; border-color: var(--c-accora);
  box-shadow: 0 0 0 3px var(--c-accora-pale);
}
.field-row {
  display: grid; grid-template-columns: 200px 1fr;
  gap: var(--space-4); align-items: center;
  margin-bottom: var(--space-3);
}
.field-row label {
  font-size: var(--t-strong); color: var(--c-text-muted);
}

/* Empty state */
.empty {
  color: var(--c-text-muted); font-style: italic;
  padding: var(--space-4); text-align: center;
}

/* Modal */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(20, 22, 28, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
  animation: fade-in 0.15s ease-out;
}
@keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
.modal {
  background: #fff; border-radius: var(--radius-3);
  box-shadow: 0 12px 48px rgba(0,0,0,0.25);
  width: 520px; max-width: 90vw; max-height: 86vh;
  display: flex; flex-direction: column;
  animation: pop-in 0.18s ease-out;
}
@keyframes pop-in {
  from { opacity: 0; transform: scale(0.96); }
  to { opacity: 1; transform: scale(1); }
}
.modal-header {
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--c-line);
  display: flex; align-items: center;
}
.modal-header h2 {
  font-size: 17px; margin: 0; font-weight: 600;
  color: var(--c-text); flex: 1;
}
.modal-close {
  border: none; background: transparent; font-size: 20px;
  color: var(--c-text-muted); cursor: pointer; padding: 0;
  width: 28px; height: 28px; border-radius: 50%;
}
.modal-close:hover { background: var(--c-bg); color: var(--c-text); }
.modal-body {
  padding: var(--space-5);
  overflow-y: auto; flex: 1;
  font-size: var(--t-body); color: var(--c-text);
  line-height: 1.5;
}
.modal-footer {
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--c-line);
  display: flex; gap: var(--space-2); justify-content: flex-end;
}

/* Toast */
.toast-host {
  position: fixed; bottom: var(--space-5); right: var(--space-5);
  display: flex; flex-direction: column; gap: var(--space-2);
  z-index: 2000; pointer-events: none;
}
.toast {
  background: #232325; color: #fff; padding: 10px 14px;
  border-radius: var(--radius-2); font-size: var(--t-body);
  box-shadow: 0 6px 20px rgba(0,0,0,0.18);
  pointer-events: auto; max-width: 360px;
  animation: slide-in 0.18s ease-out;
}
.toast.success { background: var(--c-accora-dark); }
.toast.error { background: #b54040; }
@keyframes slide-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

/* Spinner */
.spinner {
  display: inline-block;
  width: 16px; height: 16px; vertical-align: middle;
  border: 2px solid var(--c-line); border-top-color: var(--c-accora);
  border-radius: 50%;
  animation: spin 0.9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
`;

function _ensureDesignStyles() {
  if (document.getElementById('ifu-design-tokens')) return;
  const s = document.createElement('style');
  s.id = 'ifu-design-tokens';
  s.textContent = _DESIGN_CSS;
  document.head.appendChild(s);
}

// Top bar: logo + breadcrumb + (optional) right-side nav items.
function _topBar({ crumbs, rightLinks }) {
  const bar = h('div.app-topbar');
  bar.appendChild(h('a.logo', { href: '#/' }, 'Accora IFU'));
  if (crumbs && crumbs.length) {
    const crumbBox = h('div.crumbs');
    crumbs.forEach((c, i) => {
      if (i > 0) crumbBox.appendChild(h('span.sep', '/'));
      if (c.href) crumbBox.appendChild(h('a', { href: c.href }, c.label));
      else crumbBox.appendChild(h('span.current', c.label));
    });
    bar.appendChild(crumbBox);
  }
  bar.appendChild(h('div.spacer'));
  for (const link of (rightLinks || [])) {
    bar.appendChild(h('a.nav-link', { href: link.href }, link.label));
  }
  return bar;
}

// Modal component: open(title, body, footerButtons) -> close()
// body can be a string or a DOM node.  footerButtons is an array of
// {label, primary, danger, onClick} -- onClick gets the close fn.
function openModal({ title, body, footer, width }) {
  _ensureDesignStyles();
  const backdrop = h('div.modal-backdrop');
  const modal = h('div.modal');
  if (width) modal.style.width = (typeof width === 'number' ? width + 'px' : width);

  const closeBtn = h('button.modal-close', { title: 'Close' }, '×');
  const header = h('div.modal-header', [
    h('h2', title || ''),
    closeBtn,
  ]);

  const bodyEl = h('div.modal-body');
  if (typeof body === 'string') bodyEl.appendChild(document.createTextNode(body));
  else if (body instanceof Node) bodyEl.appendChild(body);
  else if (typeof body === 'function') body(bodyEl, _close);   // builder fn

  const footerEl = h('div.modal-footer');
  if (footer && footer.length) {
    for (const b of footer) {
      const cls = 'btn ' + (b.primary ? 'primary' : (b.danger ? 'danger' : ''));
      const btn = h('button', {
        class: cls.trim(),
        onClick: () => b.onClick && b.onClick(_close),
      }, b.label);
      footerEl.appendChild(btn);
    }
  } else {
    footerEl.appendChild(h('button.btn', { onClick: () => _close() }, 'Close'));
  }

  function _close() { backdrop.remove(); document.removeEventListener('keydown', _esc); }
  function _esc(e) { if (e.key === 'Escape') _close(); }
  closeBtn.addEventListener('click', _close);
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) _close();
  });
  document.addEventListener('keydown', _esc);

  modal.appendChild(header);
  modal.appendChild(bodyEl);
  modal.appendChild(footerEl);
  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);
  return { close: _close, body: bodyEl };
}

// Toast: short status message in the bottom-right.
function toast(message, kind /* 'success' | 'error' | undefined */) {
  _ensureDesignStyles();
  let host = document.querySelector('.toast-host');
  if (!host) {
    host = h('div.toast-host');
    document.body.appendChild(host);
  }
  const cls = 'toast' + (kind ? ' ' + kind : '');
  const t = h('div', { class: cls }, message);
  host.appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity 0.25s';
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 280);
  }, 3500);
}

// Attach a "..." action menu to a .card.  items is a list of:
//   { label, onClick: (closeMenu) => void, danger?: true, separator?: true }
//
// The menu pops below the button, closes on outside click / Escape, and
// stops click-propagation so the menu and its actions don't trigger the
// card's own onClick (which would otherwise navigate away while you
// pick "Delete").
function _attachCardMenu(card, items) {
  const btn = h('button.card-menu-btn',
                  { type: 'button', title: 'More actions',
                     'aria-label': 'More actions' },
                  '⋯');
  card.appendChild(btn);

  let menu = null;
  function closeMenu() {
    if (menu) { menu.remove(); menu = null; }
    btn.classList.remove('open');
    document.removeEventListener('click', _docCloser, true);
    document.removeEventListener('keydown', _escCloser);
  }
  function _docCloser(ev) {
    if (menu && !menu.contains(ev.target) && ev.target !== btn) closeMenu();
  }
  function _escCloser(ev) {
    if (ev.key === 'Escape') closeMenu();
  }
  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (menu) { closeMenu(); return; }
    menu = h('div.card-menu');
    for (const it of items) {
      if (it.separator) {
        menu.appendChild(h('div.sep'));
        continue;
      }
      const item = h('div', {
        class: 'item' + (it.danger ? ' danger' : ''),
      }, it.label);
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        closeMenu();
        try { it.onClick(closeMenu); } catch (err) { console.error(err); }
      });
      menu.appendChild(item);
    }
    // Position: anchor below the button.  Since .card has position:relative,
    // we can use right/top in the card's own coordinate space.
    menu.style.top = '32px';
    menu.style.right = '4px';
    card.appendChild(menu);
    btn.classList.add('open');
    // Defer the document listener so the click that opened the menu
    // doesn't immediately close it.
    setTimeout(() => {
      document.addEventListener('click', _docCloser, true);
      document.addEventListener('keydown', _escCloser);
    }, 0);
  });
}

// Simple confirm modal -- returns a Promise<bool>.  Cancel resolves to false.
// Clicking X or the backdrop / pressing Escape also resolves to false.
function confirmModal({ title, body, confirmLabel, danger }) {
  return new Promise((resolve) => {
    let resolved = false;
    const finish = (v) => { if (!resolved) { resolved = true; resolve(v); } };
    openModal({
      title: title || 'Confirm',
      body: typeof body === 'string' ? h('div', body) : body,
      footer: [
        { label: 'Cancel', onClick: (close) => { close(); finish(false); } },
        { label: confirmLabel || 'Confirm',
           primary: !danger, danger: !!danger,
           onClick: (close) => { close(); finish(true); } },
      ],
    });
    // Backstop: if the modal is closed any other way, the modal-backdrop
    // is removed from the DOM.  MutationObserver detects that and treats
    // it as a cancel.
    const obs = new MutationObserver(() => {
      if (!document.querySelector('.modal-backdrop')) {
        obs.disconnect();
        finish(false);
      }
    });
    obs.observe(document.body, { childList: true });
  });
}

// Expose so screens can use them
window.IFU_UI = { openModal, toast, topBar: _topBar,
                    attachCardMenu: _attachCardMenu,
                    confirmModal };
// ===== end G.0 design system =====


// =====================================================================
// F.3 -- Home screen
// =====================================================================
//
// Lists every project as a card grid; recent figures strip below.
// "Open editor" link still works for the legacy single-source flow.

const _HOME_CSS = `
.home-screen {
  max-width: 1100px; margin: 0 auto; padding: 32px 24px;
  font-family: Arial, sans-serif; color: #18181b;
}
.home-screen .topbar { display: flex; align-items: baseline;
                         margin-bottom: 24px; gap: 16px; }
.home-screen .topbar h1 { font-size: 24px; margin: 0;
                            color: #00836a; flex: 1; }
.home-screen .topbar button, .home-screen .topbar a {
  font-size: 13px; padding: 6px 12px; border-radius: 4px;
  border: 1px solid #d4d4d8; background: #fff; cursor: pointer;
  text-decoration: none; color: inherit;
}
.home-screen .topbar button:hover, .home-screen .topbar a:hover {
  background: #cce6e0;
}
.home-screen h2 { font-size: 15px; margin: 24px 0 8px; color: #71717a;
                    text-transform: uppercase; letter-spacing: 0.04em; }
.home-screen .grid { display: grid;
                       grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
                       gap: 16px; }
.home-screen .card {
  border: 1px solid #d4d4d8; border-radius: 6px; padding: 16px;
  background: #fff; cursor: pointer; transition: border-color 0.1s;
}
.home-screen .card:hover { border-color: #00836a; }
.home-screen .card .name { font-size: 15px; font-weight: 600;
                              margin-bottom: 4px; }
.home-screen .card .meta { font-size: 12px; color: #71717a; }
.home-screen .card.placeholder {
  display: flex; align-items: center; justify-content: center;
  color: #71717a; border-style: dashed; min-height: 80px;
}
.home-screen .empty { color: #71717a; font-style: italic;
                        padding: 16px 0; }
.home-screen .recents { list-style: none; padding: 0; margin: 0; }
.home-screen .recents li { display: flex; gap: 8px; padding: 6px 0;
                              border-bottom: 1px solid #f4f4f5;
                              cursor: pointer; }
.home-screen .recents li:hover { background: #f4f4f5; }
.home-screen .recents .figname { flex: 1; }
.home-screen .recents .figmeta { color: #71717a; font-size: 12px; }
`;

function _ensureHomeStyles() {
  if (document.getElementById('home-screen-styles')) return;
  const s = document.createElement('style');
  s.id = 'home-screen-styles';
  s.textContent = _HOME_CSS;
  document.head.appendChild(s);
}

async function HomeScreen(container) {
  _ensureDesignStyles();
  container.className = 'app-shell';

  // Top bar
  container.appendChild(_topBar({
    crumbs: [{ label: 'Home' }],
    rightLinks: [
      { label: 'Legacy editor', href: '' },
      { label: 'Settings', href: '#/settings' },
    ],
  }));

  const main = h('div.app-main');
  container.appendChild(main);

  let projects = [], figures = [];
  let fetchError = null;
  try {
    const [pr, fr] = await Promise.all([
      fetch(API_BASE + '/api/projects'),
      fetch(API_BASE + '/api/figures'),
    ]);
    if (pr.ok) {
      projects = (await pr.json()).projects || [];
    } else {
      fetchError = `/api/projects -> HTTP ${pr.status}`;
    }
    if (fr.ok) {
      figures = (await fr.json()).figures || [];
    } else if (!fetchError) {
      fetchError = `/api/figures -> HTTP ${fr.status}`;
    }
  } catch (e) {
    fetchError = (e && e.message) || 'network failure';
  }

  // If we couldn't reach the server at all, surface the error inline
  // with a Retry button rather than rendering an empty home that the
  // user has no way to recover from.  Previously we silently swallowed
  // the exception, leaving the user staring at "+ New project" with
  // no indication that the server was down.
  if (fetchError && !projects.length && !figures.length) {
    const card = h('div.card', {
      style: { maxWidth: '480px', margin: '40px auto', padding: '24px' },
    }, [
      h('div', { style: { fontSize: '15px', fontWeight: '600',
                              marginBottom: '8px' } },
        'Couldn\\u2019t load projects'),
      h('div', { style: { fontSize: '13px',
                              color: 'var(--c-text-muted)',
                              marginBottom: '16px' } },
        `The server replied: ${fetchError}`),
      h('div', [
        Object.assign(h('button.btn.primary', 'Retry'), {
          onclick: () => renderRoute(),
        }),
        ' ',
        h('a', { href: '#/settings' }, 'Settings'),
      ]),
    ]);
    main.appendChild(card);
    return;
  }

  // Bulk views fetch: one request, server groups by project_id.
  // Replaces an N-deep parallel fan-out that was O(projects x views)
  // disk-walks at scale -- the home page hot path dropped from
  // ~1400 ms to ~30 ms at 50 projects / 200 views.
  let viewsByProject = {};
  try {
    const r = await fetch(API_BASE + '/api/views?group_by_project=1');
    if (r.ok) viewsByProject = (await r.json()).by_project || {};
  } catch (_e) {}

  // Projects -- big tiles with preview thumbnail (from the project's
  // most-recently-updated view) at the top.
  main.appendChild(h('div.section-title', `Projects (${projects.length})`));
  const grid = h('div.card-grid');
  const newCard = h('div.card.placeholder.project-new',
    [h('div', { style: { fontSize: '32px', color: 'var(--c-accora)' } }, '+'),
     h('div', 'New project')]);
  newCard.addEventListener('click', () => _openNewProjectModal());
  grid.appendChild(newCard);

  for (const p of projects) {
    const card = h('div.card.project-card');
    const projViews = viewsByProject[p.id] || [];
    const figcount = projViews.reduce(
      (acc, v) => acc + (v.figure_count || 0), 0);
    // Preview: the newest view that HAS a thumbnail.  Walk the views
    // (newest-first) and on each image error advance to the next, so a
    // project still shows a preview when its latest view hasn't been
    // rendered yet but an older one has.  Monogram only if none do.
    if (projViews.length) {
      const thumb = h('img', {
        alt: '',
        style: { width: '100%', height: '140px',
                    objectFit: 'contain',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    marginBottom: '8px',
                    border: '1px solid var(--c-line)' },
      });
      let _vi = 0;
      const _tryNextView = () => {
        if (_vi >= projViews.length) {
          thumb.replaceWith(_monogramTile(p.name));
          return;
        }
        const v = projViews[_vi++];
        thumb.src = API_BASE + '/api/views/' + encodeURIComponent(v.id)
             + '/thumbnail?v=' + encodeURIComponent(v.updated_at || '');
      };
      thumb.onerror = _tryNextView;
      _tryNextView();
      card.appendChild(thumb);
    } else {
      card.appendChild(_monogramTile(p.name));
    }
    card.appendChild(h('div.card-title', p.name));
    if (p.description) {
      card.appendChild(h('div', { style: { fontSize: '11px',
                                                color: 'var(--c-text-muted)',
                                                margin: '2px 0 4px 0',
                                                whiteSpace: 'nowrap',
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis' } },
                          p.description));
    }
    card.appendChild(h('div.card-meta', [
      h('span', `${projViews.length} view${projViews.length === 1 ? '' : 's'}`),
      h('span', '·'),
      h('span', `${figcount} figure${figcount === 1 ? '' : 's'}`),
      h('span', '·'),
      h('span', (p.updated_at || '').slice(0, 10)),
    ]));
    card.addEventListener('click', () => {
      location.hash = '#/project/' + encodeURIComponent(p.id);
    });
    _attachCardMenu(card, [
      { label: 'Rename...', onClick: () => _renameProject(p) },
      { label: 'Edit description...',
         onClick: () => _editProjectDescription(p) },
      { separator: true },
      { label: 'Delete project...', danger: true,
         onClick: () => _deleteProject(p) },
    ]);
    grid.appendChild(card);
  }
  main.appendChild(grid);

  // Recents
  if (figures.length) {
    main.appendChild(h('div.section-title', 'Recent figures'));
    const recentGrid = h('div.card-grid');
    for (const f of figures.slice(0, 6)) {
      const card = h('div.card', { style: { minHeight: '72px' } });
      card.appendChild(h('div.card-title', f.name || '(untitled)'));
      card.appendChild(h('div.card-meta', [
        h('span', f.source_id || '?'),
        h('span', '·'),
        h('span', (f.updated_at || '').slice(0, 10)),
      ]));
      card.addEventListener('click', () => {
        if (f.project_id) {
          location.hash = '#/project/' + encodeURIComponent(f.project_id)
                        + '/figure/' + encodeURIComponent(f.id);
        } else {
          location.hash = '';
        }
      });
      recentGrid.appendChild(card);
    }
    main.appendChild(recentGrid);
  }
}

// "Monogram" placeholder for projects that have no view (and thus no
// real thumbnail).  Uses the project name's initials, two-tone teal
// background.  Better than a generic "no preview" box for visual
// rhythm on the home page.
function _monogramTile(name) {
  const initials = (name || '?').split(/\s+/).slice(0, 2)
                                  .map(w => w[0] || '')
                                  .join('').toUpperCase() || '?';
  const tile = h('div', {
    style: { width: '100%', height: '140px',
                background: 'linear-gradient(135deg, var(--c-accora) 0%, '
                            + 'var(--c-accora-dark) 100%)',
                color: '#fff',
                borderRadius: 'var(--radius-1)',
                marginBottom: '8px',
                display: 'flex', alignItems: 'center',
                justifyContent: 'center',
                fontSize: '42px', fontWeight: 700,
                letterSpacing: '2px',
                fontFamily: 'var(--font-ui, Inter, sans-serif)' } },
    initials);
  return tile;
}

registerRoute(/^#\/$/, HomeScreen);

// New-project wizard.  A project is a CAD model + the figures
// authored against it -- the model has to be chosen BEFORE any
// figures exist, so the modal won't create the project until the
// user has either imported an Onshape document OR picked one of
// the existing sources.
function _openNewProjectModal() {
  let nameInput, descInput, urlInput, errorBox;
  let progressWrap, progressBar, progressLabel, progressDetail;
  let probeHint, sourceSelect;
  let modeTabImport, modeTabExisting;
  let importPane, existingPane;
  // True after the user has typed in the name field at all, so we
  // don't clobber their text with the auto-probed document name OR
  // existing-source choice.
  let nameTouched = false;
  let probeTimer = null;
  let lastProbedUrl = null;
  let mode = 'import';   // 'import' | 'existing'
  // List of source dicts returned by /api/sources
  let availableSources = [];

  // The "Model" section is a tabbed control: either bring in a new
  // Onshape document or pick one that has already been imported /
  // is part of the bundled demo set.
  modeTabImport   = h('button.tab', 'Import from Onshape');
  modeTabExisting = h('button.tab', 'Use an existing model');

  // ---- Import pane ----------------------------------------------------
  importPane = h('div', [
    h('div.field-row', [
      h('label', 'Onshape document URL'),
      (urlInput = h('input.input', {
        placeholder: 'https://cad.onshape.com/documents/...',
        style: { width: '100%', fontFamily: 'var(--font-mono)',
                    fontSize: '12px' },
        autocomplete: 'off',
        spellcheck: false,
      })),
      (probeHint = h('div', { style: { fontSize: '11px',
                                              color: 'var(--c-text-muted)',
                                              marginTop: '4px',
                                              minHeight: '14px' } }, '')),
    ]),
  ]);

  // ---- Existing pane --------------------------------------------------
  existingPane = h('div', { style: { display: 'none' } }, [
    h('div.field-row', [
      h('label', 'Model'),
      (sourceSelect = h('select.select', {
        style: { width: '100%' },
      })),
      h('div', { style: { fontSize: '11px',
                              color: 'var(--c-text-muted)',
                              marginTop: '4px' } },
        'Demo assemblies and previously imported Onshape documents.'),
    ]),
  ]);

  // ---- Modal body -----------------------------------------------------
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Project name'),
      (nameInput = h('input.input', {
        placeholder: 'e.g. Presto IFU R03',
        style: { width: '100%' },
      })),
    ]),
    h('div.field-row', [
      h('label', 'Description (optional)'),
      (descInput = h('input.input', {
        placeholder: 'short description shown on the home card',
        style: { width: '100%' },
      })),
    ]),
    h('div', { style: { marginTop: '8px',
                            fontSize: 'var(--t-meta)',
                            fontWeight: 600,
                            color: 'var(--c-text)' } },
      'Model'),
    h('div', { style: { display: 'flex',
                            gap: '4px',
                            borderBottom: '1px solid var(--c-line)',
                            marginBottom: '12px' } },
        [modeTabImport, modeTabExisting]),
    importPane,
    existingPane,
    (errorBox = h('div', { style: { display: 'none',
                                          padding: '8px 12px',
                                          marginTop: '4px',
                                          background: '#fef2f2',
                                          border: '1px solid #fecaca',
                                          color: '#991b1b',
                                          borderRadius: 'var(--radius-1)',
                                          fontSize: '12px' } })),

    // Progress block, shown only during import
    (progressWrap = h('div', { style: { display: 'none',
                                              marginTop: '16px',
                                              padding: '16px',
                                              background: 'var(--c-accora-pale)',
                                              borderRadius: 'var(--radius-1)' } }, [
      (progressLabel = h('div', { style: { fontWeight: 600,
                                                  marginBottom: '6px',
                                                  color: 'var(--c-accora-dark)' } },
                          'Connecting to Onshape...')),
      (progressDetail = h('div', { style: { fontSize: '12px',
                                                   color: 'var(--c-text-muted)',
                                                   marginBottom: '10px' } }, '')),
      h('div', { style: { height: '6px',
                              background: 'var(--c-surface-1)',
                              borderRadius: '3px',
                              overflow: 'hidden' } }, [
        (progressBar = h('div', { style: { height: '100%',
                                                  width: '0%',
                                                  background: 'var(--c-accora)',
                                                  transition: 'width 0.3s ease' } })),
      ]),
    ])),
  ]);

  // ---- Tab styling + switching ----------------------------------------
  function _styleTab(btn, active) {
    btn.style.background = 'transparent';
    btn.style.border = 'none';
    btn.style.borderBottom = active
      ? '2px solid var(--c-accora)'
      : '2px solid transparent';
    btn.style.color = active ? 'var(--c-accora-dark)' : 'var(--c-text-muted)';
    btn.style.fontWeight = active ? '600' : '400';
    btn.style.fontSize = 'var(--t-body)';
    btn.style.padding = '8px 12px';
    btn.style.cursor = 'pointer';
    btn.style.marginBottom = '-1px';
  }
  function _setMode(next) {
    mode = next;
    _styleTab(modeTabImport,   mode === 'import');
    _styleTab(modeTabExisting, mode === 'existing');
    importPane.style.display   = mode === 'import'   ? 'block' : 'none';
    existingPane.style.display = mode === 'existing' ? 'block' : 'none';
    // Update auto-name from the freshly active pane
    if (!nameTouched) {
      if (mode === 'existing' && sourceSelect.value) {
        const src = availableSources.find(s => s.id === sourceSelect.value);
        if (src) nameInput.value = src.label || src.id;
      } else if (mode === 'import' && lastProbedUrl) {
        // Leave whatever the probe set
      }
    }
  }
  modeTabImport.addEventListener('click', (e) => {
    e.preventDefault(); _setMode('import');
  });
  modeTabExisting.addEventListener('click', (e) => {
    e.preventDefault(); _setMode('existing');
  });
  _setMode('import');

  // ---- Load existing-source list (async) ------------------------------
  fetch(API_BASE + '/api/sources').then(r => r.json()).then(data => {
    availableSources = data.sources || [];
    sourceSelect.innerHTML = '';
    if (!availableSources.length) {
      const opt = document.createElement('option');
      opt.disabled = true; opt.textContent = '(no models available)';
      sourceSelect.appendChild(opt);
      return;
    }
    for (const s of availableSources) {
      const opt = document.createElement('option');
      opt.value = s.id;
      const tag = s.origin === 'dynamic' ? ' (Onshape)' : ' (demo)';
      opt.textContent = s.label + tag;
      sourceSelect.appendChild(opt);
    }
    // If user is on the Existing tab and hasn't typed a name yet,
    // seed it from the default-selected source.
    if (mode === 'existing' && !nameTouched && availableSources[0]) {
      nameInput.value = availableSources[0].label || availableSources[0].id;
    }
  }).catch(() => {});

  sourceSelect.addEventListener('change', () => {
    if (mode === 'existing' && !nameTouched) {
      const src = availableSources.find(s => s.id === sourceSelect.value);
      if (src) nameInput.value = src.label || src.id;
    }
  });

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.style.display = 'block';
  }
  function hideError() {
    errorBox.style.display = 'none';
  }
  function setProgress(pct, label, detail) {
    progressWrap.style.display = 'block';
    progressBar.style.width = Math.max(0, Math.min(100, pct)) + '%';
    if (label != null) progressLabel.textContent = label;
    if (detail != null) progressDetail.textContent = detail;
  }
  function clearProgress() {
    progressWrap.style.display = 'none';
    progressBar.style.width = '0%';
    progressLabel.textContent = '';
    progressDetail.textContent = '';
  }

  // Track the in-flight import job so the error path can fire-and-forget
  // a cancel request to the server.  Without this, the user gets back
  // to the modal but the worker keeps churning until the next checkpoint.
  let _inFlightJobId = null;

  async function pollImport(jobId) {
    _inFlightJobId = jobId;
    try {
      while (true) {
        await new Promise(res => setTimeout(res, 1500));
        const r = await fetch(API_BASE + '/api/onshape/import/'
                                + encodeURIComponent(jobId));
        if (!r.ok) throw new Error('poll failed: HTTP ' + r.status);
        const job = await r.json();
        setProgress(job.progress || 0,
                      _labelForImportStatus(job),
                      job.message || '');
        if (job.status === 'ready') return job;
        if (job.status === 'cancelled') {
          throw new Error('import cancelled');
        }
        if (job.status === 'error') {
          throw new Error(job.error || job.message || 'import failed');
        }
      }
    } finally {
      _inFlightJobId = null;
    }
  }

  function _cancelInFlight() {
    if (!_inFlightJobId) return;
    fetch(API_BASE + '/api/onshape/import/'
            + encodeURIComponent(_inFlightJobId), {
      method: 'DELETE',
    }).catch(() => {});
  }

  // Mark the name as user-touched once they type anything.  Stops the
  // debounced probe from clobbering their text.
  nameInput.addEventListener('input', () => {
    if (nameInput.value.trim()) nameTouched = true;
  });

  async function probeUrl(url) {
    if (url === lastProbedUrl) return;
    lastProbedUrl = url;
    probeHint.textContent = 'checking Onshape...';
    probeHint.style.color = 'var(--c-text-muted)';
    try {
      const r = await fetch(API_BASE + '/api/onshape/probe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const data = await r.json();
      if (!r.ok) {
        probeHint.textContent = data.error || 'could not read URL';
        probeHint.style.color = 'var(--c-danger)';
        return;
      }
      const docName = data.document_name || '';
      const elName = data.element_name || '';
      probeHint.textContent = `${docName} · ${elName}`;
      probeHint.style.color = 'var(--c-accora)';
      if (!nameTouched && docName) {
        nameInput.value = docName;
      }
    } catch (e) {
      probeHint.textContent = 'probe failed: ' + (e.message || e);
      probeHint.style.color = 'var(--c-danger)';
    }
  }

  urlInput.addEventListener('input', () => {
    const url = (urlInput.value || '').trim();
    if (probeTimer) clearTimeout(probeTimer);
    if (!url) {
      probeHint.textContent = '';
      lastProbedUrl = null;
      return;
    }
    // Don't fire the probe until the URL looks structurally valid --
    // saves a request per keystroke while typing.
    if (!/\/documents\/[0-9a-f]{16,}\/[wvm]\//i.test(url)) {
      probeHint.textContent = '';
      lastProbedUrl = null;
      return;
    }
    probeTimer = setTimeout(() => probeUrl(url), 600);
  });

  const modal = openModal({
    title: 'New project',
    body,
    footer: [
      (cancelBtn = { label: 'Cancel', onClick: (close) => close() }),
      (createBtn = { label: 'Create', primary: true,
                       onClick: async (close) => {
        const name = (nameInput.value || '').trim();
        if (!name) { nameInput.focus(); return; }
        const description = (descInput.value || '').trim();
        hideError();

        // Enforce: the user must have chosen a model
        let primary_source_id = null;
        let onshape_ids = null;
        let importedJob = null;

        if (mode === 'import') {
          const url = (urlInput.value || '').trim();
          if (!url) {
            showError('Paste an Onshape document URL, or switch to '
                        + '"Use an existing model".');
            urlInput.focus();
            return;
          }
          // Disable inputs once we start
          nameInput.disabled = true;
          descInput.disabled = true;
          urlInput.disabled = true;
          modeTabImport.disabled = true;
          modeTabExisting.disabled = true;
          try {
            setProgress(2, 'Starting import...', url);
            const r0 = await fetch(API_BASE + '/api/onshape/import', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ url }),
            });
            if (!r0.ok) {
              const j = await r0.json().catch(() => ({}));
              throw new Error(j.error || ('HTTP ' + r0.status));
            }
            const job0 = await r0.json();
            importedJob = await pollImport(job0.id);
            primary_source_id = importedJob.source_id || null;
            onshape_ids = importedJob.onshape_ids || null;
            setProgress(100, 'Import complete', primary_source_id);
          } catch (e) {
            // Tell the server to stop the worker -- otherwise it keeps
            // churning at the next checkpoint until translation timeout.
            _cancelInFlight();
            showError(e.message || String(e));
            clearProgress();
            nameInput.disabled = false;
            descInput.disabled = false;
            urlInput.disabled = false;
            urlInput.focus();
            modeTabImport.disabled = false;
            modeTabExisting.disabled = false;
            return;
          }
        } else {
          // mode === 'existing'
          primary_source_id = sourceSelect.value || null;
          if (!primary_source_id) {
            showError('Pick a model from the list.');
            return;
          }
          const src = availableSources.find(s => s.id === primary_source_id);
          if (src && src.onshape_ids) onshape_ids = src.onshape_ids;
        }

        // Create the project record (model is now committed)
        try {
          const pr = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description,
                                      primary_source_id, onshape_ids }),
          });
          if (!pr.ok) throw new Error('project create failed: HTTP ' + pr.status);
          const p = await pr.json();
          close();
          toast(importedJob
                  ? 'Project created from Onshape document'
                  : 'Project created', 'success');
          location.hash = '#/project/' + encodeURIComponent(p.id);
        } catch (e) {
          showError(e.message || String(e));
          clearProgress();
          nameInput.disabled = false;
          descInput.disabled = false;
          if (urlInput) urlInput.disabled = false;
          modeTabImport.disabled = false;
          modeTabExisting.disabled = false;
        }
      } }),
    ],
  });
  setTimeout(() => nameInput.focus(), 50);
  return modal;
}

function _labelForImportStatus(job) {
  switch (job.status) {
    case 'queued':      return 'Queued';
    case 'resolving':   return 'Reading document metadata...';
    case 'translating': return 'Onshape is converting your assembly to STEP...';
    case 'downloading': return 'Downloading STEP geometry...';
    case 'ready':       return 'Done';
    case 'error':       return 'Import failed';
    default:            return job.status || '...';
  }
}
// ===== end F.3 Home screen =====


// =====================================================================
// F.4 -- Project workspace screen
// =====================================================================
//
// One project at a time.  Breadcrumb back to home.  Figure grid +
// "new figure" card.  Source binding bar (shows the source + revision
// status; refresh button hits the existing /api/sources/.../refresh
// endpoint).

async function ProjectScreen(container, params) {
  _ensureDesignStyles();
  container.className = 'app-shell';
  const projId = params[0];

  let proj = null, figs = [], sources = [];
  try {
    const [pr, fr, sr] = await Promise.all([
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId)),
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId) + '/figures'),
      fetch(API_BASE + '/api/sources'),
    ]);
    if (pr.ok) proj = await pr.json();
    if (fr.ok) figs = (await fr.json()).figures || [];
    if (sr.ok) sources = (await sr.json()).sources || [];
  } catch (_e) {}

  if (!proj) {
    container.appendChild(_topBar({
      crumbs: [{ label: 'Home', href: '#/' }, { label: 'Project not found' }],
    }));
    const main = h('div.app-main');
    main.appendChild(h('p', { style: { color: 'var(--c-text-muted)' } },
                        'This project could not be loaded.'));
    main.appendChild(h('a.btn', { href: '#/' }, '← Back to home'));
    container.appendChild(main);
    return;
  }

  AppState.currentProjectId = projId;

  container.appendChild(_topBar({
    crumbs: [
      { label: 'Home', href: '#/' },
      { label: proj.name },
    ],
    rightLinks: [
      { label: 'Settings', href: '#/settings' },
    ],
  }));
  const main = h('div.app-main');
  container.appendChild(main);

  if (proj.description) {
    main.appendChild(h('p', { style: { color: 'var(--c-text-muted)',
                                            margin: '0 0 24px 0' } },
                        proj.description));
  }

  // Source status bar.  Prefer the project's primary source -- that
  // is the model the project IS.  Fall back to the union of figure
  // sources for legacy projects that don't have a primary set.
  let usedSourceIds;
  if (proj.primary_source_id) {
    usedSourceIds = [proj.primary_source_id];
    // Include any orphan sources used by older figures so the user
    // still sees them in the bar (rare).
    for (const f of figs) {
      if (f.source_id && f.source_id !== proj.primary_source_id
          && !usedSourceIds.includes(f.source_id)) {
        usedSourceIds.push(f.source_id);
      }
    }
  } else {
    usedSourceIds = [...new Set(figs.map(f => f.source_id).filter(Boolean))];
  }
  if (usedSourceIds.length) {
    const bar = h('div', { style: {
        background: 'var(--c-surface)', border: '1px solid var(--c-line)',
        borderRadius: 'var(--radius-2)', padding: '12px 16px',
        marginBottom: '24px',
        display: 'flex', alignItems: 'center', gap: '12px',
        fontSize: 'var(--t-body)',
      } });
    bar.appendChild(h('span', { style: { color: 'var(--c-text-muted)' } }, 'Sources:'));
    for (const sid of usedSourceIds) {
      const src = sources.find(s => s.id === sid);
      bar.appendChild(h('span', { style: { fontWeight: '500' } },
                        src?.label || sid));
      bar.appendChild(h('span.badge.ok', sid));
    }
    bar.appendChild(h('div', { style: { flex: '1' } }));
    const refreshBtn = h('button.btn', '↻ Refresh Onshape Versions');
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      refreshBtn.textContent = 'Refreshing...';
      let ok = 0, fail = 0;
      for (const sid of usedSourceIds) {
        try {
          const r = await fetch(API_BASE + '/api/sources/'
                                   + encodeURIComponent(sid)
                                   + '/versions/refresh', { method: 'POST' });
          if (r.ok) ok++; else fail++;
        } catch (_e) { fail++; }
      }
      toast(`Refreshed ${ok} source(s)` + (fail ? `, ${fail} failed` : ''),
            fail ? 'error' : 'success');
      refreshBtn.disabled = false;
      refreshBtn.textContent = '↻ Refresh Onshape Versions';
    });
    bar.appendChild(refreshBtn);
    main.appendChild(bar);
  }

  // Views grid: each view = camera angle, owns 1..N figures (highlight
  // variants).  "New view" sends the user into the editor in a special
  // "create-view" mode that captures whatever camera angle they choose.
  let views = [];
  try {
    const vr = await fetch(API_BASE + '/api/projects/'
                            + encodeURIComponent(projId) + '/views');
    if (vr.ok) views = (await vr.json()).views || [];
  } catch (_e) {}

  main.appendChild(h('div.section-title', `Views (${views.length})`));
  const grid = h('div.card-grid');
  const newCard = h('div.card.placeholder',
    [h('div', { style: { fontSize: '24px' } }, '+'),
     h('div', 'New view')]);
  newCard.addEventListener('click', () => _openNewViewModal(projId, proj));
  grid.appendChild(newCard);

  for (const view of views) {
    const card = h('div.card.figure-card');
    const thumb = h('img', {
      src: API_BASE + '/api/views/' + encodeURIComponent(view.id)
           + '/thumbnail?v=' + encodeURIComponent(view.updated_at || ''),
      alt: '',
      style: { width: '100%', height: '96px',
                  objectFit: 'contain',
                  background: 'var(--c-surface-1)',
                  borderRadius: 'var(--radius-1)',
                  marginBottom: '8px',
                  border: '1px solid var(--c-line)' },
    });
    thumb.onerror = () => {
      // Small monogram from the view name instead of a screaming
      // "no preview yet" rectangle.  Pulls the first two letters
      // of the view name onto a tinted block -- much calmer in a
      // dense grid of N views.
      const initials = (view.name || '?').replace(/[^a-zA-Z0-9]/g, '')
                          .slice(0, 2).toUpperCase() || '·';
      thumb.replaceWith(h('div', {
        style: { width: '100%', height: '96px',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    border: '1px solid var(--c-line)',
                    marginBottom: '8px',
                    display: 'flex', alignItems: 'center',
                    justifyContent: 'center',
                    color: 'var(--c-text-muted)',
                    fontSize: '22px', fontWeight: '600',
                    letterSpacing: '1px' } },
        initials));
    };
    card.appendChild(thumb);
    card.appendChild(h('div.card-title', view.name || '(untitled view)'));
    const n = view.figure_count || (view.figure_ids || []).length;
    card.appendChild(h('div.card-meta', [
      h('span', n + (n === 1 ? ' figure' : ' figures')),
      h('span', '·'),
      h('span', (view.updated_at || '').slice(0, 10)),
    ]));
    card.addEventListener('click', () => {
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(view.id);
    });
    _attachCardMenu(card, [
      { label: 'Rename...', onClick: () => _renameView(view, projId) },
      { separator: true },
      { label: 'Delete view...', danger: true,
         onClick: () => _deleteView(view, projId) },
    ]);
    grid.appendChild(card);
  }
  main.appendChild(grid);

  // Legacy figures section: figures that aren't attached to any view
  // (= pre-Phase-3 data the migration couldn't link, OR figures whose
  // view got deleted).  Surfaced so the user can recover them.
  const orphanFigs = figs.filter(f => !f.view_id
                                       || !views.some(v => v.id === f.view_id));
  if (orphanFigs.length) {
    main.appendChild(h('div.section-title',
      { style: { marginTop: '32px', color: 'var(--c-text-muted)' } },
      `Unfiled figures (${orphanFigs.length})`));
    main.appendChild(h('p', { style: { fontSize: '12px',
                                              color: 'var(--c-text-muted)',
                                              margin: '0 0 12px 0' } },
      'These figures predate the View layer.  Open them to assign a View, '
      + 'or delete them.'));
    const orphanGrid = h('div.card-grid');
    for (const fig of orphanFigs) {
      const card = h('div.card.figure-card');
      const thumb = h('img', {
        src: API_BASE + '/api/figures/' + encodeURIComponent(fig.id)
             + '/thumbnail?v=' + encodeURIComponent(fig.updated_at || ''),
        alt: '',
        style: { width: '100%', height: '120px',
                    objectFit: 'contain',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    marginBottom: '6px',
                    border: '1px solid var(--c-line)' },
      });
      thumb.onerror = () => {
        thumb.replaceWith(h('div', {
          style: { width: '100%', height: '120px',
                      background: 'var(--c-surface-1)',
                      borderRadius: 'var(--radius-1)',
                      border: '1px dashed var(--c-line)',
                      marginBottom: '6px',
                      display: 'flex', alignItems: 'center',
                      justifyContent: 'center',
                      color: 'var(--c-text-muted)',
                      fontSize: '11px', fontStyle: 'italic' } },
          'no preview yet'));
      };
      card.appendChild(thumb);
      card.appendChild(h('div.card-title', fig.name || '(untitled)'));
      card.appendChild(h('div.card-meta', [
        h('span', fig.source_id || '?'),
        h('span', '·'),
        h('span', (fig.updated_at || '').slice(0, 10)),
      ]));
      card.addEventListener('click', () => {
        location.hash = '#/project/' + encodeURIComponent(projId)
                      + '/figure/' + encodeURIComponent(fig.id);
      });
      _attachCardMenu(card, [
        { label: 'Rename...', onClick: () => _renameFigure(fig, projId) },
        { separator: true },
        { label: 'Delete figure...', danger: true,
           onClick: () => _deleteFigure(fig, projId) },
      ]);
      orphanGrid.appendChild(card);
    }
    main.appendChild(orphanGrid);
  }
}

// Open the editor with the project's primary source so the user can
// pose the camera and "Save view".  No view created up-front -- we
// stamp the View on first save so deleted-without-save doesn't leave
// empty Views littering the project.
async function _openNewViewModal(projId, proj) {
  // Real "create a view" flow: name it, optionally seed the camera
  // from an existing view of the same project, then POST /api/views.
  // The ViewScreen handler picks it up, creates a Default variant,
  // and drops the user into the editor.
  let nameInput, seedSelect;

  // Pull the existing views so the user can copy a camera from one
  // of them.  Default to no seeding -> the editor's iso default.
  let existing = [];
  try {
    const r = await fetch(API_BASE + '/api/projects/'
                            + encodeURIComponent(projId) + '/views');
    if (r.ok) existing = (await r.json()).views || [];
  } catch (_e) {}

  const fields = [
    h('div.field-row', [
      h('label', 'View name'),
      (nameInput = h('input.input', {
        placeholder: 'e.g. "Front 3/4"',
        value: 'View ' + (existing.length + 1),
        style: { width: '100%' },
      })),
    ]),
  ];
  if (existing.length) {
    seedSelect = h('select.input', { style: { width: '100%' } });
    seedSelect.appendChild(
      h('option', { value: '' }, '(use source default)'));
    for (const v of existing) {
      seedSelect.appendChild(
        h('option', { value: v.id }, v.name || '(untitled)'));
    }
    fields.push(h('div.field-row', [
      h('label', 'Seed camera from'),
      seedSelect,
    ]));
  }

  openModal({
    title: 'New view',
    body: h('div', fields),
    footer: [
      { label: 'Cancel', onClick: (close) => close() },
      { label: 'Create view', primary: true,
         onClick: async (close) => {
        const name = (nameInput.value || '').trim() || 'Untitled view';
        let camera = null;
        let configuration = null;
        const seedId = seedSelect ? seedSelect.value : '';
        if (seedId) {
          try {
            const sr = await fetch(API_BASE + '/api/views/'
                                     + encodeURIComponent(seedId));
            if (sr.ok) {
              const seed = await sr.json();
              camera = seed.camera || null;
              configuration = seed.configuration || null;
            }
          } catch (_e) {}
        }
        try {
          const r = await fetch(API_BASE + '/api/views', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              project_id: projId,
              name,
              camera,
              configuration,
            }),
          });
          if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            throw new Error(j.error || ('HTTP ' + r.status));
          }
          const v = await r.json();
          close();
          // Hand off to ViewScreen which creates the Default variant
          // figure and drops the user into the editor.
          location.hash = '#/project/' + encodeURIComponent(projId)
                        + '/view/' + encodeURIComponent(v.id);
        } catch (e) {
          toast('Create view failed: ' + (e.message || e), 'error');
        }
      } },
    ],
  });
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 50);
}

async function _renameView(view, projId) {
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'View name'),
      (nameInput = h('input.input', { value: view.name || '',
                                          style: { width: '100%' } })),
    ]),
  ]);
  openModal({
    title: 'Rename view',
    body,
    footer: [
      { label: 'Cancel', onClick: (close) => close() },
      { label: 'Save', primary: true, onClick: async (close) => {
        const name = (nameInput.value || '').trim();
        if (!name) { nameInput.focus(); return; }
        try {
          const r = await fetch(API_BASE + '/api/views/'
                                  + encodeURIComponent(view.id),
                                  { method: 'PUT',
                                     headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify({ ...view, name }) });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          close();
          toast('View renamed', 'success');
          if (window.IFU_APP?.renderRoute) window.IFU_APP.renderRoute();
        } catch (e) {
          toast('Rename failed: ' + (e.message || e), 'error');
        }
      } },
    ],
  });
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 50);
}

async function _deleteView(view, projId) {
  const n = view.figure_count || (view.figure_ids || []).length;
  const body = h('div', [
    h('p', { style: { marginTop: 0 } },
      'Delete the view ', h('strong', view.name || '(untitled)'),
      n ? ` and its ${n} figure${n === 1 ? '' : 's'}?` : '?'),
    h('p', { style: { color: 'var(--c-text-muted)',
                          fontSize: '12px', marginBottom: 0 } },
      'This action cannot be undone.'),
  ]);
  const ok = await new Promise((resolve) => {
    openModal({
      title: 'Delete view?',
      body,
      footer: [
        { label: 'Cancel', onClick: (close) => { close(); resolve(false); } },
        { label: 'Delete', danger: true, onClick: (close) => {
          close(); resolve(true);
        } },
      ],
    });
  });
  if (!ok) return;
  try {
    const r = await fetch(API_BASE + '/api/views/'
                            + encodeURIComponent(view.id) + '?cascade=1',
                            { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('View deleted', 'success');
    if (window.IFU_APP?.renderRoute) window.IFU_APP.renderRoute();
  } catch (e) {
    toast('Delete failed: ' + (e.message || e), 'error');
  }
}

function _openNewFigureModal(projId, sources, proj) {
  let nameInput, sourceSelect, viewSelect, captureCurrent;
  let configWrap, configStatus;
  // configInputs keys off parameter id; values are <select> or <input>
  const configInputs = {};

  // The project owns its model -- figures inherit it.  Only legacy
  // projects (created before this constraint) fall back to letting
  // the user pick.
  const projSourceId = proj && proj.primary_source_id;
  const projSource = projSourceId
    ? (sources || []).find(s => s.id === projSourceId) || null
    : null;
  const legacyMode = !projSourceId;

  // Detect current 3D camera if the editor was open before.  Falls
  // back to null -- the figure will use its source's iso preset.
  const currentCam = (() => {
    try {
      const c = window.IFU_VIEWER && window.IFU_VIEWER.getCameraEyeTarget?.();
      return c || null;
    } catch (_e) { return null; }
  })();

  // Source-area renders differently depending on whether the project
  // already has a model bound.
  const sourceArea = legacyMode
    ? h('div.field-row', [
        h('label', 'Source'),
        (sourceSelect = h('select.select', { style: { width: '100%' } })),
      ])
    : h('div', { style: { padding: '10px 12px',
                              background: 'var(--c-surface-1)',
                              borderRadius: 'var(--radius-1)',
                              fontSize: 'var(--t-body)',
                              display: 'flex', alignItems: 'center',
                              gap: '8px' } }, [
        h('span', { style: { color: 'var(--c-text-muted)' } }, 'Model:'),
        h('strong', projSource ? projSource.label : projSourceId),
        h('span.badge.ok',
          (projSource && projSource.origin === 'dynamic')
            ? 'Onshape' : 'demo'),
      ]);

  const body = h('div', [
    h('div.field-row', [
      h('label', 'Figure name'),
      (nameInput = h('input.input', { placeholder: 'e.g. Side rail close-up',
                                         style: { width: '100%' } })),
    ]),
    sourceArea,
    h('div.field-row', [
      h('label', 'Starting view'),
      (viewSelect = h('select.select', { style: { width: '100%' } })),
    ]),
    currentCam
      ? h('div', { style: { marginTop: '8px', padding: '8px 12px',
                                background: 'var(--c-accora-pale)',
                                borderRadius: 'var(--radius-1)',
                                fontSize: 'var(--t-meta)',
                                color: 'var(--c-accora-dark)' } }, [
          (captureCurrent = h('input', { type: 'checkbox', checked: true,
                                            style: { marginRight: '6px' } })),
          h('span', "Use my current 3D camera angle as the figure's view"),
        ])
      : null,
    // Onshape configuration block (populated when source has onshape_ids)
    (configWrap = h('div', { style: { marginTop: '12px', display: 'none' } }, [
      h('div', { style: { fontSize: 'var(--t-meta)', fontWeight: 600,
                              color: 'var(--c-text)', marginBottom: '6px' } },
        'Onshape configuration'),
      (configStatus = h('div', { style: { fontSize: '12px',
                                                color: 'var(--c-text-muted)',
                                                marginBottom: '8px' } },
                          'loading...')),
    ])),
    h('div', { style: { marginTop: '12px', fontSize: '12px',
                            color: 'var(--c-text-muted)' } },
      'You can re-pose the camera in the editor at any time.'),
  ]);
  // Populate source dropdown only in legacy mode
  if (legacyMode && sourceSelect) {
    for (const s of (sources || [])) {
      const opt = document.createElement('option');
      opt.value = s.id;
      let suffix = '';
      if (s.origin === 'dynamic') suffix = '  (Onshape import)';
      else if (!s.onshape_ids) suffix = '  (local)';
      opt.textContent = `${s.label}${suffix}`;
      sourceSelect.appendChild(opt);
    }
  }
  // Starting-view presets -- this is just a default if no camera capture
  ['iso', 'front', 'side'].forEach(vid => {
    const opt = document.createElement('option');
    opt.value = vid; opt.textContent = vid;
    viewSelect.appendChild(opt);
  });

  // Resolve the active source id at any moment -- driven by the
  // dropdown in legacy mode, or by the project binding otherwise.
  function _activeSourceId() {
    if (legacyMode && sourceSelect) return sourceSelect.value;
    return projSourceId;
  }

  // Fetch configuration parameters for the active source.  Only
  // sources with onshape_ids will return any -- everything else gets
  // ``has_config: false`` and we hide the block.
  async function refreshConfigForSource() {
    // Clear existing inputs
    Object.keys(configInputs).forEach(k => delete configInputs[k]);
    while (configWrap.children.length > 2) {
      configWrap.removeChild(configWrap.lastChild);
    }
    const sid = _activeSourceId();
    const src = (sources || []).find(s => s.id === sid);
    if (!src || !src.onshape_ids) {
      configWrap.style.display = 'none';
      return;
    }
    configWrap.style.display = 'block';
    configStatus.textContent = 'loading parameters...';
    try {
      const r = await fetch(API_BASE + '/api/sources/'
                              + encodeURIComponent(sid) + '/configuration');
      if (!r.ok) {
        configStatus.textContent = 'parameters unavailable';
        return;
      }
      const cfg = await r.json();
      if (!cfg.has_config || !cfg.parameters?.length) {
        configStatus.textContent =
          'this assembly has no configurable parameters';
        return;
      }
      configStatus.textContent =
        cfg.parameters.length + ' parameter'
        + (cfg.parameters.length === 1 ? '' : 's')
        + ' available -- pick variant to render';
      for (const p of cfg.parameters) {
        const labelEl = h('label', {
          style: { marginBottom: 0,
                      fontSize: 'var(--t-body)',
                      color: 'var(--c-text)',
                      fontWeight: 500 } },
          p.name || p.id || '(unnamed parameter)');
        const row = h('div', {
          style: { display: 'grid',
                      gridTemplateColumns: '160px 1fr',
                      gap: '12px',
                      alignItems: 'center',
                      marginTop: '6px' } }, [labelEl]);

        if (p.type === 'enum' && p.options?.length) {
          const sel = h('select.select', { style: { width: '100%' } });
          for (const o of p.options) {
            const opt = document.createElement('option');
            opt.value = o.value;
            opt.textContent = o.label;
            if (o.value === p.default) opt.selected = true;
            sel.appendChild(opt);
          }
          row.appendChild(sel);
          configInputs[p.id] = sel;
        } else if (p.type === 'boolean') {
          const wrap = h('label', {
            style: { display: 'flex', alignItems: 'center',
                        gap: '8px', cursor: 'pointer',
                        fontSize: 'var(--t-meta)',
                        color: 'var(--c-text-muted)' } });
          const cb = h('input', { type: 'checkbox' });
          if (p.default === true || p.default === 'true') cb.checked = true;
          wrap.appendChild(cb);
          wrap.appendChild(h('span', cb.checked ? 'enabled' : 'disabled'));
          cb.addEventListener('change', () => {
            wrap.lastChild.textContent = cb.checked ? 'enabled' : 'disabled';
          });
          // Read .value as a string so the create-handler can write it
          // to the configuration map uniformly.
          Object.defineProperty(cb, 'value', {
            get() { return cb.checked ? 'true' : 'false'; },
          });
          row.appendChild(wrap);
          configInputs[p.id] = cb;
        } else if (p.type === 'quantity') {
          const inner = h('div', {
            style: { display: 'flex', alignItems: 'center',
                        gap: '6px' } });
          const inp = h('input.input', {
            type: 'text',
            placeholder: p.default != null
              ? `default: ${p.default}` : '',
            style: { flex: '1', minWidth: 0 },
          });
          if (p.default != null) inp.value = String(p.default);
          inner.appendChild(inp);
          if (p.unit) {
            inner.appendChild(h('span', {
              style: { color: 'var(--c-text-muted)',
                          fontSize: 'var(--t-meta)' } },
              p.unit));
          }
          row.appendChild(inner);
          configInputs[p.id] = inp;
        } else {
          // string / unknown: plain text input
          const inp = h('input.input', {
            type: 'text',
            placeholder: p.default != null
              ? `default: ${p.default}` : '',
            style: { width: '100%' },
          });
          if (p.default != null) inp.value = String(p.default);
          row.appendChild(inp);
          configInputs[p.id] = inp;
        }
        configWrap.appendChild(row);
      }
    } catch (e) {
      configStatus.textContent = 'error: ' + (e.message || e);
    }
  }
  if (legacyMode && sourceSelect) {
    sourceSelect.addEventListener('change', refreshConfigForSource);
  }
  // Kick off for whichever source is active by default
  setTimeout(refreshConfigForSource, 0);

  openModal({
    title: 'New figure',
    body,
    footer: [
      { label: 'Cancel', onClick: (close) => close() },
      { label: 'Create + open editor', primary: true, onClick: async (close) => {
        const name = (nameInput.value || '').trim();
        if (!name) { nameInput.focus(); return; }
        const sourceId = _activeSourceId();
        if (!sourceId) return;
        const useCurrent = currentCam && captureCurrent && captureCurrent.checked;
        // Pull configuration values into the figure payload
        const configValues = {};
        let configCount = 0;
        for (const [pid, el] of Object.entries(configInputs)) {
          const v = el.value;
          if (v !== undefined && v !== null && v !== '') {
            configValues[pid] = v;
            configCount++;
          }
        }
        const payload = {
          name, source_id: sourceId, project_id: projId,
          view_id: useCurrent ? 'custom' : (viewSelect.value || 'iso'),
        };
        if (useCurrent) {
          payload.camera = {
            eye: currentCam.eye, target: currentCam.target,
            up_axis: 'z',
          };
        }
        if (configCount > 0) {
          payload.configuration = configValues;
        }
        try {
          const r = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const f = await r.json();
          close();
          toast('Figure created' + (useCurrent ? ' with current 3D pose' : ''),
                'success');
          location.hash = '#/project/' + encodeURIComponent(projId)
                        + '/figure/' + encodeURIComponent(f.id);
        } catch (e) {
          toast('Create failed: ' + (e.message || 'unknown'), 'error');
        }
      } },
    ],
  });
  setTimeout(() => nameInput.focus(), 50);
}

registerRoute(/^#\/project\/([^/]+)$/, ProjectScreen);


// =====================================================================
// Phase 3 -- View workspace (figures within a view)
// =====================================================================

async function ViewScreen(container, params) {
  _ensureDesignStyles();
  container.className = 'app-shell';
  const projId = params[0];
  const viewId = params[1];

  // Special "new view" route: redirect to the editor with the project's
  // primary source so the user can pose the camera and Save view.
  if (viewId === '__new__') {
    location.hash = '#/project/' + encodeURIComponent(projId)
                  + '/figure/__new_view__';
    return;
  }

  let proj = null, view = null, figs = [];
  try {
    const [pr, vr, fr] = await Promise.all([
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId)),
      fetch(API_BASE + '/api/views/' + encodeURIComponent(viewId)),
      fetch(API_BASE + '/api/views/' + encodeURIComponent(viewId) + '/figures'),
    ]);
    if (pr.ok) proj = await pr.json();
    if (vr.ok) view = await vr.json();
    if (fr.ok) figs = (await fr.json()).figures || [];
  } catch (_e) {}

  // PIVOT: skip this intermediate workspace and drop straight into the
  // editor for the view's first figure -- the variant strip in the
  // editor sidebar already shows all the highlight variants.  If the
  // view has no figures yet, create a "Default" one so the editor has
  // something to load.  Auto-save means switching variants is safe.
  if (proj && view) {
    let target = figs[0];
    if (!target) {
      try {
        const r = await fetch(API_BASE + '/api/figures', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: 'Default variant',
            source_id: view.source_id,
            project_id: projId,
            view_id: viewId,
            camera: view.camera,
            configuration: view.configuration,
          }),
        });
        if (r.ok) {
          target = await r.json();
          await fetch(API_BASE + '/api/views/'
                        + encodeURIComponent(viewId)
                        + '/figures/' + encodeURIComponent(target.id),
                        { method: 'POST' });
        }
      } catch (_e) {}
    }
    if (target) {
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(target.id);
      return;
    }
  }

  if (!proj || !view) {
    container.appendChild(_topBar({
      crumbs: [{ label: 'Home', href: '#/' },
                { label: 'Not found' }],
    }));
    container.appendChild(h('div.app-main',
      h('p', 'Project or view not found.')));
    return;
  }

  AppState.currentProjectId = projId;
  container.appendChild(_topBar({
    crumbs: [
      { label: 'Home', href: '#/' },
      { label: proj.name, href: '#/project/' + encodeURIComponent(projId) },
      { label: view.name || 'View' },
    ],
    rightLinks: [
      { label: 'Settings', href: '#/settings' },
    ],
  }));
  const main = h('div.app-main');
  container.appendChild(main);

  // Big view preview at the top so the user sees the camera angle
  // they're working under.
  main.appendChild(h('div', { style: { marginBottom: '24px' } }, [
    h('img', {
      src: API_BASE + '/api/views/' + encodeURIComponent(viewId)
           + '/thumbnail?v=' + encodeURIComponent(view.updated_at || ''),
      style: { maxWidth: '480px', maxHeight: '280px',
                  objectFit: 'contain',
                  background: 'var(--c-surface-1)',
                  border: '1px solid var(--c-line)',
                  borderRadius: 'var(--radius-2)',
                  padding: '12px' },
      onerror: 'this.style.display=\"none\"'
    }),
  ]));

  main.appendChild(h('div.section-title', `Figures in this view (${figs.length})`));
  const grid = h('div.card-grid');

  const newCard = h('div.card.placeholder',
    [h('div', { style: { fontSize: '24px' } }, '+'),
     h('div', 'New figure')]);
  newCard.addEventListener('click', () =>
    _createFigureInView(projId, viewId, view));
  grid.appendChild(newCard);

  for (const fig of figs) {
    const card = h('div.card.figure-card');
    const thumb = h('img', {
      src: API_BASE + '/api/figures/' + encodeURIComponent(fig.id)
           + '/thumbnail?v=' + encodeURIComponent(fig.updated_at || ''),
      style: { width: '100%', height: '120px',
                  objectFit: 'contain',
                  background: 'var(--c-surface-1)',
                  borderRadius: 'var(--radius-1)',
                  marginBottom: '6px',
                  border: '1px solid var(--c-line)' },
    });
    thumb.onerror = () => {
      thumb.replaceWith(h('div', {
        style: { width: '100%', height: '120px',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    border: '1px dashed var(--c-line)',
                    marginBottom: '6px',
                    display: 'flex', alignItems: 'center',
                    justifyContent: 'center',
                    color: 'var(--c-text-muted)',
                    fontSize: '11px', fontStyle: 'italic' } },
        'no preview yet'));
    };
    card.appendChild(thumb);
    card.appendChild(h('div.card-title', fig.name || '(untitled)'));
    const n = (fig.selection || []).length;
    card.appendChild(h('div.card-meta', [
      h('span', n + (n === 1 ? ' part' : ' parts') + ' highlighted'),
    ]));
    card.addEventListener('click', () => {
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(fig.id);
    });
    _attachCardMenu(card, [
      { label: 'Rename...', onClick: () => _renameFigure(fig, projId) },
      { separator: true },
      { label: 'Delete figure...', danger: true,
         onClick: () => _deleteFigure(fig, projId) },
    ]);
    grid.appendChild(card);
  }
  main.appendChild(grid);
}

async function _createFigureInView(projId, viewId, view) {
  // Inherit camera + source from the view; user names the highlight
  // variant and lands in the editor immediately.
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Figure name'),
      (nameInput = h('input.input', {
        placeholder: 'e.g. "Step 1 — locate caster"',
        style: { width: '100%' },
      })),
    ]),
    h('p', { style: { fontSize: '12px', color: 'var(--c-text-muted)',
                          margin: '4px 0 0 0' } },
      "The figure inherits the view's camera.  Highlight parts and "
      + "pick a style in the editor."),
  ]);
  openModal({
    title: 'New figure',
    body,
    footer: [
      { label: 'Cancel', onClick: (close) => close() },
      { label: 'Create + open editor', primary: true,
         onClick: async (close) => {
        const name = (nameInput.value || '').trim();
        if (!name) { nameInput.focus(); return; }
        try {
          const r = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name,
              source_id: view.source_id,
              project_id: projId,
              view_id: viewId,
              camera: view.camera,
              configuration: view.configuration,
            }),
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const f = await r.json();
          // Attach to view
          await fetch(API_BASE + '/api/views/'
                        + encodeURIComponent(viewId)
                        + '/figures/' + encodeURIComponent(f.id),
                        { method: 'POST' });
          close();
          toast('Figure created', 'success');
          location.hash = '#/project/' + encodeURIComponent(projId)
                        + '/view/' + encodeURIComponent(viewId)
                        + '/figure/' + encodeURIComponent(f.id);
        } catch (e) {
          toast('Create failed: ' + (e.message || e), 'error');
        }
      } },
    ],
  });
  setTimeout(() => nameInput.focus(), 50);
}

registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)$/, ViewScreen);


// --- Card actions: rename / delete projects + figures ---------------

function _renameProject(p) {
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Project name'),
      (nameInput = h('input.input', { value: p.name || '',
                                          style: { width: '100%' } })),
    ]),
  ]);
  openModal({
    title: 'Rename project',
    body,
    footer: [
      { label: 'Cancel', onClick: (close) => close() },
      { label: 'Save', primary: true, onClick: async (close) => {
        const name = (nameInput.value || '').trim();
        if (!name) { nameInput.focus(); return; }
        try {
          await _saveProjectPatch(p.id, { ...p, name });
          close();
          toast('Project renamed', 'success');
          if (typeof window.IFU_APP?.renderRoute === 'function') {
            window.IFU_APP.renderRoute();
          }
        } catch (e) {
          toast('Rename failed: ' + (e.message || 'unknown'), 'error');
        }
      } },
    ],
  });
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 50);
}

function _editProjectDescription(p) {
  let descInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Description'),
      (descInput = h('textarea.input', {
        rows: 4, style: { width: '100%', resize: 'vertical' },
      }, p.description || '')),
    ]),
  ]);
  openModal({
    title: 'Edit description',
    body,
    footer: [
      { label: 'Cancel', onClick: (close) => close() },
      { label: 'Save', primary: true, onClick: async (close) => {
        const description = (descInput.value || '').trim();
        try {
          await _saveProjectPatch(p.id, { ...p, description });
          close();
          toast('Description updated', 'success');
          if (typeof window.IFU_APP?.renderRoute === 'function') {
            window.IFU_APP.renderRoute();
          }
        } catch (e) {
          toast('Update failed: ' + (e.message || 'unknown'), 'error');
        }
      } },
    ],
  });
  setTimeout(() => descInput.focus(), 50);
}

async function _saveProjectPatch(projId, patch) {
  const r = await fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId),
                          { method: 'PUT',
                             headers: { 'Content-Type': 'application/json' },
                             body: JSON.stringify(patch) });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}

async function _deleteProject(p) {
  const figcount = (p.figure_ids || []).length;
  const body = h('div', [
    h('p', { style: { marginTop: 0 } },
      'This will delete the project ',
      h('strong', p.name || '(untitled)'),
      figcount
        ? `, which contains ${figcount} figure${figcount === 1 ? '' : 's'}.`
        : '.'),
    figcount
      ? h('label', { style: { display: 'flex',
                                    alignItems: 'center',
                                    gap: '8px',
                                    marginTop: '12px' } }, [
          h('input', { type: 'checkbox', id: '_del_cascade',
                          checked: false }),
          h('span', `Also delete the ${figcount} figure${figcount === 1 ? '' : 's'}`),
        ])
      : null,
    h('p', { style: { color: 'var(--c-text-muted)',
                          fontSize: '12px', marginBottom: 0 } },
      'This action cannot be undone.'),
  ]);
  const ok = await new Promise((resolve) => {
    openModal({
      title: 'Delete project?',
      body,
      footer: [
        { label: 'Cancel', onClick: (close) => { close(); resolve(null); } },
        { label: 'Delete', danger: true, onClick: (close) => {
          const cb = document.getElementById('_del_cascade');
          close();
          resolve({ cascade: cb ? cb.checked : false });
        } },
      ],
    });
  });
  if (!ok) return;
  try {
    const q = ok.cascade ? '?cascade=1' : '';
    const r = await fetch(API_BASE + '/api/projects/' + encodeURIComponent(p.id) + q,
                            { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('Project deleted', 'success');
    if (typeof window.IFU_APP?.renderRoute === 'function') {
      window.IFU_APP.renderRoute();
    }
  } catch (e) {
    toast('Delete failed: ' + (e.message || 'unknown'), 'error');
  }
}

function _renameFigure(fig, projId) {
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Figure name'),
      (nameInput = h('input.input', { value: fig.name || '',
                                          style: { width: '100%' } })),
    ]),
  ]);
  openModal({
    title: 'Rename figure',
    body,
    footer: [
      { label: 'Cancel', onClick: (close) => close() },
      { label: 'Save', primary: true, onClick: async (close) => {
        const name = (nameInput.value || '').trim();
        if (!name) { nameInput.focus(); return; }
        try {
          const r = await fetch(API_BASE + '/api/figures/'
                                  + encodeURIComponent(fig.id),
                                  { method: 'PUT',
                                     headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify({ ...fig, name }) });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          close();
          toast('Figure renamed', 'success');
          if (typeof window.IFU_APP?.renderRoute === 'function') {
            window.IFU_APP.renderRoute();
          }
        } catch (e) {
          toast('Rename failed: ' + (e.message || 'unknown'), 'error');
        }
      } },
    ],
  });
  setTimeout(() => { nameInput.focus(); nameInput.select(); }, 50);
}

async function _deleteFigure(fig, projId) {
  const ok = await confirmModal({
    title: 'Delete figure?',
    body: h('div', [
      h('p', { style: { marginTop: 0 } },
        'Delete ', h('strong', fig.name || '(untitled)'), '?'),
      h('p', { style: { color: 'var(--c-text-muted)',
                            fontSize: '12px', marginBottom: 0 } },
        'This action cannot be undone.'),
    ]),
    confirmLabel: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch(API_BASE + '/api/figures/'
                            + encodeURIComponent(fig.id),
                            { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('Figure deleted', 'success');
    if (typeof window.IFU_APP?.renderRoute === 'function') {
      window.IFU_APP.renderRoute();
    }
  } catch (e) {
    toast('Delete failed: ' + (e.message || 'unknown'), 'error');
  }
}
// ===== end F.4 Project screen =====


// =====================================================================
// F.6 -- Settings screen
// =====================================================================
//
// App-level prefs (the figure-level styling controls live in the
// editor's right panel).  Reads from /api/settings, writes back on
// every change via PATCH.  Single-user, so no debounce needed.

async function SettingsScreen(container) {
  _ensureDesignStyles();
  container.className = 'app-shell';

  container.appendChild(_topBar({
    crumbs: [{ label: 'Home', href: '#/' }, { label: 'Settings' }],
  }));
  const mainEl = h('div.app-main');
  container.appendChild(mainEl);
  const container_orig = container;
  // Redirect subsequent appendChild calls in this function to mainEl
  container = mainEl;

  // Load current settings + source list
  let settings = {};
  let sources = [];
  try {
    const [sr, srcs] = await Promise.all([
      fetch(API_BASE + '/api/settings'),
      fetch(API_BASE + '/api/sources'),
    ]);
    if (sr.ok) settings = await sr.json();
    if (srcs.ok) sources = (await srcs.json()).sources || [];
  } catch (_e) {}
  AppState.settings = settings;

  // Generic row helper: a labelled control on one line
  function fieldRow(label, control) {
    return h('div', { style: { display: 'flex',
                                    alignItems: 'center',
                                    gap: '12px',
                                    marginBottom: '12px' } },
              [
                h('label', { style: { width: '220px',
                                          fontSize: '13px',
                                          color: '#71717a' } }, label),
                control,
              ]);
  }

  async function patchSettings(patch) {
    try {
      const r = await fetch(API_BASE + '/api/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      if (r.ok) {
        settings = await r.json();
        toast('Settings saved', 'success');
      } else {
        toast('Save failed: HTTP ' + r.status, 'error');
      }
    } catch (e) {
      toast('Save failed: ' + (e.message || e), 'error');
    }
  }

  // ---- General ----
  container.appendChild(h('div.section-title', 'General'));

  const detailSelect = h('select');
  for (const opt of ['coarse', 'normal', 'fine']) {
    const o = document.createElement('option');
    o.value = opt; o.textContent = opt;
    if ((settings.default_detail || 'normal') === opt) o.selected = true;
    detailSelect.appendChild(o);
  }
  detailSelect.addEventListener('change', () =>
    patchSettings({ default_detail: detailSelect.value }));
  container.appendChild(fieldRow('Default render detail', detailSelect));

  const strokeColor = h('input', { type: 'color',
    value: settings.default_stroke_color || '#00836a' });
  strokeColor.addEventListener('change', () =>
    patchSettings({ default_stroke_color: strokeColor.value }));
  container.appendChild(fieldRow('Default stroke colour', strokeColor));

  const strokeWidth = h('input', { type: 'number', step: '0.5',
    min: '0.5', max: '15',
    value: settings.default_stroke_width_mm ?? 3.0,
    style: { width: '70px' } });
  strokeWidth.addEventListener('change', () =>
    patchSettings({ default_stroke_width_mm: parseFloat(strokeWidth.value) }));
  container.appendChild(fieldRow('Default stroke width (mm)', strokeWidth));

  const fillColor = h('input', { type: 'color',
    value: settings.default_fill_color || '#cce6e0' });
  fillColor.addEventListener('change', () =>
    patchSettings({ default_fill_color: fillColor.value }));
  container.appendChild(fieldRow('Default fill colour', fillColor));

  const fillAlpha = h('input', { type: 'number', step: '0.05',
    min: '0', max: '1',
    value: settings.default_fill_alpha ?? 0.3,
    style: { width: '70px' } });
  fillAlpha.addEventListener('change', () =>
    patchSettings({ default_fill_alpha: parseFloat(fillAlpha.value) }));
  container.appendChild(fieldRow('Default fill alpha (0–1)', fillAlpha));

  // ---- Line styles ----
  container.appendChild(h('div.section-title', 'Line styles'));
  container.appendChild(h('div', { style: { fontSize: '13px',
      marginBottom: '12px', display: 'flex', alignItems: 'center',
      gap: '12px' } }, [
    h('span', { style: { color: '#71717a' } },
      'Create and edit line-style presets (stroke colour, weight, dash) '
      + 'with a live preview.'),
    h('a', { href: '#/settings/styles', style: { color: 'var(--accora-teal,'
      + '#00836a)', fontWeight: '500' } }, 'Edit line styles \\u2192'),
  ]));

  // ---- Sources ----
  container.appendChild(h('div.section-title', 'Sources'));
  const srcList = h('div', { style: { marginBottom: '24px' } });
  if (!sources.length) {
    srcList.appendChild(h('div.empty', 'No sources configured.'));
  } else {
    for (const s of sources) {
      srcList.appendChild(h('div', { style: { marginBottom: '8px',
                                                   fontSize: '13px' } },
        [
          h('strong', s.label),
          ' (' + s.id + ') ',
          s.onshape_ids
            ? h('span', { style: { color: '#0a8' } }, 'Onshape')
            : h('span', { style: { color: '#71717a' } }, 'local STEP'),
        ]));
    }
  }
  container.appendChild(srcList);

  // ---- Imported sources (Onshape-origin, deletable) -----------------
  // Static demos live in ifu/config.SOURCES and can't be deleted from
  // the UI -- only the user's own imported documents land here.
  const imported = (sources || []).filter(s => s.origin === 'dynamic');
  container.appendChild(h('div.section-title', 'Imported sources'));
  if (!imported.length) {
    container.appendChild(h('div',
      { style: { color: '#71717a', fontSize: '13px',
                     marginBottom: '24px' } },
      'No Onshape documents imported yet. Use \\u201c+ New project '
      + '\\u2192 Import from Onshape\\u201d to add one.'));
  } else {
    const importList = h('div', { style: { marginBottom: '24px',
                                                   display: 'flex',
                                                   flexDirection: 'column',
                                                   gap: '6px' } });
    for (const s of imported) {
      const row = h('div', { style: {
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: '12px',
                                  padding: '8px 12px',
                                  border: '1px solid var(--c-line)',
                                  borderRadius: 'var(--radius-1)',
                                  fontSize: '13px',
                              } }, [
        h('div', { style: { flex: 1 } }, [
          h('div', h('strong', s.label || s.id)),
          h('div', { style: { color: '#71717a', fontSize: '11px' } },
            s.id),
          s.imported_from
            ? h('div', { style: { color: '#a1a1aa', fontSize: '11px',
                                         maxWidth: '420px',
                                         whiteSpace: 'nowrap',
                                         overflow: 'hidden',
                                         textOverflow: 'ellipsis' } },
                s.imported_from)
            : '',
        ]),
        Object.assign(h('button',
          { style: { padding: '4px 10px', fontSize: '12px',
                          border: '1px solid #c44', color: '#c44',
                          background: '#fff', cursor: 'pointer',
                          borderRadius: '4px' } },
          'Delete'),
          {
            onclick: async () => {
              if (!confirm(`Delete imported source \\u201c${s.label || s.id}\\u201d?  `
                          + 'This removes the STEP file and the source '
                          + 'record. Projects that reference it will be '
                          + 'left dangling.')) return;
              try {
                const r = await fetch(API_BASE + '/api/sources/'
                                        + encodeURIComponent(s.id),
                                        { method: 'DELETE' });
                if (!r.ok) {
                  const j = await r.json().catch(() => ({}));
                  throw new Error(j.error || ('HTTP ' + r.status));
                }
                toast('Source deleted', 'success');
                renderRoute();   // re-mount Settings with fresh list
              } catch (e) {
                toast('Delete failed: ' + (e.message || e), 'error');
              }
            },
          }),
      ]);
      importList.appendChild(row);
    }
    container.appendChild(importList);
  }

  // ---- Storage ----
  container.appendChild(h('div.section-title', 'Storage'));
  container.appendChild(h('div', { style: { fontSize: '13px',
                                                marginBottom: '24px' } },
    [
      h('strong', 'Projects folder: '),
      h('code', settings.projects_dir || '?'),
    ]));

  // ---- Reset ----
  container.appendChild(h('div.section-title', 'Danger zone'));
  const resetBtn = h('button',
    { style: { padding: '8px 12px', fontSize: '13px',
                  border: '1px solid #c44', color: '#c44',
                  background: '#fff', cursor: 'pointer',
                  borderRadius: '4px' } },
    'Reset to defaults');
  resetBtn.addEventListener('click', async () => {
    if (!confirm('Reset ALL app settings to defaults?  '
                + 'Per-figure and per-project state is untouched.')) return;
    await fetch(API_BASE + '/api/settings/reset', { method: 'POST' });
    renderRoute();   // re-mount this screen with fresh values
  });
  container.appendChild(resetBtn);
}

registerRoute(/^#\/settings$/, SettingsScreen);
// ===== end F.6 Settings screen =====


// =====================================================================
// Line-style presets editor (#/settings/styles)
// Lists built-in + user presets as cards with a live preview swatch.
// User presets get per-edge-category controls (stroke colour / width /
// dash); built-ins are read-only but can be duplicated to edit.  Backs
// onto /api/presets (see ifu/presets.py).
// =====================================================================
const _STYLE_CATS = [
  ['outline_v',      'Silhouette'],
  ['sharp_v',        'Sharp edges'],
  ['smooth_v',       'Smooth / tangent'],
  ['hidden_sharp',   'Hidden sharp'],
  ['hidden_outline', 'Hidden outline'],
];

async function StylesSettingsScreen(container) {
  _ensureDesignStyles();
  container.className = 'app-shell';
  container.appendChild(_topBar({
    crumbs: [{ label: 'Home', href: '#/' },
             { label: 'Settings', href: '#/settings' },
             { label: 'Line styles' }],
  }));
  const mainEl = h('div.app-main');
  container.appendChild(mainEl);

  let data = { presets: [], default_id: null };
  try {
    const r = await fetch(API_BASE + '/api/presets');
    if (r.ok) data = await r.json();
  } catch (_e) {}

  mainEl.appendChild(h('div.section-title', 'Line-style presets'));
  mainEl.appendChild(h('p', { style: { color: '#71717a', fontSize: '13px',
      maxWidth: '640px', marginTop: '-4px' } },
    'Presets control the stroke colour, weight (mm) and dash of each edge '
    + 'category in the 2D line-art. Pick one in the figure tools panel; '
    + 'edit or create your own here. Built-in presets are read-only — '
    + 'duplicate one to make it yours.'));

  const grid = h('div', { style: { display: 'flex', flexWrap: 'wrap',
      gap: '16px', marginTop: '12px' } });
  mainEl.appendChild(grid);

  function previewImg(id) {
    return h('img', { src: API_BASE + '/api/presets/' + encodeURIComponent(id)
      + '/preview.svg?t=' + Date.now(),
      style: { width: '160px', height: '120px', objectFit: 'contain',
               border: '1px solid var(--c-line, #ececec)', borderRadius: '6px',
               background: '#fff' } });
  }

  // Debounced PATCH that refreshes the card's swatch after save.
  function makeSaver(id, getImg) {
    let timer = null;
    return (styles) => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          const r = await fetch(API_BASE + '/api/presets/'
            + encodeURIComponent(id), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ styles }),
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const img = getImg();
          if (img) img.src = API_BASE + '/api/presets/'
            + encodeURIComponent(id) + '/preview.svg?t=' + Date.now();
          window.IFU_ANNOT?.reloadPresets?.(id);
          toast('Style saved', 'success');
        } catch (e) { toast('Save failed: ' + (e.message || e), 'error'); }
      }, 350);
    };
  }

  function card(preset) {
    const isBuiltin = !!preset.builtin;
    const imgEl = previewImg(preset.id);
    const save = makeSaver(preset.id, () => imgEl);
    const styles = JSON.parse(JSON.stringify(preset.styles || {}));

    const controls = h('div', { style: { display: 'flex',
        flexDirection: 'column', gap: '6px', flex: 1, minWidth: '260px' } });
    for (const [cat, label] of _STYLE_CATS) {
      const st = styles[cat] || { stroke: '#000000', width: 0.3, dash: null };
      styles[cat] = st;
      const color = h('input', { type: 'color', value: st.stroke || '#000000',
        disabled: isBuiltin, style: { width: '34px', height: '26px',
        padding: '0', border: '1px solid #d4d4d8', borderRadius: '4px' } });
      const width = h('input', { type: 'number', step: '0.05', min: '0.05',
        max: '5', value: st.width ?? 0.3, disabled: isBuiltin,
        style: { width: '64px' } });
      const dash = h('input', { type: 'text', value: st.dash || '',
        placeholder: 'solid', disabled: isBuiltin,
        style: { width: '70px' } });
      const onChange = () => {
        st.stroke = color.value;
        st.width = parseFloat(width.value) || 0.3;
        st.dash = dash.value.trim() || null;
        save(styles);
      };
      color.addEventListener('change', onChange);
      width.addEventListener('change', onChange);
      dash.addEventListener('change', onChange);
      controls.appendChild(h('div', { style: { display: 'flex',
          alignItems: 'center', gap: '8px', fontSize: '12px' } }, [
        h('span', { style: { width: '110px', color: '#52525b' } }, label),
        color, width,
        h('span', { style: { color: '#a1a1aa', fontSize: '11px' } }, 'mm'),
        dash,
      ]));
    }

    const actions = h('div', { style: { display: 'flex', gap: '8px',
        marginTop: '8px' } });
    const dupBtn = h('button.btn', { style: { padding: '4px 10px',
        fontSize: '12px', cursor: 'pointer' } }, 'Duplicate');
    dupBtn.addEventListener('click', async () => {
      try {
        const r = await fetch(API_BASE + '/api/presets', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: preset.name + ' copy',
            styles: preset.styles }) });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        window.IFU_ANNOT?.reloadPresets?.();
        renderRoute();
      } catch (e) { toast('Duplicate failed: ' + (e.message || e), 'error'); }
    });
    actions.appendChild(dupBtn);
    if (!isBuiltin) {
      const delBtn = h('button', { style: { padding: '4px 10px',
          fontSize: '12px', border: '1px solid #c44', color: '#c44',
          background: '#fff', cursor: 'pointer', borderRadius: '4px' } },
        'Delete');
      delBtn.addEventListener('click', async () => {
        if (!confirm('Delete preset "' + preset.name + '"?')) return;
        try {
          const r = await fetch(API_BASE + '/api/presets/'
            + encodeURIComponent(preset.id), { method: 'DELETE' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          window.IFU_ANNOT?.reloadPresets?.();
          renderRoute();
        } catch (e) { toast('Delete failed: ' + (e.message || e), 'error'); }
      });
      actions.appendChild(delBtn);
    }

    return h('div', { style: { border: '1px solid var(--c-line, #e4e4e7)',
        borderRadius: '8px', padding: '14px', width: '480px',
        display: 'flex', flexDirection: 'column', gap: '10px',
        background: '#fff' } }, [
      h('div', { style: { display: 'flex', alignItems: 'center',
          justifyContent: 'space-between' } }, [
        h('strong', preset.name),
        isBuiltin ? h('span', { style: { fontSize: '11px', color: '#a1a1aa' } },
          'built-in') : h('span', { style: { fontSize: '11px',
          color: '#0a8' } }, 'custom'),
      ]),
      h('div', { style: { display: 'flex', gap: '14px',
          alignItems: 'flex-start' } }, [imgEl, controls]),
      actions,
    ]);
  }

  for (const p of data.presets) grid.appendChild(card(p));

  // New blank preset
  const newBtn = h('button.btn.primary', { style: { marginTop: '16px',
      padding: '8px 14px', cursor: 'pointer' } }, '+ New preset');
  newBtn.addEventListener('click', async () => {
    const name = prompt('Name for the new preset?', 'My style');
    if (!name) return;
    try {
      const r = await fetch(API_BASE + '/api/presets', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      window.IFU_ANNOT?.reloadPresets?.();
      renderRoute();
    } catch (e) { toast('Create failed: ' + (e.message || e), 'error'); }
  });
  mainEl.appendChild(newBtn);
}

registerRoute(/^#\/settings\/styles$/, StylesSettingsScreen);


// =====================================================================
// F.5 -- Editor route + breadcrumb on the legacy editor
// =====================================================================
//
// '#/project/<pid>/figure/<fid>' opens the legacy editor and auto-loads
// the figure on top.  A breadcrumb appears above the legacy header so
// you can navigate back to Home / Project without using the URL bar.
//
// The legacy editor's chrome itself is not re-skinned in F.5 -- that's
// a bigger reorganisation deferred to a later phase.  This is the
// minimal wiring to make the editor first-class within the new route
// shape.

const _CRUMB_ID = 'editor-breadcrumb';
const _CRUMB_CSS = `
#${_CRUMB_ID} {
  display: flex; gap: 8px; align-items: center;
  padding: 8px 16px; background: #f4f4f5; font-size: 13px;
  border-bottom: 1px solid #d4d4d8;
}
#${_CRUMB_ID} a { color: #71717a; text-decoration: none; }
#${_CRUMB_ID} a:hover { color: #00836a; text-decoration: underline; }
#${_CRUMB_ID} .sep { color: #d4d4d8; }
#${_CRUMB_ID} .current { color: #18181b; font-weight: 600; }
`;

function _ensureCrumbStyles() {
  if (document.getElementById('editor-crumb-styles')) return;
  const s = document.createElement('style');
  s.id = 'editor-crumb-styles';
  s.textContent = _CRUMB_CSS;
  document.head.appendChild(s);
}

function _removeCrumb() {
  document.getElementById(_CRUMB_ID)?.remove();
}

function _installCrumb(parts) {
  _ensureCrumbStyles();
  _removeCrumb();
  const crumb = h('div', { id: _CRUMB_ID });
  parts.forEach((p, i) => {
    if (i > 0) crumb.appendChild(h('span.sep', '/'));
    if (p.href) crumb.appendChild(h('a', { href: p.href }, p.label));
    else crumb.appendChild(h('span.current', p.label));
  });
  document.body.insertBefore(crumb, document.body.firstChild);
}

async function EditorScreen(container, params) {
  // We don't render into `container` -- the LEGACY editor is what we
  // want visible.  We unhide it, install a breadcrumb, then load
  // the figure on top of it.
  try {
    (window._reportClientError || function(){})({
      level: 'info', op: 'editor.enter',
      msg: 'params=' + JSON.stringify(params || []).slice(0, 200),
    });
  } catch (_e) {}
  container.style.display = 'none';
  const header = document.querySelector('header');
  const main = document.querySelector('main');
  if (header) header.style.display = '';
  if (main) main.style.display = '';

  const projId = params[0];
  const figId = params[1];
  const opts = params[2] || {};
  const viewIdFromRoute = opts.viewId || null;

  // Fetch both in parallel so we know the project name for the crumb
  let proj = null, fig = null;
  try {
    const [pr, fr] = await Promise.all([
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId)),
      fetch(API_BASE + '/api/figures/' + encodeURIComponent(figId)),
    ]);
    if (pr.ok) proj = await pr.json();
    if (fr.ok) fig = await fr.json();
  } catch (e) {
    (window._reportClientError || function(){})({
      level: 'err', op: 'editor.fetch',
      msg: String(e && e.message || e),
    });
  }
  try {
    (window._reportClientError || function(){})({
      level: 'info', op: 'editor.fetched',
      msg: 'proj=' + !!proj + ' fig=' + !!fig
            + ' projId=' + projId + ' figId=' + figId,
    });
  } catch (_e) {}

  // If no view id came in the route but the figure has one, use that
  // -- the variant strip needs the view id to know which figures are
  // siblings.
  const viewId = viewIdFromRoute || fig?.view_id || null;
  AppState.currentViewId = viewId;

  _installCrumb([
    { label: 'Home', href: '#/' },
    { label: proj?.name || '(unknown project)',
       href: '#/project/' + encodeURIComponent(projId) },
    { label: fig?.name || '(unknown figure)' },
  ]);

  if (fig) {
    AppState.currentProjectId = projId;
    AppState.currentFigureId = figId;
    // Yield a tick so the legacy editor's catalogue is fully ready,
    // then drop the figure in.  Skip the "replace current work?"
    // confirm: the user JUST clicked into this figure via the
    // workspace -- their intent isn't ambiguous.  autoGenerate fires
    // /api/render with the figure's camera so the 2D base view
    // appears without the user having to click "generate 2D" -- the
    // intended UX for a "subview with different highlighting".
    setTimeout(() => {
      try { window._loadFigureIntoEditor(fig, {
        skipConfirm: true,
        autoGenerate: !!(fig.camera && fig.camera.eye && fig.camera.target),
      }); }
      catch (e) {
        // DON'T swallow silently -- report so the user / smoke test
        // can see why the auto-render path bailed.
        (window._reportClientError || function(){})({
          level: 'err', op: 'editor.loadFigure',
          msg: String((e && e.message) || e),
          stack: (e && e.stack || '').slice(0, 600),
        });
        console.error('[editor] _loadFigureIntoEditor threw:', e);
      }
      // Bind the legacy sidebar's project filter to THIS project so
      // the figures list only shows figures in this project, not
      // every figure ever made.  Defer one more tick so the project
      // selector has finished populating from /api/projects.
      setTimeout(() => {
        const pSel = document.getElementById('project-sel');
        if (pSel && projId) {
          // Make sure the option exists (it should, but defensively
          // add it if /api/projects hasn't returned yet).
          if (!Array.from(pSel.options).some(o => o.value === projId)) {
            const opt = document.createElement('option');
            opt.value = projId;
            opt.textContent = proj?.name || projId;
            pSel.appendChild(opt);
          }
          pSel.value = projId;
          pSel.dispatchEvent(new Event('change'));
        }
        // Pre-fill the figure-name input + retitle the save button
        // so the user knows hitting save UPDATES this figure, not
        // creates a duplicate.  Reveal the secondary "save as new"
        // action for explicit forking.
        const fn = document.getElementById('fig-name');
        const sb = document.getElementById('btn-fig-save');
        const sa = document.getElementById('btn-fig-save-as');
        if (fn && fig) {
          fn.value = fig.name || '';
          fn.placeholder = 'rename to save under different name';
        }
        if (sb) {
          sb.textContent = 'save';
          sb.title = 'Update "' + (fig?.name || 'this figure')
                      + '" with the current camera, selection, styles';
        }
        if (sa) sa.style.display = '';
        // Capture the figure's loaded state as the dirty-tracking
        // baseline.  Indicator polls every 1s.
        if (window._markLoadedFigureBaseline) {
          window._markLoadedFigureBaseline();
        }
        // Render the variant strip if this figure is under a view
        if (viewId && typeof _renderVariantStrip === 'function') {
          _renderVariantStrip(projId, viewId, figId);
        }
        // Inject a "back to project" pill in the legacy header so
        // there's an obvious exit.  Sits right after the logo.
        const hdr = document.querySelector('header');
        if (hdr && !hdr.querySelector('.back-to-project')) {
          const pill = document.createElement('a');
          pill.className = 'back-to-project';
          pill.href = '#/project/' + encodeURIComponent(projId);
          pill.title = 'Return to ' + (proj?.name || 'project') + ' workspace';
          pill.innerHTML = '<span style="font-size:13px;">←</span> '
                            + (proj?.name || 'Project');
          const h1 = hdr.querySelector('h1')?.parentElement;
          if (h1) h1.insertAdjacentElement('afterend', pill);
          else hdr.insertBefore(pill, hdr.firstChild);
        }
      }, 100);
    }, 200);
  }

  // Hide legacy sidebar sections that just add noise inside a
  // project (Saved views legacy / Onshape tree / STEP-order parts
  // list aren't useful for a project-bound figure).  CSS hook lives
  // in the design system so the editor stays uncluttered.
  document.body.classList.add('project-scoped-editor');

  // Teardown: remove the breadcrumb + restore the global view when
  // the user navigates away.  Made async so we can flush any pending
  // auto-save BEFORE clearing currentFigureId -- otherwise a fast
  // variant click can lose the previous variant's last edit.
  return async () => {
    // Flush first, while AppState.currentFigureId is still valid.
    if (typeof window._flushAutoSave === 'function') {
      try { await window._flushAutoSave(); } catch (_e) {}
    }
    _removeCrumb();
    document.body.classList.remove('project-scoped-editor');
    // Restore the original save button + hide save-as-new
    const fn = document.getElementById('fig-name');
    const sb = document.getElementById('btn-fig-save');
    const sa = document.getElementById('btn-fig-save-as');
    if (fn) { fn.value = ''; fn.placeholder = 'figure name...'; }
    if (sb) {
      sb.textContent = 'save';
      sb.title = 'Capture current state as a new figure';
    }
    if (sa) sa.style.display = 'none';
    if (typeof AppState !== 'undefined') {
      AppState.currentFigureId = null;
      AppState.currentViewId = null;
    }
    // Clear the variant strip
    const stripEl = document.getElementById('variants-strip');
    if (stripEl) stripEl.innerHTML = '';
    // Pull the back-to-project pill so non-project routes don't
    // inherit a stale exit
    document.querySelectorAll('header .back-to-project')
            .forEach(el => el.remove());
  };
}

registerRoute(/^#\/project\/([^/]+)\/figure\/([^/]+)$/, EditorScreen);
// View-aware editor route -- accept the same EditorScreen.  The route
// handler reads the view id from params[1] when present so the editor
// can later use the view's camera + configuration.  For now params are
// (projId, viewId, figId) when this matches.
registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)\/figure\/([^/]+)$/,
              (container, params) => EditorScreen(container,
                [params[0], params[2], { viewId: params[1] }]));

// Update the Project screen's figure-card click to route into the
// editor instead of falling back to legacy.  We do this by replacing
// ProjectScreen with a slightly fuller version.
const _OrigProjectScreen = ProjectScreen;
ProjectScreen = async function(container, params) {
  await _OrigProjectScreen(container, params);
  // After mount, rebind each figure card click to navigate properly.
  const projId = params[0];
  const cards = container.querySelectorAll('.grid .card:not(.placeholder)');
  // We need the actual figure ids -- refetch them in order.
  let figs = [];
  try {
    const r = await fetch(API_BASE + '/api/projects/'
                            + encodeURIComponent(projId) + '/figures');
    if (r.ok) figs = (await r.json()).figures || [];
  } catch (_e) {}
  cards.forEach((card, i) => {
    if (i >= figs.length) return;
    const fid = figs[i].id;
    // Replace existing click handler by cloning the node (drops listeners)
    const clone = card.cloneNode(true);
    card.parentNode.replaceChild(clone, card);
    clone.addEventListener('click', () => {
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/figure/' + encodeURIComponent(fid);
    });
  });
};
// re-register so the wrapped version wins.  IMPORTANT: keep the
// Phase-3 view routes here too -- the original list was clobbered.
_routes.length = 0;
registerRoute(/^#\/$/, HomeScreen);
registerRoute(/^#\/project\/([^/]+)$/, ProjectScreen);
registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)$/, ViewScreen);
registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)\/figure\/([^/]+)$/,
              (container, params) => EditorScreen(container,
                [params[0], params[2], { viewId: params[1] }]));
registerRoute(/^#\/project\/([^/]+)\/figure\/([^/]+)$/, EditorScreen);
registerRoute(/^#\/settings\/styles$/, StylesSettingsScreen);
registerRoute(/^#\/settings$/, SettingsScreen);

// Fire the router on first load.  Wait for `load` (not just DOM
// parse) so the `type="module"` script at the bottom has executed
// and set up window.generateLiveSVGForCamera, window.IFU_VIEWER, etc.
// Without this defer, EditorScreen's auto-render path runs while
// generateLiveSVGForCamera is still undefined and silently bails.
// Found via tests/smoke_figure_render.py.
if (document.readyState === 'complete') {
  renderRoute();
} else {
  window.addEventListener('load', () => { renderRoute(); }, { once: true });
}
// ===== end F.5 Editor route =====


const canvasWrap = $('canvas-wrap');
const fileSel = $('file-sel');
const viewSel = $('view-sel');
const partList = $('part-list');
const selectionInfo = $('selection-info');
const tooltip = $('tooltip');
const calloutCount = $('callout-count');

// state per (file,view): pan/zoom/highlights(Set)/annotations
const state = {};

function paneKey(f, v) { return f + '/' + v; }
function getState(f, v) {
  const k = paneKey(f, v);
  if (!state[k]) state[k] = {
    tx: 0, ty: 0, scale: 1, highlights: new Set(), annotations: []
  };
  return state[k];
}

// Up-axis override table: maps a "what axis is up in the model" choice
// to the rotation that brings that axis onto world Z (our pipeline's
// canonical up). The 3D viewer applies this live; the Python side
// reads the same tuple from SOURCES (pre_rotate) and bakes it into HLR.
const UP_AXIS_ROT = {
  'Z':  { axis: [0,0,1], angle:    0 },   // identity
  'Y':  { axis: [1,0,0], angle:   90 },   // Y -> Z
  'X':  { axis: [0,1,0], angle:  -90 },   // X -> Z
  '-Z': { axis: [1,0,0], angle:  180 },   // -Z -> Z
  '-Y': { axis: [1,0,0], angle:  -90 },   // -Y -> Z
  '-X': { axis: [0,1,0], angle:   90 },   // -X -> Z
};

const upAxisSel = $('up-axis-sel');
function _upAxisKey(fid) { return 'upAxis_' + fid; }
function loadUpAxisFor(fid) {
  const v = localStorage.getItem(_upAxisKey(fid)) || 'Z';
  upAxisSel.value = v;
  return v;
}
upAxisSel.addEventListener('change', () => {
  localStorage.setItem(_upAxisKey(fileSel.value), upAxisSel.value);
  window.IFU_VIEWER?.applyUpAxisOverride?.(UP_AXIS_ROT[upAxisSel.value]);
  // Drop the existing Live SVG -- it was rendered against the old
  // orientation and would be misleading next to the freshly-rotated 3D.
  invalidateLiveView(fileSel.value);
});

// Remove any cached "Live (from 3D)" view for a source.  Called whenever
// upstream state changes (Up: override, source switch) that would make
// the previously-generated SVG stale relative to the current 3D pane.
function invalidateLiveView(file_id) {
  const fe = CATALOGUE.find(x => x.file_id === file_id);
  if (!fe) return;
  const had = fe.views.some(v => v.view_id === '__live__');
  fe.views = fe.views.filter(v => v.view_id !== '__live__');
  document
    .querySelectorAll(`.svg-pane[data-file="${file_id}"][data-view="__live__"]`)
    .forEach((p) => p.remove());
  if (!had) return;
  if (fileSel.value === file_id) {
    const wasLive = viewSel.value === '__live__';
    refreshViews();
    if (wasLive) {
      viewSel.value = fe.views[0]?.view_id || 'iso';
      refreshPane();
    }
  }
}
$('btn-copy-orient').addEventListener('click', () => {
  const r = UP_AXIS_ROT[upAxisSel.value];
  const line = (r.angle === 0)
    ? 'None,  # no pre_rotation needed'
    : `((${r.axis.join(', ')}), ${r.angle}),`;
  navigator.clipboard?.writeText(line);
  const btn = $('btn-copy-orient');
  const orig = btn.textContent;
  btn.textContent = 'copied!';
  setTimeout(() => { btn.textContent = orig; }, 1500);
});

// Populate selectors
CATALOGUE.forEach(fe => {
  const opt = document.createElement('option');
  opt.value = fe.file_id; opt.textContent = fe.file_label;
  fileSel.appendChild(opt);
});
// Fallback view list for sources that aren't in the baked CATALOGUE
// (e.g. Onshape imports landed at runtime).  Same iso / front / side
// presets used by the standard sources, with the same view directions
// the build pipeline uses.  No baked SVG -- live /api/render fills in.
const _FALLBACK_VIEWS = [
  { view_id: 'iso',   label: 'Iso 3/4 (front-right-above)',
     view_dir: [-0.5, -1.0, 0.7] },
  { view_id: 'front', label: 'Front elevation',
     view_dir: [ 0.0, -1.0, 0.25] },
  { view_id: 'side',  label: 'Side elevation',
     view_dir: [-1.0,  0.0, 0.25] },
];

function refreshViews() {
  viewSel.innerHTML = '';
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  const views = (fe && fe.views && fe.views.length)
                ? fe.views : _FALLBACK_VIEWS;
  views.forEach(ve => {
    const o = document.createElement('option');
    o.value = ve.view_id; o.textContent = ve.label;
    viewSel.appendChild(o);
  });
}
fileSel.addEventListener('change', () => {
  refreshViews(); refreshPane();
  const upStored = loadUpAxisFor(fileSel.value);
  window.IFU_VIEWER?.applyUpAxisOverride?.(UP_AXIS_ROT[upStored]);
});
viewSel.addEventListener('change', refreshPane);
// P1.a: when the View dropdown changes (Iso / Front / Side / Live / saved),
// snap the 3D camera to match that view direction.  This is the cheap
// "make 3D match what I'm looking at in 2D" workflow Composer uses --
// no separate "view in 3D" button needed.  Skip when the view has no
// usable view_dir (e.g. a placeholder entry).
function snap3DToCurrentView() {
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  const ve = fe?.views.find(v => v.view_id === viewSel.value);
  const vd = ve?.view_dir;
  if (!vd || vd.length !== 3) return;
  // Pick eye = focal + view_dir * dist; distance is fitted properly
  // inside snapCameraTo via the ortho bounds re-fit.
  const len = Math.hypot(vd[0], vd[1], vd[2]) || 1;
  const dist = 4000;     // generous; ortho fit will re-tune frustum
  const eye = [vd[0] / len * dist, vd[1] / len * dist, vd[2] / len * dist];
  const target = [0, 0, 0];
  window.IFU_VIEWER?.snapCameraTo?.(eye, target);
}
viewSel.addEventListener('change', snap3DToCurrentView);
refreshViews();

function activePane() {
  return document.querySelector(
    `.svg-pane[data-file="${fileSel.value}"][data-view="${viewSel.value}"]`);
}
function activeSvg() { return activePane()?.querySelector('svg'); }

function refreshPartList() {
  partList.innerHTML = '';
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  // Dynamic Onshape sources don't have a baked parts list -- show
  // a placeholder rather than crashing.
  if (!fe || !fe.parts || !fe.parts.length) {
    const li = document.createElement('li');
    li.style.cssText = 'color:var(--muted);font-style:italic;padding:4px 0;';
    li.textContent = '(no parts list for live source)';
    partList.appendChild(li);
    return;
  }
  fe.parts.forEach(p => {
    const li = document.createElement('li');
    li.textContent = `[${String(p.idx).padStart(3, '0')}] ${p.label}`;
    li.dataset.part = p.idx;
    li.addEventListener('click', (ev) =>
      togglePartHighlight(p.idx, {append: ev.ctrlKey || ev.metaKey}));
    partList.appendChild(li);
  });
}

// Multi-select highlight: state.highlights is a Set of part idx.
//   - plain click   = replace selection with just this part
//                     (or clear if it was already the only one selected)
//   - Ctrl/Cmd-click = toggle this part in/out of the current selection
//   - Esc            = clear all
function togglePartHighlight(idx, opts) {
  opts = opts || {};
  const append = !!opts.append;
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights) st.highlights = new Set();
  const before = [...st.highlights].sort((a,b)=>a-b).join(',');
  if (append) {
    if (st.highlights.has(idx)) st.highlights.delete(idx);
    else st.highlights.add(idx);
  } else {
    if (st.highlights.size === 1 && st.highlights.has(idx)) {
      st.highlights.clear();
    } else {
      st.highlights.clear();
      st.highlights.add(idx);
    }
  }
  const after = [...st.highlights].sort((a,b)=>a-b).join(',');
  if (window._track) {
    window._track('toggle',
      'idx=' + idx + (append ? ' +ctrl' : '')
      + ' before=[' + before + '] after=[' + after + ']');
  }
  applyHighlights();
}

function clearHighlights() {
  const st = getState(fileSel.value, viewSel.value);
  if (st.highlights) st.highlights.clear();
  applyHighlights();
}

let _lastSilHighlightSig = '';
// Lightweight perf HUD: floats top-right, shows last-call durations
// for the hot paths.  Add ?dbg=1 to URL to enable, or set window._DBG = true.
const _DBG_ON = (new URLSearchParams(location.search)).get('dbg') === '1';
let _dbgEl = null;
function _dbgLine(label, ms, extra) {
  if (!_DBG_ON) return;
  if (!_dbgEl) {
    _dbgEl = document.createElement('div');
    _dbgEl.id = '_dbg_hud';
    _dbgEl.style.cssText = 'position:fixed;top:8px;right:8px;z-index:99999;'
      + 'background:rgba(0,0,0,.82);color:#0f0;font:11px/1.4 ui-monospace,Consolas;'
      + 'padding:6px 9px;border-radius:6px;pointer-events:none;'
      + 'white-space:pre;max-width:340px';
    document.body.appendChild(_dbgEl);
    _dbgEl._lines = {};
  }
  _dbgEl._lines[label] = `${label.padEnd(22)} ${ms.toFixed(1).padStart(7)}ms${extra?'  '+extra:''}`;
  _dbgEl.textContent = Object.values(_dbgEl._lines).join('\n');
}
function _dbgTime(label, fn, extra) {
  if (!_DBG_ON) return fn();
  const t0 = performance.now();
  try { return fn(); }
  finally { _dbgLine(label, performance.now() - t0, extra); }
}

// ===== Interaction tracker overlay =================================
// A floating panel that records every click + selection state change
// so the user (and us) can SEE exactly what's happening when something
// looks wrong.  Toggle with the 'track' button in the header.  Lines
// are appended chronologically; the panel keeps the last ~200 events.
//
//   click   part_001  layer=outline_v  sel=2 (3,5)
//   apply   highlight 6 parts dim 71 parts
//   svg     parts=77  layers=outline_v,sharp_v,smooth_v
//
let _trackerEl = null;
let _trackerBody = null;
let _trackerOpen = false;
let _trackerEntries = [];
const _TRACKER_MAX = 200;
function _ensureTrackerUI() {
  if (_trackerEl) return;
  _trackerEl = document.createElement('div');
  _trackerEl.id = '_iact_track';
  _trackerEl.style.cssText =
      'position:fixed;top:60px;right:8px;z-index:99996;'
    + 'width:380px;max-height:380px;display:none;'
    + 'background:rgba(15,15,17,.94);color:#d4d4d8;'
    + 'border:1px solid #3f3f46;border-radius:6px;'
    + 'font:11px/1.4 ui-monospace,Consolas,monospace;'
    + 'box-shadow:0 4px 16px rgba(0,0,0,.4);'
    + 'flex-direction:column;';
  const head = document.createElement('div');
  head.style.cssText =
      'padding:6px 10px;border-bottom:1px solid #3f3f46;'
    + 'display:flex;align-items:center;justify-content:space-between;'
    + 'background:#27272a;font-weight:600;color:#fafafa;';
  const title = document.createElement('span');
  title.textContent = 'Interaction log';
  const right = document.createElement('span');
  right.style.cssText = 'display:flex;gap:6px;';
  const copyBtn = document.createElement('button');
  copyBtn.textContent = 'copy';
  copyBtn.style.cssText =
      'background:transparent;border:1px solid #52525b;color:#d4d4d8;'
    + 'border-radius:3px;padding:1px 6px;font-size:10px;cursor:pointer;';
  copyBtn.addEventListener('click', async () => {
    const text = _trackerEntries.map(e =>
      e.t.toFixed(2).padStart(7) + '  ' + e.kind.padEnd(6) + ' ' + e.msg
    ).join('\\n');
    try {
      await navigator.clipboard.writeText(text);
      copyBtn.textContent = 'copied!';
      setTimeout(() => { copyBtn.textContent = 'copy'; }, 1500);
    } catch (_e) {
      copyBtn.textContent = '(blocked)';
    }
  });
  const clearBtn = document.createElement('button');
  clearBtn.textContent = 'clear';
  clearBtn.style.cssText =
      'background:transparent;border:1px solid #52525b;color:#d4d4d8;'
    + 'border-radius:3px;padding:1px 6px;font-size:10px;cursor:pointer;';
  clearBtn.addEventListener('click', () => {
    _trackerEntries = [];
    if (_trackerBody) _trackerBody.innerHTML = '';
  });
  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  closeBtn.style.cssText =
      'background:transparent;border:none;color:#a1a1aa;'
    + 'font-size:16px;cursor:pointer;line-height:1;padding:0 4px;';
  closeBtn.addEventListener('click', () => _toggleTracker(false));
  right.appendChild(clearBtn);
  right.appendChild(closeBtn);
  head.appendChild(title);
  head.appendChild(right);
  _trackerEl.appendChild(head);
  _trackerBody = document.createElement('div');
  _trackerBody.style.cssText =
      'flex:1;overflow-y:auto;padding:4px 8px;'
    + 'white-space:pre;font-size:11px;';
  _trackerEl.appendChild(_trackerBody);
  document.body.appendChild(_trackerEl);
}
function _toggleTracker(force) {
  _ensureTrackerUI();
  _trackerOpen = (force !== undefined) ? !!force : !_trackerOpen;
  _trackerEl.style.display = _trackerOpen ? 'flex' : 'none';
  const btn = document.getElementById('btn-iact-track');
  if (btn) btn.classList.toggle('active', _trackerOpen);
  try {
    localStorage.setItem('ifu:tracker_open', _trackerOpen ? '1' : '0');
  } catch (_e) {}
  // Render current backlog so opening shows the recent events.
  if (_trackerOpen) _renderTracker();
}
function _renderTracker() {
  if (!_trackerBody) return;
  // Cheap render: last 50 lines.
  const tail = _trackerEntries.slice(-50);
  const colorOf = (kind) => kind === 'click' ? '#5ec5ff'
    : kind === 'apply' ? '#f9b94f'
    : kind === 'svg'   ? '#84cc55'
    : kind === 'fetch' ? '#a78bfa'
    : '#d4d4d8';
  _trackerBody.innerHTML = tail.map(e => {
    const c = colorOf(e.kind);
    const ts = e.t.toFixed(2).padStart(7);
    return '<div style="color:' + c + ';">'
      + ts + '  ' + e.kind.padEnd(6) + ' ' + e.msg
      + '</div>';
  }).join('');
  _trackerBody.scrollTop = _trackerBody.scrollHeight;
}
function _track(kind, msg) {
  const t = (performance.now() / 1000);
  _trackerEntries.push({ t, kind, msg });
  if (_trackerEntries.length > _TRACKER_MAX) {
    _trackerEntries.splice(0, _trackerEntries.length - _TRACKER_MAX);
  }
  if (_trackerOpen) _renderTracker();
}
// Expose so other code (highlight code, fetchers, etc) can write events.
window._track = _track;
window._toggleTracker = _toggleTracker;
// Read-only accessor for the smoke tests / external scripts.
window._getTrackerEntries = () => _trackerEntries.slice();
// ===== end Interaction tracker ====================================

// ---- Server log overlay -------------------------------------------------
// Pinned to the bottom-right.  Auto-polls /api/debug/log and renders the
// rolling buffer the server keeps so the user can see exactly which
// requests landed, how long they took, and (critically) what went wrong
// or returned zero polylines.
let _serverLogEl = null;
let _serverLogBody = null;
let _serverLogSince = 0;
let _serverLogTimer = null;
let _serverLogOpen = false;

function _ensureServerLogEl() {
  if (_serverLogEl) return;
  _serverLogEl = document.createElement('div');
  _serverLogEl.id = '_server_log';
  _serverLogEl.style.cssText =
      'position:fixed;bottom:8px;right:8px;z-index:99998;'
    + 'width:480px;max-height:280px;'
    + 'background:rgba(15,15,17,.94);color:#d4d4d8;'
    + 'border:1px solid #3f3f46;border-radius:6px;'
    + 'font:11px/1.4 ui-monospace,Consolas,monospace;'
    + 'box-shadow:0 4px 16px rgba(0,0,0,.4);'
    + 'display:none;flex-direction:column;';
  const head = document.createElement('div');
  head.style.cssText =
      'padding:5px 9px;border-bottom:1px solid #3f3f46;'
    + 'display:flex;align-items:center;justify-content:space-between;'
    + 'background:#27272a;font-weight:600;color:#fafafa;';
  const title = document.createElement('span');
  title.textContent = 'Server log';
  const right = document.createElement('span');
  right.style.cssText = 'display:flex;gap:6px;';
  const clearBtn = document.createElement('button');
  clearBtn.textContent = 'clear';
  clearBtn.style.cssText =
      'background:transparent;border:1px solid #52525b;color:#d4d4d8;'
    + 'border-radius:3px;padding:1px 6px;font-size:10px;cursor:pointer;';
  clearBtn.addEventListener('click', () => {
    _serverLogBody.innerHTML = '';
  });
  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  closeBtn.style.cssText =
      'background:transparent;border:none;color:#a1a1aa;'
    + 'font-size:16px;cursor:pointer;line-height:1;padding:0 4px;';
  closeBtn.addEventListener('click', () => _toggleServerLog(false));
  right.appendChild(clearBtn);
  right.appendChild(closeBtn);
  head.appendChild(title);
  head.appendChild(right);
  _serverLogBody = document.createElement('div');
  _serverLogBody.style.cssText =
      'padding:4px 6px;overflow-y:auto;flex:1;white-space:pre-wrap;';
  _serverLogEl.appendChild(head);
  _serverLogEl.appendChild(_serverLogBody);
  document.body.appendChild(_serverLogEl);
}

function _serverLogRender(events) {
  if (!events || !events.length) return;
  const wasAtBottom =
      _serverLogBody.scrollTop + _serverLogBody.clientHeight
      >= _serverLogBody.scrollHeight - 4;
  for (const e of events) {
    const line = document.createElement('div');
    let color = '#d4d4d8';
    if (e.level === 'err')   color = '#fda4af';
    else if (e.level === 'warn')  color = '#fde047';
    else if (e.level === 'ok')    color = '#86efac';
    else if (e.level === 'req')   color = '#93c5fd';
    line.style.color = color;
    const parts = [`[${e.t}]`, (e.level || '').padEnd(4)];
    for (const [k, v] of Object.entries(e)) {
      if (k === 't' || k === 'level' || k === 'seq') continue;
      if (v === null || v === undefined || v === '') continue;
      parts.push(`${k}=${v}`);
    }
    line.textContent = parts.join(' ');
    _serverLogBody.appendChild(line);
  }
  // Cap to last 200 lines so the DOM doesn't blow up
  while (_serverLogBody.children.length > 200) {
    _serverLogBody.removeChild(_serverLogBody.firstChild);
  }
  if (wasAtBottom) _serverLogBody.scrollTop = _serverLogBody.scrollHeight;
}

async function _serverLogPoll() {
  if (!_serverLogOpen) return;
  try {
    const r = await fetch(API_BASE + '/api/debug/log'
                            + (_serverLogSince ? `?since=${_serverLogSince}` : ''));
    if (r.ok) {
      const data = await r.json();
      _serverLogSince = data.latest_seq || _serverLogSince;
      _serverLogRender(data.events || []);
    }
  } catch (_e) {}
  _serverLogTimer = setTimeout(_serverLogPoll, 1500);
}

function _toggleServerLog(forceOpen) {
  _ensureServerLogEl();
  if (forceOpen === undefined) forceOpen = !_serverLogOpen;
  _serverLogOpen = forceOpen;
  _serverLogEl.style.display = _serverLogOpen ? 'flex' : 'none';
  localStorage.setItem('ifu:server_log_open', _serverLogOpen ? '1' : '0');
  if (_serverLogOpen) {
    // On (re-)open: ask for the whole buffer once so we have context
    _serverLogSince = 0;
    if (_serverLogTimer) clearTimeout(_serverLogTimer);
    _serverLogPoll();
  } else if (_serverLogTimer) {
    clearTimeout(_serverLogTimer);
    _serverLogTimer = null;
  }
}

// Wire up the header button (will no-op if the element isn't on this
// page, e.g. on a non-editor route)
const _logBtn = document.getElementById('btn-server-log');
if (_logBtn) {
  _logBtn.addEventListener('click', () => _toggleServerLog());
}
const _trackBtn = document.getElementById('btn-iact-track');
if (_trackBtn) {
  _trackBtn.addEventListener('click', () => _toggleTracker());
}
// Restore previous tracker state -- off by default.
if (localStorage.getItem('ifu:tracker_open') === '1') {
  _toggleTracker(true);
}

// ===== Part-region colour map ======================================
// Paint each .part wrapper a unique stroke colour derived from its
// data-part value so the user can SEE which pixel region maps to
// which part_idx.  Lets us tell apart:
//   - "I selected 1 part but 4 lit up"  = different colours = real bug
//   - "I selected 1 part but 4 lit up"  = same colour       = visual
//                                                            overlap
const _REGION_PALETTE = [
  '#ef4444', '#f59e0b', '#84cc16', '#10b981', '#06b6d4',
  '#6366f1', '#ec4899', '#0ea5e9', '#a855f7', '#f43f5e',
  '#22c55e', '#eab308', '#14b8a6', '#3b82f6', '#d946ef',
];
function _applyRegionColours() {
  document.querySelectorAll(
    '.svg-pane svg .part[data-part]'
  ).forEach(g => {
    const idx = parseInt(g.dataset.part);
    if (!Number.isFinite(idx)) return;
    const c = _REGION_PALETTE[idx % _REGION_PALETTE.length];
    g.querySelectorAll('path').forEach(p => {
      p.style.stroke = c;
    });
  });
}
function _clearRegionColours() {
  document.querySelectorAll(
    '.svg-pane svg .part[data-part] path'
  ).forEach(p => { p.style.stroke = ''; });
}
function _toggleRegionMode(force) {
  const on = (force !== undefined) ? !!force
    : !document.body.classList.contains('show-part-regions');
  document.body.classList.toggle('show-part-regions', on);
  const btn = document.getElementById('btn-show-regions');
  if (btn) btn.classList.toggle('active', on);
  if (on) _applyRegionColours();
  else    _clearRegionColours();
  try {
    localStorage.setItem('ifu:show_regions', on ? '1' : '0');
  } catch (_e) {}
}
window._toggleRegionMode = _toggleRegionMode;
const _regBtn = document.getElementById('btn-show-regions');
if (_regBtn) {
  _regBtn.addEventListener('click', () => _toggleRegionMode());
}
if (localStorage.getItem('ifu:show_regions') === '1') {
  // Repaint after the SVG injects (delay handled by injectLiveSVG which
  // wipes inline styles -- we'll re-apply on demand).
  setTimeout(() => _toggleRegionMode(true), 100);
}
// ===== end Part-region colour map =================================

// ===== Closed-loop capture =========================================
// Snapshot the live 2D state (SVG outerHTML + what you clicked +
// tracker log) to the server so a headless harness can render it,
// crop a zoomed screenshot around the clicked parts, and analyse what
// the silhouette layer actually drew.  This is the "let Claude see the
// pixels" button -- DOM counts lie about visual bleed; screenshots
// don't.
async function _captureState(note) {
  const svgEl = document.querySelector(
    ".svg-pane[data-view='__live__'] svg")
    || document.querySelector(".svg-pane svg");
  if (!svgEl) { alert('No live SVG to capture yet.'); return null; }
  let fid = '', vid = '';
  try { fid = fileSel.value; } catch (_e) {}
  try { vid = viewSel.value; } catch (_e) {}

  // Current selection from the editor's per-pane state.
  let selection = [];
  try {
    const st = getState(fid, vid);
    selection = st && st.highlights ? [...st.highlights] : [];
  } catch (_e) {}

  // What the user actually clicked, pulled from the tracker.
  let tracker = [], clicks = [];
  try {
    tracker = (window._getTrackerEntries && window._getTrackerEntries())
      || [];
    clicks = tracker.filter(e => e.kind === 'click');
  } catch (_e) {}

  // Persisted per-part styles for this figure (so the harness knows
  // which parts SHOULD be styled vs which are transient selection).
  let styles = {};
  try {
    styles = JSON.parse(localStorage.getItem('partStyles_' + fid) || '{}');
  } catch (_e) {}

  // 3D highlight state -- lets the replay compare which part is lit in
  // the 3D pane vs the 2D pane (catches 2D<->3D index disagreement).
  let meshes3d = null;
  try {
    meshes3d = window.IFU_VIEWER && window.IFU_VIEWER.debugDump3D
      ? window.IFU_VIEWER.debugDump3D() : null;
  } catch (_e) {}

  const body = {
    note: note || '',
    fid, vid,
    selection,
    clicks,
    tracker: tracker.slice(-60),
    styles,
    meshes3d,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    svg: svgEl.outerHTML,
  };
  try {
    const r = await fetch(API_BASE + '/api/debug/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    const btn = document.getElementById('btn-iact-capture');
    if (btn) {
      const old = btn.textContent;
      btn.textContent = '✓ captured #' + j.seq;
      btn.classList.add('active');
      setTimeout(() => { btn.textContent = old;
        btn.classList.remove('active'); }, 2000);
    }
    if (window._track) window._track('capture',
      'seq=' + j.seq + ' sel=[' + selection.join(',') + '] svg='
      + Math.round((body.svg.length||0)/1024) + 'KB');
    return j;
  } catch (e) {
    alert('Capture failed: ' + e);
    return null;
  }
}
window._captureState = _captureState;
const _capBtn = document.getElementById('btn-iact-capture');
if (_capBtn) {
  _capBtn.addEventListener('click', () => _captureState(''));
}
// ===== end Closed-loop capture =====================================
// Restore previous open/closed state.  Default OFF so it doesn't get
// in the way unless the user asked for it.
if (localStorage.getItem('ifu:server_log_open') === '1') {
  _toggleServerLog(true);
}
// Also open it automatically on ?dbg=1 so the perf HUD + server log
// pair up usefully.
if (_DBG_ON && localStorage.getItem('ifu:server_log_open') !== '0') {
  _toggleServerLog(true);
}
window._toggleServerLog = _toggleServerLog;

function applyHighlights() {
  const _t0 = _DBG_ON ? performance.now() : 0;
  const st = getState(fileSel.value, viewSel.value);
  const set = st.highlights || new Set();
  const any = set.size > 0;
  const svg = activeSvg();
  if (_DBG_ON) {
    const preview = [...set].slice(0, 12).join(',');
    _dbgLine('SELECTED', 0, set.size + ': ' + preview);
  }
  if (svg) {
    const partCount = svg.querySelectorAll('.part').length;
    let _hitCount = 0, _dimCount = 0;
    const _hitIdxs = new Set();   // unique data-part values that got .highlight
    _dbgTime('toggle-classes', () => {
      svg.querySelectorAll('.part').forEach(p => {
        const idx = parseInt(p.dataset.part);
        const hit = set.has(idx);
        p.classList.toggle('highlight', hit);
        p.classList.toggle('dim', any && !hit);
        if (hit) { _hitCount++; _hitIdxs.add(idx); }
        if (any && !hit) _dimCount++;
      });
    }, `${partCount} parts`);
    // Tracker: how many .part wrappers got highlight + how many
    // distinct part-idx values.  If the user selected 1 idx but the
    // tracker shows >1 _hitIdxs, that's the "other parts highlighted"
    // bug -- some wrapper has the wrong data-part value.
    if (window._track) {
      const idxs = [...set].sort((a,b)=>a-b).join(',');
      const hitIdxs = [...(_hitIdxs)].sort((a,b)=>a-b);
      const sameAsSel = hitIdxs.length === set.size
                         && hitIdxs.every(i => set.has(i));
      window._track('apply',
        'sel=' + set.size + ' [' + idxs + ']'
        + ' .highlight=' + _hitCount
        + ' uniq=' + hitIdxs.length
        + (sameAsSel ? '' : ' WARN_EXTRA=[' + hitIdxs.join(',') + ']')
        + ' .dim=' + _dimCount
        + ' /' + partCount);
    }
    // Closed-silhouette fill / outline overlay.  This is the TRANSIENT
    // selection feedback (drawn while the user has parts selected,
    // before they apply a preset).  Kept deliberately thin so it
    // can't bleed into neighbour parts -- a 4mm bold stroke at this
    // stage was the source of the "selecting one part highlighted a
    // bunch" complaint, because a 4mm halo around a small part swallows
    // the adjacent geometry visually.  The PERSISTENT silhouette
    // (renderPersistentSilhouettes, draws the user's APPLIED preset
    // at its chosen width) is unchanged.
    _dbgTime('applySilhouetteFill', () => applySilhouetteFill(
      svg, set,
      $('sty-fill-on').checked,
      $('sty-fill').value,
      parseFloat($('sty-fill-opacity').value),
      $('sty-stroke').value,
      // Transient stroke width: cap at 0.8 mm regardless of the
      // sty-width slider value.  The slider drives the APPLIED preset
      // strength; selection feedback should always be a thin
      // indicator.
      Math.min(0.8, parseFloat($('sty-width').value)),
      { dashed: true }
    ), `${set.size} sel`);
    // Kick the server fetches ONLY when the highlight set has actually
    // changed (style-only refreshes are routed through
    // restyleSilhouetteOnly and don't get here).  Bold edge now uses
    // the rasterized footprint, so we fetch it on demand for the
    // selected parts; old silhouette fetch is gated on the shade
    // checkbox inside fetchTrueSilhouettes.
    const sig = set.size ? [...set].sort((a,b)=>a-b).join(',') : '';
    if (sig !== _lastSilHighlightSig) {
      _lastSilHighlightSig = sig;
      if (set.size > 0) {
        setTimeout(fetchSelectedFootprints, 0);
        setTimeout(fetchTrueSilhouettes, 0);
      }
    }
  }
  partList.querySelectorAll('li').forEach(li => {
    li.classList.toggle('highlighted', set.has(parseInt(li.dataset.part)));
  });
  if (treeRoot) {
    treeRoot.querySelectorAll('.tree-row').forEach(r => {
      const idx = _tree_to_part_idx[r.dataset.treeId];
      r.classList.toggle('highlighted', idx != null && set.has(idx));
    });
  }
  if (set.size === 0) {
    selectionInfo.textContent = 'Nothing selected';
  } else if (set.size === 1) {
    const idx = [...set][0];
    const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
    const p = fe.parts.find(x => x.idx === idx);
    selectionInfo.innerHTML = `<b>Part ${idx}</b><br>${p ? p.label : ''}`;
  } else {
    const list = [...set].sort((a,b)=>a-b);
    const preview = list.slice(0, 8).join(', ') + (list.length > 8 ? ', ...' : '');
    selectionInfo.innerHTML = `<b>${set.size} parts</b> selected<br>` +
      `<span style="font-family: ui-monospace, Consolas, monospace; font-size: 11px;">${preview}</span>`;
  }
  _dbgTime('applyHighlights3D', () =>
    window.IFU_VIEWER?.applyHighlights3D?.(set), `${set.size} sel`);
  // Light up the matching preset (or clear) so the user can see at
  // a glance what's already applied to the selection.
  if (typeof _refreshPresetActiveState === 'function') {
    try { _refreshPresetActiveState(); } catch (_e) {}
  }
  if (_DBG_ON) {
    _dbgLine('applyHighlights TOTAL', performance.now() - _t0,
      `${set.size} sel`);
  }
}

// Keyboard shortcuts (only when no input/select/textarea has focus).
//   Esc      -- clear selection
//   1/2/3    -- 2D / Split / 3D layout
//   R        -- reset 3D camera to current view's direction
//   F        -- fit (reset pan/zoom on active 2D pane)
window.addEventListener('keydown', (e) => {
  // Ctrl/Cmd+S = save the current figure.  This works even if focus
  // is in an <input> (e.g. the fig-name field) because we want save
  // to be available everywhere in the editor.  Browser default is
  // "save page" -- preventDefault so we capture it.
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    if (typeof saveCurrentAsFigure === 'function') saveCurrentAsFigure();
    return;
  }
  const t = e.target;
  if (t && /^(INPUT|TEXTAREA|SELECT)$/i.test(t.tagName)) return;
  if (e.key === 'Escape') return clearHighlights();
  if (e.key === '1') return $('lay-2d').click();
  if (e.key === '2') return $('lay-split').click();
  if (e.key === '3') return $('lay-3d').click();
  if (e.key.toLowerCase() === 'r') {
    // R = re-snap 3D camera to current 2D view direction
    snap3DToCurrentView?.();
    return;
  }
  if (e.key.toLowerCase() === 'f') {
    // F = reset pan/zoom on the active 2D pane
    const pane = activePane();
    if (pane) {
      const st = getState(pane.dataset.file, pane.dataset.view);
      st.tx = 0; st.ty = 0; st.scale = 1;
      applyTransform(pane);
    }
    return;
  }
});

function applyTransform(pane) {
  const svg = pane.querySelector('svg');
  const inner = svg.querySelector(':scope > g');
  if (!inner) return;
  const st = getState(pane.dataset.file, pane.dataset.view);
  // Outer transform group: wrap if not present
  let viewG = svg.querySelector(':scope > g.view-transform');
  if (!viewG) {
    viewG = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    viewG.setAttribute('class', 'view-transform');
    // move all existing children of svg into viewG
    while (svg.firstChild) viewG.appendChild(svg.firstChild);
    svg.appendChild(viewG);
    // annotation layer above transform group
    const al = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    al.setAttribute('class', 'annotation-layer');
    svg.appendChild(al);
  }
  viewG.setAttribute('transform',
    `translate(${st.tx} ${st.ty}) scale(${st.scale})`);
}

// Map a screen/client point to the SVG ROOT user space (viewBox units)
// -- the same space the view-transform's translate/scale live in.  The
// pan/zoom math MUST work in this space: these viewBoxes are thousands
// of mm wide, so doing the math in raw pixels (the old bug) mixed two
// coordinate systems and flung the drawing around on zoom.
function _clientToUserPt(svg, clientX, clientY) {
  try {
    const m = svg.getScreenCTM();
    if (m) {
      const p = new DOMPoint(clientX, clientY).matrixTransform(m.inverse());
      return { x: p.x, y: p.y };
    }
  } catch (_e) {}
  const rect = svg.getBoundingClientRect();
  return { x: clientX - rect.left, y: clientY - rect.top };
}

// Cache of per-part edge vertices (in scaleG user space) for nearest-
// edge click resolution.  Built lazily per-SVG; invalidated when a new
// SVG is injected (the cache lives on the svg element itself).
function _partEdgeVerts(svg) {
  if (svg._edgeVertsCache) return svg._edgeVertsCache;
  const m = new Map();   // idx -> [ polyline, ... ]  (polyline = [[x,y],..])
  // Only VISIBLE edge layers -- the user clicks the lines they can see.
  ['.layer-outline_v', '.layer-sharp_v', '.layer-smooth_v'].forEach(sel => {
    svg.querySelectorAll(sel + ' .part[data-part]').forEach(g => {
      const idx = parseInt(g.dataset.part);
      if (Number.isNaN(idx)) return;
      let polys = m.get(idx);
      if (!polys) { polys = []; m.set(idx, polys); }
      g.querySelectorAll('path').forEach(p => {
        const toks = (p.getAttribute('d') || '').match(/-?\d+(?:\.\d+)?/g);
        if (!toks || toks.length < 2) return;
        const pl = [];
        for (let i = 0; i + 1 < toks.length; i += 2)
          pl.push([parseFloat(toks[i]), parseFloat(toks[i + 1])]);
        if (pl.length) polys.push(pl);
      });
    });
  });
  svg._edgeVertsCache = m;
  return m;
}

// Squared distance from point (px,py) to segment (ax,ay)-(bx,by).
function _distPtSeg2(px, py, ax, ay, bx, by) {
  const vx = bx - ax, vy = by - ay;
  const wx = px - ax, wy = py - ay;
  const c1 = vx*wx + vy*wy;
  if (c1 <= 0) return wx*wx + wy*wy;
  const c2 = vx*vx + vy*vy;
  if (c2 <= c1) { const dx = px-bx, dy = py-by; return dx*dx + dy*dy; }
  const t = c1 / c2;
  const dx = px - (ax + t*vx), dy = py - (ay + t*vy);
  return dx*dx + dy*dy;
}

// Resolve which part a click really targets.  The hit-hull layer uses
// CONVEX HULLS, which massively over-claim area for non-convex parts
// (rings, frames, diagonals) -- so a click can land inside several
// parts' hulls at once and the topmost (by DOM order) wins, selecting a
// part the user didn't click on ("I clicked one part, a different/large
// one highlighted").  Fix: among every part whose hull is under the
// cursor, pick the one whose ACTUAL rendered edge is nearest the click.
// That matches the line-art the user actually aimed at.
function _resolvePartClick(svg, ev) {
  // Candidate parts = every part whose CLICK TARGET is under the
  // pointer.  Parts are clickable through two layers: the convex
  // hit-hull (pointer-events:fill) and the 3mm stroke hit layer
  // (server-baked).  Both surface in elementsFromPoint; the visible
  // edge + silhouette layers are pointer-events:none so they don't.
  // We therefore collect any .part[data-part] under the cursor.
  const stack = document.elementsFromPoint(ev.clientX, ev.clientY);
  const cands = [];
  for (const el of stack) {
    const g = el.closest && el.closest('.part[data-part]');
    if (g && !g.closest('.layer-silhouette')) {
      const idx = parseInt(g.dataset.part);
      if (!Number.isNaN(idx) && !cands.includes(idx)) cands.push(idx);
    }
  }
  if (!cands.length) return null;
  if (cands.length === 1) return cands[0];

  // Map the click into scaleG user space (same space as the edge 'd'
  // tokens) using any path's inverse screen-CTM.
  const anyPath = svg.querySelector('.layer-hit-hull path')
               || svg.querySelector('.layer-outline_v path');
  let cx = ev.clientX, cy = ev.clientY;
  try {
    const ctm = anyPath.getScreenCTM();
    if (ctm) {
      const pt = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(
        ctm.inverse());
      cx = pt.x; cy = pt.y;
    }
  } catch (_e) {}

  const verts = _partEdgeVerts(svg);
  let best = cands[0], bestD = Infinity;
  for (const idx of cands) {
    const polys = verts.get(idx);
    if (!polys || !polys.length) continue;
    let d2 = Infinity;
    for (const pl of polys) {
      // point-to-segment over each polyline (so a part with many
      // densely-sampled verts doesn't win just by vertex count -- we
      // measure distance to the actual LINE the user sees).
      for (let i = 0; i + 1 < pl.length; i++) {
        const dd = _distPtSeg2(cx, cy, pl[i][0], pl[i][1],
                               pl[i+1][0], pl[i+1][1]);
        if (dd < d2) d2 = dd;
      }
      if (pl.length === 1) {
        const dx = pl[0][0]-cx, dy = pl[0][1]-cy;
        const dd = dx*dx+dy*dy; if (dd < d2) d2 = dd;
      }
    }
    if (d2 < bestD) { bestD = d2; best = idx; }
  }
  return best;
}

function attachInteractivity(pane) {
  const svg = pane.querySelector('svg');
  if (svg.dataset.attached) return;
  svg.dataset.attached = '1';

  // Capture-on-open: a freshly-attached LIVE svg means a figure just
  // rendered.  Snapshot it (debounced, after silhouettes settle) and
  // upload as the figure's + parent view's thumbnail so tiles get a
  // preview without the user having to explicitly save.
  if (pane && pane.dataset && pane.dataset.view === '__live__'
      && typeof _scheduleOpenThumbnail === 'function') {
    try { _scheduleOpenThumbnail(); } catch (_e) {}
  }

  // make sure the transform wrapper exists
  applyTransform(pane);

  // Click on a path -> resolve nearest-edge part -> highlight.
  // Ctrl/Cmd-click toggles into a multi-selection.
  svg.addEventListener('click', e => {
    if (svg.classList.contains('annotate-mode')) {
      handleAnnotateClick(e, svg, pane); return;
    }
    // Trace: which element actually received the click and which
    // layer (if any) wraps it.  Surfaces the "I clicked part_3 but
    // it highlighted part_5" mystery: shows what the DOM saw.
    const targetTag = (e.target && e.target.tagName) || '?';
    const targetCls = (e.target && e.target.getAttribute &&
                       e.target.getAttribute('class')) || '';
    // What the raw topmost hull would have selected (for diagnostics).
    let rawIdx = null;
    {
      let p = e.target;
      while (p && p !== svg && !p.classList?.contains('part'))
        p = p.parentElement;
      if (p && p.classList?.contains('part'))
        rawIdx = parseInt(p.dataset.part);
    }
    // Nearest-edge resolution among all overlapping hulls.  Fall back
    // to the raw e.target part when the resolver finds no candidate
    // under the pointer (e.g. a synthetic click dispatched directly on
    // a .part element with coordinates that miss every hit layer).
    let idx = _resolvePartClick(svg, e);
    if ((idx == null || Number.isNaN(idx)) && rawIdx != null
        && !Number.isNaN(rawIdx)) {
      idx = rawIdx;
    }
    if (idx != null && !Number.isNaN(idx)) {
      if (window._track) window._track('click',
        'idx=' + idx
        + (rawIdx != null && rawIdx !== idx
            ? ' (topmost-hull was ' + rawIdx + ', nearest-edge won)' : '')
        + ' tag=' + targetTag
        + (e.ctrlKey || e.metaKey ? ' +ctrl' : '')
        + ' targetClass="' + targetCls + '"');
      togglePartHighlight(idx,
                          {append: e.ctrlKey || e.metaKey});
    } else {
      if (window._track) window._track('click',
        'MISS  tag=' + targetTag + ' class="' + targetCls + '"');
    }
  });

  // Hover: show tooltip with part label
  svg.addEventListener('mousemove', e => {
    if (svg.classList.contains('annotate-mode')) return;
    let p = e.target;
    while (p && p !== svg && !p.classList?.contains('part')) p = p.parentElement;
    if (p && p.classList?.contains('part')) {
      tooltip.textContent = p.dataset.label || '';
      tooltip.style.left = (e.clientX + 12) + 'px';
      tooltip.style.top = (e.clientY + 12) + 'px';
      tooltip.classList.add('show');
    } else {
      tooltip.classList.remove('show');
    }
  });
  svg.addEventListener('mouseleave', () => tooltip.classList.remove('show'));

  // Pan: middle-mouse, or left when not on a part.  Track the cursor in
  // SVG user space so the content tracks the pointer 1:1 regardless of
  // viewBox size (the old code added raw pixel deltas to user-unit tx).
  let panning = false, lastU = null;
  svg.addEventListener('mousedown', e => {
    if (svg.classList.contains('annotate-mode')) {
      annotateMouseDown(e, svg, pane); return;
    }
    let onPart = false;
    let p = e.target;
    while (p && p !== svg) {
      if (p.classList?.contains('part')) { onPart = true; break; }
      p = p.parentElement;
    }
    if (e.button === 1 || (e.button === 0 && (e.shiftKey || !onPart))) {
      panning = true;
      lastU = _clientToUserPt(svg, e.clientX, e.clientY);
      svg.classList.add('panning');
      e.preventDefault();
    }
  });
  window.addEventListener('mousemove', e => {
    if (!panning) return;
    const st = getState(pane.dataset.file, pane.dataset.view);
    const u = _clientToUserPt(svg, e.clientX, e.clientY);
    if (lastU) { st.tx += (u.x - lastU.x); st.ty += (u.y - lastU.y); }
    lastU = u;
    applyTransform(pane);
  });
  window.addEventListener('mouseup', () => {
    const wasPanning = panning;
    panning = false; svg.classList.remove('panning');
    // Panning while zoomed in moves the visible region -> refresh the
    // high-detail overlay for the new viewport once the drag settles.
    if (wasPanning) _scheduleAutoSharpen(pane);
  });

  // Wheel zoom centred on the cursor.  Work in SVG user space (the same
  // space tx/ty/scale live in) so the point under the pointer stays put.
  // Clamp deltaY so a fast wheel / trackpad fling can't jump the zoom.
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    const st = getState(pane.dataset.file, pane.dataset.view);
    const dy = Math.max(-120, Math.min(120, e.deltaY));
    const factor = Math.exp(-dy * 0.0015);
    const newScale = Math.max(0.1, Math.min(50, st.scale * factor));
    if (newScale === st.scale) return;
    const u = _clientToUserPt(svg, e.clientX, e.clientY);
    const k = newScale / st.scale;
    // Keep the user-space point under the cursor fixed on screen.
    st.tx = u.x - (u.x - st.tx) * k;
    st.ty = u.y - (u.y - st.ty) * k;
    st.scale = newScale;
    applyTransform(pane);
    _scheduleAutoSharpen(pane);
  }, { passive: false });
}

// Lazy SVG loader: baked SVGs are no longer inlined into viewer.html
// (saved ~25 MB of HTML).  When refreshPane activates a pane that
// hasn't been populated yet, it fetches `data-svg-src` synchronously
// (via async, but we await it so the rest of refreshPane sees a real
// <svg> in the DOM).
async function _ensurePaneSvgLoaded(pane) {
  if (!pane) return;
  // Already populated (either by build_html inlining or a previous
  // fetch) -> nothing to do.
  if (pane.querySelector('svg')) return;
  // The __live__ pane is populated by injectLiveSVG, never by fetch.
  if (pane.dataset.view === '__live__') return;
  const src = pane.dataset.svgSrc;
  if (!src) return;
  try {
    const r = await fetch(src);
    if (!r.ok) {
      console.error('baked SVG fetch failed', src, r.status);
      return;
    }
    let txt = await r.text();
    // Strip xml prolog + give the <svg> a stable id (the JS that
    // attached interactivity used to do this; legacy code still
    // expects the id to be present).
    txt = txt.replace(/<\\?xml[^>]*\\?>\\s*/, '');
    const svgId = pane.dataset.svgId
                    || `svg_${pane.dataset.file}_${pane.dataset.view}`;
    if (!/id\\s*=/.test(txt.slice(0, 80))) {
      txt = txt.replace('<svg', `<svg id="${svgId}"`);
    }
    pane.innerHTML = txt;
  } catch (e) {
    console.error('baked SVG load error', src, e);
  }
}

async function refreshPane() {
  const _t0 = _DBG_ON ? performance.now() : 0;
  document.querySelectorAll('.svg-pane').forEach(p => p.classList.remove('active'));
  const pane = activePane();
  if (!pane) return;
  pane.classList.add('active');
  // Lazy-load the baked SVG into this pane the first time it's active.
  await _ensurePaneSvgLoaded(pane);
  _dbgTime('attachInteractivity', () => attachInteractivity(pane));
  _dbgTime('refreshPartList', () => refreshPartList());
  _dbgTime('applyMode', () => applyMode());
  _dbgTime('injectHitHullsLayer', () => injectHitHullsLayer());
  _dbgTime('renderPersistentSilhouettes', () => renderPersistentSilhouettes());
  _dbgTime('applyHighlights (in refreshPane)', () => applyHighlights());
  _dbgTime('refreshAnnotations', () => refreshAnnotations(pane));
  _dbgTime('updateCalloutCount', () => updateCalloutCount());
  if (_DBG_ON) _dbgLine('refreshPane TOTAL',
    performance.now() - _t0, fileSel.value + '/' + viewSel.value);
}

// --- Mode + layer toggles ---
const MODE_LAYERS = {
  smart:    { outline_v: 1, sharp_v: 1, smooth_v: 0, hidden_outline: 0, hidden_sharp: 0 },
  detailed: { outline_v: 1, sharp_v: 1, smooth_v: 1, hidden_outline: 0, hidden_sharp: 0 },
  hidden:   { outline_v: 1, sharp_v: 1, smooth_v: 0, hidden_outline: 1, hidden_sharp: 1 },
};
let currentMode = 'smart';
function setMode(m) {
  currentMode = m;
  $('mode-pill').textContent = m;
  document.querySelectorAll('header button[id^="btn-"]').forEach(b => {
    if (['btn-smart', 'btn-detailed', 'btn-hidden'].includes(b.id)) {
      b.classList.toggle('active', b.id === 'btn-' + m);
    }
  });
  // Sync checkbox panel with mode
  const ms = MODE_LAYERS[m];
  document.querySelectorAll('input[data-layer]').forEach(cb => {
    cb.checked = !!ms[cb.dataset.layer];
  });
  applyMode();
}
function applyMode() {
  const svg = activeSvg();
  if (!svg) return;
  document.querySelectorAll('input[data-layer]').forEach(cb => {
    svg.classList.toggle('hide-' + cb.dataset.layer, !cb.checked);
  });
}
$('btn-smart').onclick = () => setMode('smart');
$('btn-detailed').onclick = () => setMode('detailed');
$('btn-hidden').onclick = () => setMode('hidden');
document.querySelectorAll('input[data-layer]').forEach(cb => {
  cb.addEventListener('change', applyMode);
});

// --- Annotations ---
let annotating = false;
let annoStart = null;
let annoPreview = null;
$('btn-annotate').onclick = () => {
  annotating = !annotating;
  $('btn-annotate').classList.toggle('active', annotating);
  document.querySelectorAll('.svg-pane svg').forEach(s => {
    s.classList.toggle('annotate-mode', annotating);
  });
};
$('btn-clear').onclick = () => {
  const st = getState(fileSel.value, viewSel.value);
  st.annotations = [];
  refreshAnnotations(activePane());
  updateCalloutCount();
};

function svgClientToUser(svg, clientX, clientY) {
  const pt = svg.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  const inner = svg.querySelector('g.view-transform');
  return pt.matrixTransform(inner.getScreenCTM().inverse());
}

function annotateMouseDown(e, svg, pane) {
  if (e.button !== 0) return;
  const p = svgClientToUser(svg, e.clientX, e.clientY);
  annoStart = {x: p.x, y: p.y, paneFile: pane.dataset.file,
                paneView: pane.dataset.view, screenX: e.clientX,
                screenY: e.clientY};
  e.preventDefault();

  // Live preview while dragging
  const layer = svg.querySelector('g.annotation-layer');
  if (annoPreview) annoPreview.remove();
  annoPreview = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  annoPreview.setAttribute('x1', p.x); annoPreview.setAttribute('y1', p.y);
  annoPreview.setAttribute('x2', p.x); annoPreview.setAttribute('y2', p.y);
  annoPreview.setAttribute('stroke', 'var(--accora-teal)');
  annoPreview.setAttribute('stroke-width', '0.7');
  annoPreview.setAttribute('stroke-dasharray', '3 3');
  layer.appendChild(annoPreview);

  // Track drag + mouseup
  const onMove = ev => {
    if (!annoStart || !annoPreview) return;
    const q = svgClientToUser(svg, ev.clientX, ev.clientY);
    annoPreview.setAttribute('x2', q.x);
    annoPreview.setAttribute('y2', q.y);
  };
  const onUp = ev => {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    if (!annoStart) return;
    const dx = ev.clientX - annoStart.screenX;
    const dy = ev.clientY - annoStart.screenY;
    const dragLen = Math.hypot(dx, dy);
    if (annoPreview) { annoPreview.remove(); annoPreview = null; }
    if (dragLen < 5) { annoStart = null; return; }  // misclick
    const q = svgClientToUser(svg, ev.clientX, ev.clientY);
    const text = prompt('Callout label:', '');
    if (text === null || text === '') { annoStart = null; return; }
    const st = getState(annoStart.paneFile, annoStart.paneView);
    st.annotations.push({
      x1: annoStart.x, y1: annoStart.y,
      x2: q.x, y2: q.y, text: text
    });
    annoStart = null;
    refreshAnnotations(pane);
    updateCalloutCount();
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}
function handleAnnotateClick(_e, _svg, _pane) { /* drag-based; click is a no-op */ }

function refreshAnnotations(pane) {
  if (!pane) return;
  const svg = pane.querySelector('svg');
  let layer = svg.querySelector('g.annotation-layer');
  if (!layer) return;
  layer.innerHTML = '';
  const st = getState(pane.dataset.file, pane.dataset.view);
  st.annotations.forEach((a, i) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', 'anno-group');
    g.dataset.idx = i;
    // arrow
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', a.x1); line.setAttribute('y1', a.y1);
    line.setAttribute('x2', a.x2); line.setAttribute('y2', a.y2);
    line.setAttribute('class', 'arrow');
    g.appendChild(line);
    // arrowhead at (x1,y1)
    const dx = a.x1 - a.x2, dy = a.y1 - a.y2;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const px = -uy, py = ux;
    const ah = 18;
    const aw = 7;
    const p1 = `${a.x1},${a.y1}`;
    const p2 = `${a.x1 - ux*ah + px*aw},${a.y1 - uy*ah + py*aw}`;
    const p3 = `${a.x1 - ux*ah - px*aw},${a.y1 - uy*ah - py*aw}`;
    const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    poly.setAttribute('points', `${p1} ${p2} ${p3}`);
    poly.setAttribute('class', 'arrowhead');
    g.appendChild(poly);
    // label
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', a.x2 + 6);
    t.setAttribute('y', a.y2);
    t.setAttribute('dominant-baseline', 'middle');
    t.textContent = a.text;
    g.appendChild(t);
    g.addEventListener('click', ev => {
      if (annotating) return;
      ev.stopPropagation();
      if (confirm('Delete callout "' + a.text + '"?')) {
        st.annotations.splice(i, 1);
        refreshAnnotations(pane); updateCalloutCount();
      }
    });
    layer.appendChild(g);
  });
}

function updateCalloutCount() {
  const st = getState(fileSel.value, viewSel.value);
  calloutCount.textContent = `${st.annotations.length} callout` +
    (st.annotations.length === 1 ? '' : 's') + ' on this view';
}

// --- Screenshot exporter ------------------------------------------------
// Captures whichever panes are currently visible (2D, 3D, or both for
// Split) into a single PNG so the user can save the rendered comparison
// for iteration / IFU artwork prep.
// - 2D pane: serialise the SVG, rasterise via <canvas>
// - 3D pane: read the WebGL canvas directly
// - Split:   composite the two side-by-side onto a single canvas
async function svgPaneToCanvas(pane, width, height) {
  const svg = pane.querySelector('svg');
  if (!svg) return null;
  // Inline computed dimensions from the viewBox so the serialised SVG
  // rasterises at a known size.
  const clone = svg.cloneNode(true);
  clone.setAttribute('width',  width);
  clone.setAttribute('height', height);
  // Inline the per-part styles so they survive serialisation.  The
  // <style id="per-part-styles"> tag lives in document.head, not inside
  // the SVG; without inlining, an SVG-as-image-via-blob has no document
  // context to pick up our overrides.
  const styleEl = document.getElementById('per-part-styles');
  if (styleEl && styleEl.textContent) {
    const inline = document.createElementNS('http://www.w3.org/2000/svg', 'style');
    inline.textContent = styleEl.textContent;
    clone.insertBefore(inline, clone.firstChild);
  }
  const xml = new XMLSerializer().serializeToString(clone);
  const blob = new Blob([xml], { type: 'image/svg+xml;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const img = new Image();
  await new Promise((res, rej) => {
    img.onload = res; img.onerror = rej; img.src = url;
  });
  const cnv = document.createElement('canvas');
  cnv.width = width; cnv.height = height;
  const ctx = cnv.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);
  ctx.drawImage(img, 0, 0, width, height);
  URL.revokeObjectURL(url);
  return cnv;
}

async function captureScreenshot() {
  const wantS = document.body.classList.contains('layout-split');
  const want2 = wantS || document.body.classList.contains('layout-2d');
  const want3 = wantS || document.body.classList.contains('layout-3d');
  let canvas2 = null, canvas3 = null;

  if (want2) {
    const pane = activePane();
    if (pane) {
      const r = pane.getBoundingClientRect();
      canvas2 = await svgPaneToCanvas(pane, Math.round(r.width), Math.round(r.height));
    }
  }
  if (want3) {
    const webglCanvas = document.getElementById('webgl-canvas');
    if (webglCanvas) {
      // Force a fresh render before reading the pixels -- the WebGL
      // back-buffer is often cleared after present.
      renderer3d_request_present();
      const out = document.createElement('canvas');
      out.width = webglCanvas.width;
      out.height = webglCanvas.height;
      const ctx = out.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, out.width, out.height);
      ctx.drawImage(webglCanvas, 0, 0);
      canvas3 = out;
    }
  }

  // Composite into one image
  let final;
  if (canvas2 && canvas3) {
    // Side-by-side; scale to match heights
    const h = Math.max(canvas2.height, canvas3.height);
    const w2 = Math.round(canvas2.width * h / canvas2.height);
    const w3 = Math.round(canvas3.width * h / canvas3.height);
    final = document.createElement('canvas');
    final.width = w2 + 8 + w3;
    final.height = h;
    const ctx = final.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, final.width, final.height);
    ctx.drawImage(canvas2, 0, 0, w2, h);
    ctx.fillStyle = '#d8d8da';
    ctx.fillRect(w2 + 3, 0, 2, h);
    ctx.drawImage(canvas3, w2 + 8, 0, w3, h);
  } else {
    final = canvas2 || canvas3;
  }
  if (!final) return;

  // Trigger download
  final.toBlob((blob) => {
    const a = document.createElement('a');
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.href = URL.createObjectURL(blob);
    a.download = `${fileSel.value}_${viewSel.value}_${ts}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }, 'image/png');
}

// Stub kept here so the synchronous capture call resolves -- the module
// script overrides this with a real `renderer.render(scene, camera)` call.
function renderer3d_request_present() {}

$('btn-screenshot').addEventListener('click', () => {
  captureScreenshot().catch(err => {
    console.error('screenshot failed:', err);
    alert('Screenshot failed: ' + err.message);
  });
});

// P2.b: hi-detail render of the currently-visible viewport.  We
// compute the visible bbox in projector (u,v) space, POST it to
// /api/render_region at a finer mesh/sample, then overlay the
// returned SVG as a new <g class="layer-region-detail"> group inside
// the active pane's scale-flip group.  Clicking again clears it.
async function detailRenderActive() {
  if (typeof API_BASE !== 'string') return;
  const svg = activeSvg();
  if (!svg) return;
  // The visible (u,v) bbox = viewBox + current pan/zoom transform.
  // The scale-flip group has transform="scale(1,-1)" so the SVG y
  // axis is negated.  We compute bbox in PROJECTOR space by taking
  // the SVG's viewBox and the pan/zoom transform of the view-transform group.
  const viewG = svg.querySelector('g.view-transform');
  const vb = svg.getAttribute('viewBox').split(/\\s+/).map(parseFloat);
  // viewBox is [x, y, w, h]; flip y to projector u,v
  // For a fresh load (no pan/zoom) the viewport IS the viewBox.
  // When zoomed, viewG has translate(tx,ty) scale(s); we invert that
  // to find which portion of the viewBox is currently visible.
  let bboxUv = [vb[0], -(vb[1] + vb[3]), vb[0] + vb[2], -vb[1]];
  if (viewG) {
    const t = viewG.getAttribute('transform') || '';
    const tm = t.match(/translate\\(([-\\d.]+)\\s+([-\\d.]+)\\)\\s*scale\\(([-\\d.]+)\\)/);
    if (tm) {
      const tx = parseFloat(tm[1]), ty = parseFloat(tm[2]), sc = parseFloat(tm[3]);
      // Visible area in viewBox coords = (viewBox + pan) / scale
      const vw = vb[2] / sc, vh = vb[3] / sc;
      const vx = vb[0] - tx / sc, vy = vb[1] - ty / sc;
      bboxUv = [vx, -(vy + vh), vx + vw, -vy];
    }
  }
  const fid = fileSel.value, vid = viewSel.value;
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  const body = {
    file_id: fid,
    bbox_uv: bboxUv,
    mesh_defl: 0.3,
    sample_defl: 0.3,
  };
  // Camera: same as fetchTrueSilhouettes
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  } else if (ve && ve.view_dir) {
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  } else {
    alert('No view direction for the active source/view');
    return;
  }
  const btn = $('btn-detail');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ rendering...';
  try {
    const r = await fetch(API_BASE + '/api/render_region', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    // Parse and graft the new SVG's inner contents into a <g> overlay
    const tmp = new DOMParser().parseFromString(text, 'image/svg+xml');
    const incoming = tmp.documentElement;   // <svg>
    const scaleG = svg.querySelector('g[transform="scale(1,-1)"]')
                || svg.querySelector('.view-transform > g')
                || svg.querySelector(':scope > g');
    if (!scaleG) throw new Error('no scale group in active SVG');
    scaleG.querySelector(':scope > g.layer-region-detail')?.remove();
    const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    layer.setAttribute('class', 'layer-region-detail');
    // The incoming SVG has its own scale(1,-1) wrapper -- we want its
    // INNER content placed directly into the existing scale-flip group
    // so the wrappers don't double up.
    const incomingScaleG = incoming.querySelector('g[transform="scale(1,-1)"]');
    if (incomingScaleG) {
      Array.from(incomingScaleG.children).forEach(ch => layer.appendChild(ch.cloneNode(true)));
    } else {
      Array.from(incoming.children).forEach(ch => layer.appendChild(ch.cloneNode(true)));
    }
    scaleG.appendChild(layer);
    const nParts = r.headers.get('X-Region-Parts') || '?';
    const seconds = r.headers.get('X-Region-Seconds') || '?';
    btn.textContent = `✓ ${nParts} parts in ${seconds}s`;
    $('btn-detail-clear').style.display = '';
    setTimeout(() => { btn.disabled = false; btn.textContent = orig; }, 2500);
  } catch (e) {
    btn.textContent = '✗ ' + (e.message || 'failed');
    setTimeout(() => { btn.disabled = false; btn.textContent = orig; }, 3000);
  }
}
$('btn-detail').addEventListener('click', detailRenderActive);
$('btn-detail-clear').addEventListener('click', () => {
  const svg = activeSvg();
  if (!svg) return;
  svg.querySelector('g.layer-region-detail')?.remove();
  $('btn-detail-clear').style.display = 'none';
});

// ---- Auto-sharpen on zoom (level-of-detail) ------------------------------
// When the user zooms into the live editor pane past a threshold, re-render
// just the visible region at finer tessellation (via /api/render_region --
// same backend path as the manual "sharpen" button) and overlay it as a
// screen-only <g class="layer-region-detail">.  Zooming back out removes the
// overlay so the clean base shows again.  The base render, the saved figure,
// and the IFU export are ALL unaffected -- the overlay is display-only and
// is stripped on export.  Self-contained (doesn't rely on fileSel/viewSel)
// so it works inside the editor's live pane.
let _autoSharpenTimer = null;
let _autoSharpenInFlight = false;
let _lastSharpenSig = '';
const _AUTO_SHARPEN_SCALE = 2.2;   // start sharpening once zoomed >2.2x

function _clearRegionDetail() {
  const svg = activeSvg();
  if (svg) svg.querySelector('g.layer-region-detail')?.remove();
  const clr = document.getElementById('btn-detail-clear');
  if (clr) clr.style.display = 'none';
  _lastSharpenSig = '';
}

async function autoSharpenViewport(pane) {
  const svg = pane && pane.querySelector('svg');
  if (!svg || typeof API_BASE !== 'string') return;
  const fid = window.IFU_VIEWER?.getActiveFileId?.();
  if (!fid) return;
  const vb = (svg.getAttribute('viewBox') || '').split(/\s+/).map(parseFloat);
  if (vb.length !== 4 || vb.some(isNaN)) return;
  const st = getState(pane.dataset.file, pane.dataset.view);
  if (!st) return;
  const sc = st.scale || 1, tx = st.tx || 0, ty = st.ty || 0;
  // Visible region in viewBox coords, then flip y -> projector (u,v) to
  // match the scale(1,-1) wrapper (identical math to detailRenderActive).
  const vw = vb[2] / sc, vh = vb[3] / sc;
  const vx = vb[0] - tx / sc, vy = vb[1] - ty / sc;
  const bbox_uv = [vx, -(vy + vh), vx + vw, -vy];
  const ctx = window.IFU_VIEWER?._getLiveCamCtx?.(fid);
  if (!ctx || !ctx.eye || !ctx.target) return;   // need a live camera
  const body = {
    file_id: fid, bbox_uv,
    mesh_defl: 0.3, sample_defl: 0.3,
    eye: ctx.eye, target: ctx.target,
  };
  if (ctx.up_axis) body.up_axis = ctx.up_axis;
  const r = await fetch(API_BASE + '/api/render_region', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) return;
  const text = await r.text();
  const incoming = new DOMParser()
    .parseFromString(text, 'image/svg+xml').documentElement;
  const scaleG = svg.querySelector('g[transform="scale(1,-1)"]')
              || svg.querySelector('.view-transform > g')
              || svg.querySelector(':scope > g');
  if (!scaleG) return;
  scaleG.querySelector(':scope > g.layer-region-detail')?.remove();
  const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  layer.setAttribute('class', 'layer-region-detail');
  const inScale = incoming.querySelector('g[transform="scale(1,-1)"]');
  (inScale ? Array.from(inScale.children) : Array.from(incoming.children))
    .forEach(ch => layer.appendChild(ch.cloneNode(true)));
  scaleG.appendChild(layer);
  const clr = document.getElementById('btn-detail-clear');
  if (clr) clr.style.display = '';
}

function _scheduleAutoSharpen(pane) {
  // Editor live pane only -- that's where users zoom to inspect detail.
  if (!pane || pane.dataset.view !== '__live__') return;
  if (document.body.classList.contains('no-auto-sharpen')) return;
  if (_autoSharpenTimer) clearTimeout(_autoSharpenTimer);
  _autoSharpenTimer = setTimeout(() => {
    _autoSharpenTimer = null;
    const st = getState(pane.dataset.file, pane.dataset.view);
    if (!st) return;
    if (st.scale < _AUTO_SHARPEN_SCALE) { _clearRegionDetail(); return; }
    // Quantise scale+pan so tiny nudges don't re-fire an expensive render.
    const sig = [Math.round(st.scale * 4),
                 Math.round(st.tx), Math.round(st.ty)].join(',');
    if (sig === _lastSharpenSig || _autoSharpenInFlight) return;
    _autoSharpenInFlight = true;
    _lastSharpenSig = sig;
    Promise.resolve(autoSharpenViewport(pane))
      .catch(() => { _lastSharpenSig = ''; })
      .finally(() => { _autoSharpenInFlight = false; });
  }, 450);
}

$('btn-export').onclick = () => {
  const svg = activeSvg();
  if (!svg) return;
  const clone = svg.cloneNode(true);
  // Stripping the SVG's hide-* classes used to drop layer-visibility
  // state on export.  Keep them so the same CSS rules apply, then
  // ALSO physically remove hidden <g class="layer-X"> wrappers so the
  // export is correct even in viewers that don't honour stylesheets.
  // (Annotate-mode and the dev hide-* classes don't matter here --
  // leave them in, they're harmless in a free-standing SVG.)
  // Reset any pan/zoom the user did in the live pane.  The exported
  // figure should show the natural viewBox extent, not whatever the
  // user dragged to mid-edit.  applyTransform wraps content in a
  // <g class="view-transform"> -- collapse it back to identity.
  const viewG = clone.querySelector('g.view-transform');
  if (viewG) {
    viewG.removeAttribute('transform');
  }

  // Drop the on-screen zoom-sharpen overlay: it only covers whatever
  // region was visible while zoomed in, so it must NOT bleed into the
  // exported full-figure drawing (which would then be fine in one patch
  // and base elsewhere).  Export is always the clean, consistent base.
  clone.querySelectorAll('g.layer-region-detail').forEach(g => g.remove());

  // Physically drop any <g class="layer-X"> whose visibility
  // checkbox is unchecked.  Otherwise the exported SVG inherits
  // the source's full set of layers regardless of what the user
  // turned off in the viewer.
  document.querySelectorAll('input[data-layer]').forEach(cb => {
    if (cb.checked) return;
    const cat = cb.dataset.layer;
    clone.querySelectorAll(`g.layer-${cat}`).forEach(g => g.remove());
  });

  // The drawing-weight + per-part style overrides live in the document
  // head as <style> blocks targeting .svg-pane[data-file=...] selectors.
  // A cloned, free-standing SVG won't sit inside that pane so those
  // rules never match.  Bake the user's current weights into the SVG
  // in two ways for maximum portability:
  //   1. Patch the <g class="layer ..."> wrappers' inline stroke-width
  //      so even SVG viewers that don't honour <style> see the right
  //      weights (Illustrator's "ignore stylesheets" mode, image
  //      previewers etc).
  //   2. Embed a <style> element inside the SVG carrying the same
  //      rules + a <rect> background, so anything that DOES honour
  //      stylesheets renders identically to what's on screen.
  try {
    const settings = _readDrawSettingsFromUI();
    const lineCol = _LINE_COLOR_VALUES[settings.line_color] || '#000000';
    const paperCol = _PAPER_COLORS[settings.paper] || '#ffffff';
    const k = settings.contrast;
    const widthFor = {
      outline_v:           settings.outline_w * k,
      assembly_silhouette: settings.outline_w * k,
      sharp_v:             settings.sharp_w * k,
      smooth_v:            settings.smooth_w * k,
      hidden_outline:      settings.hidden_w * k,
      hidden_sharp:        settings.hidden_w * k,
    };
    // Patch each <g class="layer layer-X"> wrapper.  Class scheme:
    // the bake emits TWO separate classes per group ("layer" +
    // "layer-<cat>"), so we look for "layer-<cat>" specifically.
    for (const cat of Object.keys(widthFor)) {
      clone.querySelectorAll(`g.layer-${cat}`).forEach(g => {
        g.setAttribute('stroke-width', widthFor[cat].toFixed(3));
        if (cat === 'outline_v' || cat === 'sharp_v'
            || cat === 'assembly_silhouette') {
          g.setAttribute('stroke', lineCol);
        }
        if (cat === 'smooth_v' && settings.smooth_alpha < 1) {
          g.setAttribute('opacity', settings.smooth_alpha.toFixed(2));
        }
      });
    }

    // Paint a background rect first so the SVG isn't transparent in
    // viewers that don't render <style> (and so the exported file
    // matches the chosen Paper colour).  width/height pulled from
    // viewBox so it covers everything regardless of pan/zoom.
    const vb = (clone.getAttribute('viewBox') || '').trim();
    if (vb) {
      const nums = vb.split(/[\\s,]+/).map(parseFloat)
                       .filter(n => Number.isFinite(n));
      if (nums.length === 4) {
        const [vx, vy, vw, vh] = nums;
        const bg = document.createElementNS(
          'http://www.w3.org/2000/svg', 'rect');
        bg.setAttribute('x', String(vx));
        bg.setAttribute('y', String(vy));
        bg.setAttribute('width',  String(vw));
        bg.setAttribute('height', String(vh));
        bg.setAttribute('fill', paperCol);
        bg.setAttribute('data-export', 'paper');
        clone.insertBefore(bg, clone.firstChild);
      }
    }

    // Embed an in-SVG <style> as well so highlight + selection-styling
    // rules (set on the document head as #per-part-styles + the
    // highlight overlay paths) make it across.  We hand-roll a small
    // CSS block that mirrors the head <style> blocks scoped to "svg"
    // (no .svg-pane prefix because the clone won't have it).
    const docHeadCss = [
      document.getElementById('per-part-styles'),
      document.getElementById('draw-settings-style'),
    ].map(el => el ? el.textContent : '').join('\\n');
    // Strip the .svg-pane[...] selector prefixes -- they won't match
    // a free-standing SVG.  Cheap, regex-based: replace any prefix up
    // to "svg " with empty so " svg .layer.X path { ... }" becomes
    // ".layer.X path { ... }".
    let scoped = docHeadCss
      .replace(/\\.svg-pane\\[[^\\]]*\\]\\s*svg\\s+/g, '')
      .replace(/\\.svg-pane\\[[^\\]]*\\]\\s*/g, '');
    // Consistent line weight across views: vector-effect:non-scaling-
    // stroke makes every stroke render at a CONSTANT thickness in the
    // final output regardless of how large/small this view is placed in
    // the IFU.  Without it, the mm-based widths scale with each view's
    // placement size, so the same "0.7mm" looks thicker in a zoomed-in
    // view than a zoomed-out one.  With it, the weight numbers (set via
    // the drawing-weight sliders) give identical thickness on every
    // view's exported drawing.  Belt-and-braces: also set the attribute
    // on each path for viewers that honour attributes but not <style>.
    const NSS = (window._nonScalingStroke !== false);   // default ON
    if (NSS) {
      scoped += '\\nsvg path, svg line, svg polyline '
              + '{ vector-effect: non-scaling-stroke; }';
      clone.querySelectorAll('path, line, polyline').forEach(p => {
        p.setAttribute('vector-effect', 'non-scaling-stroke');
      });
    }
    if (scoped.trim()) {
      const styleEl = document.createElementNS(
        'http://www.w3.org/2000/svg', 'style');
      styleEl.textContent = scoped;
      clone.insertBefore(styleEl, clone.firstChild);
    }
  } catch (e) {
    console.warn('[export] could not inline draw settings:', e);
  }

  const xml = new XMLSerializer().serializeToString(clone);
  const blob = new Blob([
    '<?xml version="1.0" encoding="UTF-8"?>\n', xml
  ], {type: 'image/svg+xml'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${fileSel.value}_${viewSel.value}_annotated.svg`;
  a.click();
};

// --- Onshape feature tree sidebar -----------------------------------------
// Renders the live instance tree pulled from Onshape into the left sidebar.
// Click a leaf-Part to highlight the matching STEP solid (in both the 2D
// SVG view and the 3D view-finder).  v1 mapping: i-th leaf Part in tree
// order <-> i-th solid in STEP order (positional, since cadquery's STEP
// importer drops Onshape part names).

const treeRoot = $('tree-root');
const treeStatus = $('tree-status');
let _tree_id_counter = 0;
let _tree_idmap = {};        // tree-node-id -> tree-node-object
let _tree_to_part_idx = {};  // tree-node-id -> solid idx (or null)
let _leafByPartIdx = new Map(); // solid idx -> leaf tree node (for grouping)

function _flattenLeaves(nodes, out) {
  for (const n of nodes || []) {
    if (n.type === 'Part') out.push(n);
    else if (n.children && n.children.length) _flattenLeaves(n.children, out);
  }
}

// Stamp every tree node with a back-pointer to its parent so the
// "expand to parent group" operation can walk upward without recursing
// the whole tree per call.
function _annotateParents(nodes, parent) {
  for (const n of nodes || []) {
    n._parent = parent || null;
    if (n.children && n.children.length) _annotateParents(n.children, n);
  }
}

function refreshTree() {
  treeRoot.innerHTML = '';
  _tree_idmap = {};
  _tree_to_part_idx = {};
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  const tree = ONSHAPE_TREES[fileSel.value];
  if (!tree || !tree.length) {
    treeStatus.textContent = 'No tree for this source.';
    return;
  }
  // positional leaf->solid map + parent-back-pointers.  Each leaf may
  // map to MULTIPLE solid indices (multi-body STEP Part); for Onshape
  // trees the API gives us one idx per leaf, but for STEP trees the
  // server pre-computes _solid_indices as a contiguous range.
  _annotateParents(tree, null);
  const leaves = [];
  _flattenLeaves(tree, leaves);
  let cursor = 0;
  leaves.forEach((leaf, i) => {
    if (Array.isArray(leaf._solid_indices) && leaf._solid_indices.length) {
      // STEP-tree leaf: server already attached the index range
      leaf._mapped_idx = leaf._solid_indices[0];
    } else if (i < fe.parts.length) {
      leaf._mapped_idx = fe.parts[i].idx;
      leaf._solid_indices = [leaf._mapped_idx];
      cursor = i + 1;
    } else {
      leaf._mapped_idx = null;
      leaf._solid_indices = [];
    }
  });
  // Reverse map: any solid idx -> tree node (so 3D click can find its
  // sub-assembly).  Each idx in _solid_indices points back to the leaf.
  _leafByPartIdx = new Map();
  for (const leaf of leaves) {
    for (const idx of (leaf._solid_indices || [])) {
      _leafByPartIdx.set(idx, leaf);
    }
  }
  const totalBodies = leaves.reduce(
    (s, l) => s + (l._solid_indices ? l._solid_indices.length : 0), 0);
  treeStatus.textContent =
    `${leaves.length} part instances, ${totalBodies} bodies. ` +
    `Click an Assembly to select everything under it.`;

  function buildNode(n) {
    const id = String(++_tree_id_counter);
    _tree_idmap[id] = n;
    if (n.type === 'Part' && n._mapped_idx != null) {
      _tree_to_part_idx[id] = n._mapped_idx;
    }
    const li = document.createElement('li');
    const row = document.createElement('div');
    row.className = 'tree-row' +
      (n.type === 'Assembly' ? ' is-assembly' : '') +
      (n._mapped_idx != null ? ' matched' : '');
    row.dataset.treeId = id;
    const hasKids = (n.children && n.children.length) > 0;
    const twisty = document.createElement('span');
    twisty.className = 'twisty';
    twisty.textContent = hasKids ? '▾' : ' ';
    const icon = document.createElement('span');
    icon.className = 'icon';
    icon.textContent = n.type === 'Assembly' ? '⊞' : '·';
    const lbl = document.createElement('span');
    lbl.textContent = n.name;
    row.appendChild(twisty); row.appendChild(icon); row.appendChild(lbl);
    li.appendChild(row);
    if (hasKids) {
      const ul = document.createElement('ul');
      n.children.forEach(c => ul.appendChild(buildNode(c)));
      li.appendChild(ul);
      twisty.addEventListener('click', ev => {
        ev.stopPropagation();
        const collapsed = ul.style.display === 'none';
        ul.style.display = collapsed ? '' : 'none';
        twisty.textContent = collapsed ? '▾' : '▸';
      });
    }
    row.addEventListener('click', (ev) => {
      const node = _tree_idmap[id];
      const append = ev.ctrlKey || ev.metaKey;
      if (!node) return;
      // Gather all the solid indices this row represents.  Part leaves
      // can have multiple solids (multi-body STEP Part); Assemblies pull
      // in every leaf descendant's full index range.
      let indices = [];
      if (node.type === 'Part') {
        indices = (node._solid_indices && node._solid_indices.length)
          ? node._solid_indices.slice()
          : (_tree_to_part_idx[id] != null ? [_tree_to_part_idx[id]] : []);
      } else {
        const leaves = [];
        _flattenLeaves([node], leaves);
        for (const l of leaves) {
          for (const i of (l._solid_indices || [])) indices.push(i);
        }
      }
      if (!indices.length) return;
      const st = getState(fileSel.value, viewSel.value);
      if (!st.highlights) st.highlights = new Set();
      if (!append) st.highlights.clear();
      indices.forEach(i => st.highlights.add(i));
      applyHighlights();
    });
    return li;
  }
  tree.forEach(n => treeRoot.appendChild(buildNode(n)));
}

// Inject a freshly-rendered SVG (from the local server's /api/render) as a
// "live" view for the given source.  Per-source: each source has its own
// __live__ slot that gets overwritten on every generate.
// camera context (eye/target/up_axis) attached when a Live render fires;
// the silhouette endpoint reuses these so the per-part HLR projects into
// the EXACT same (u,v) space as the baked SVG.
const _liveCamCtx = {};  // file_id -> {eye, target, up_axis}
function _setLiveCamCtx(file_id, ctx) { _liveCamCtx[file_id] = ctx; }
function _getLiveCamCtx(file_id) { return _liveCamCtx[file_id] || null; }

// Tiny non-cryptographic hash (FNV-1a 32-bit) for SVG short-circuiting.
// Identical input -> identical output, ~3 ms for 2.5 MB on a laptop.
function _svgHash(s) {
  let h = 0x811c9dc5 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16);
}

function injectLiveSVG(file_id, view_dir, svgText) {
  // Strip any XML prolog and stamp an id on the <svg> so existing helpers
  // (applyTransform / attachInteractivity) can find it.
  const cleaned = svgText
    .replace(/<\\?xml[^>]*\\?>\\s*/, '')
    .replace('<svg', `<svg id="svg_${file_id}___live__"`);

  // PERF: variant-strip switches between figures that share the SAME view
  // camera produce byte-identical SVG every time.  Re-parsing 2.5 MB of
  // markup just to drop the highlight overlay is wasteful.  Hash the
  // incoming SVG; if the live pane is already showing it, skip the
  // innerHTML replacement (and the cache invalidations -- the cache is
  // still valid for THIS camera).
  const incomingHash = _svgHash(cleaned);
  const existingPane = document.querySelector(
    `.svg-pane[data-file="${file_id}"][data-view="__live__"]`
  );
  const sameContent = existingPane &&
                       existingPane.dataset.svgHash === incomingHash &&
                       existingPane.querySelector('svg');
  if (sameContent) {
    // Make sure it's the active pane and the camera context is current.
    document.querySelectorAll('.svg-pane.active')
            .forEach(p => p.classList.remove('active'));
    existingPane.classList.add('active');
    // Keep view_dir on the CATALOGUE entry up to date for whatever
    // caller is about to read it.
    const fe = CATALOGUE.find(x => x.file_id === file_id);
    if (fe) {
      const ve = fe.views.find(v => v.view_id === '__live__');
      if (ve) ve.view_dir = view_dir;
    }
    return;
  }

  // CRITICAL: every cached overlay keyed by (file_id, '__live__') is
  // tied to the camera the previous render used.  We're about to swap
  // in geometry from a DIFFERENT camera, so the stored polylines no
  // longer correspond to the new SVG's pixel space.  If we don't drop
  // them, the next applyHighlights() will paint last-camera footprints
  // onto this-camera SVG -- closed loops in the wrong place.
  const vid = '__live__';
  for (const k of Array.from(_footprintCache.keys())) {
    if (k.startsWith(file_id + '|' + vid + '|')) _footprintCache.delete(k);
  }
  for (const k of Array.from(_trueSilCache.keys())) {
    if (k.startsWith(file_id + '|' + vid + '|')) _trueSilCache.delete(k);
  }
  for (const k of Array.from(_groupSilCache.keys())) {
    if (k.startsWith(file_id + '|' + vid + '|')) _groupSilCache.delete(k);
  }
  // Assembly silhouette cache is keyed by view -- drop it when the
  // live camera changes so the new render fetches a fresh outline.
  _assemblySilhouetteCache.delete(_asKey(file_id, vid));
  _footprintViewFetched.delete(_fpViewKey(file_id, vid));
  // Force the next fetchSelected* to re-fetch even if the selection
  // didn't change between renders.
  _lastSilHighlightSig = '__force_refetch__';

  // Re-use or create the live pane for this source.
  let pane = existingPane;
  if (!pane) {
    pane = document.createElement('div');
    pane.className = 'svg-pane';
    pane.dataset.file = file_id;
    pane.dataset.view = '__live__';
    pane.dataset.svgId = `svg_${file_id}___live__`;
    canvasWrap.appendChild(pane);
  }
  pane.innerHTML = cleaned;
  pane.dataset.svgHash = incomingHash;
  // attached flag must be cleared so attachInteractivity rewires the new svg
  pane.querySelector('svg')?.removeAttribute('data-attached');

  // Mine the SVG for its part indices.  Dynamic Onshape sources have
  // no baked CATALOGUE parts list, but the SVG itself contains
  //   <g class="part part-NNN" data-part="N">
  // for every part with visible geometry.  Reading those means we
  // can prefetch the assembly footprint raster ahead of the user's
  // first click -- otherwise that first click eats a ~46s wait
  // before the closed-loop outline appears.
  try {
    const ids = new Set();
    pane.querySelectorAll('[data-part]').forEach(g => {
      const n = parseInt(g.dataset.part, 10);
      if (Number.isFinite(n)) ids.add(n);
    });
    if (ids.size) {
      // Update / create the CATALOGUE entry's parts list so
      // refreshPartList + prefetchFootprintsForCurrentView find them.
      let cf = CATALOGUE.find(x => x.file_id === file_id);
      if (!cf) {
        cf = { file_id, file_label: file_id, parts: [], views: [] };
        CATALOGUE.push(cf);
      }
      const sorted = [...ids].sort((a, b) => a - b);
      cf.parts = sorted.map(idx => ({ idx, label: 'part_'
                                          + String(idx).padStart(3, '0') }));
    }
  } catch (_e) {}

  // Add or update the "Live" option in the View dropdown (per-source).
  // Dynamic Onshape imports aren't in the baked CATALOGUE -- create
  // a stub entry on the fly so refreshViews() can populate the View
  // dropdown and refreshPane() can find the newly-injected pane.
  let fe = CATALOGUE.find(x => x.file_id === file_id);
  if (!fe) {
    fe = {
      file_id: file_id,
      file_label: file_id,
      parts: [],
      views: [],
    };
    CATALOGUE.push(fe);
  }
  let existing = fe.views.find(v => v.view_id === '__live__');
  if (existing) {
    existing.view_dir = view_dir;
  } else {
    fe.views.push({
      view_id: '__live__',
      label: '⚡ Live (from 3D)',
      view_dir: view_dir,
    });
  }
  // Refresh the View dropdown if this is the active source.  If the
  // fileSel value doesn't match (route switched between variants of
  // different views, dropdown stale), force it before refreshing so
  // we don't leave the user with a blank pane.
  if (fileSel.value !== file_id) {
    const hasOpt = Array.from(fileSel.options)
                          .some(o => o.value === file_id);
    if (!hasOpt) {
      const opt = document.createElement('option');
      opt.value = file_id; opt.textContent = file_id;
      fileSel.appendChild(opt);
    }
    fileSel.value = file_id;
  }
  refreshViews();
  viewSel.value = '__live__';
  refreshPane();
  // Defensive: even if refreshPane couldn't find the pane via
  // activePane(), force the freshly-injected one to be the active
  // (.active) one.  CSS keeps the rest at display:none, so a missing
  // .active class is the exact "nothing shows in main view" symptom
  // the user reported.
  document.querySelectorAll('.svg-pane.active')
          .forEach(p => p.classList.remove('active'));
  pane.classList.add('active');
  // Make sure pan/zoom transform is reset to identity for the freshly
  // injected SVG so prior state (zoomed in, panned off-screen) from
  // a different camera doesn't leave the new geometry invisible.
  try {
    const st = getState(file_id, '__live__');
    if (st) { st.tx = 0; st.ty = 0; st.scale = 1; }
    applyTransform(pane);
  } catch (_e) {}
  // Force layout to split / 2D mode so the SVG is visible (some
  // route entries leave the user on layout-3d).
  if (typeof setLayout === 'function') {
    document.body.classList.contains('layout-3d') && setLayout('split');
  }
  // Fire the assembly-wide footprint raster in the background so the
  // user's FIRST click on a part gets a closed-loop outline instantly
  // -- not the 46-second wait that made the outline look "stuck on
  // partial open polylines".  The endpoint memoises per-view so this
  // pays the raster cost once; further selections in the same view
  // hit the cache.
  if (typeof prefetchFootprintsForCurrentView === 'function') {
    setTimeout(() => prefetchFootprintsForCurrentView(), 100);
  }
  // If region-colour debug mode is on, re-apply colours to the new
  // SVG markup that just got injected (inline-style stroke would
  // otherwise be wiped by the innerHTML replace).
  if (document.body.classList.contains('show-part-regions')
      && typeof window._toggleRegionMode === 'function') {
    setTimeout(() => _applyRegionColours(), 50);
  }
}

// Expose for the module script (cross-script comms)
window.IFU_VIEWER = {
  togglePartHighlight,
  clearHighlights,
  injectLiveSVG,
  setLayout: (name) => setLayout(name),
  getActiveFileId: () => fileSel.value,
  getActiveViewDir: () => {
    const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
    const ve = fe?.views.find(v => v.view_id === viewSel.value);
    return ve?.view_dir;
  },
  getActiveUpAxis: () => UP_AXIS_ROT[upAxisSel.value],
  onFileChange: (cb) => fileSel.addEventListener('change', cb),
  onViewChange: (cb) => viewSel.addEventListener('change', cb),
  // Renderer perf telemetry -- populated by the three.js animate() loop
  // every ~0.5s.  Returns {fps, frameMs} or null before any frames
  // have been rendered.  Useful for the ?dbg=1 HUD and tests.
  getRendererState: () => {
    const s = window.IFU_VIEWER_STATE || {};
    if (s.fps == null) return null;
    return { fps: s.fps, frameMs: s.frameMs };
  },
  _setLiveCamCtx,
  _getLiveCamCtx,
};

// Stream FPS to the perf HUD when ?dbg=1 -- this gives the user the
// missing "why did rendering just get sluggish" signal.
if (_DBG_ON) {
  setInterval(() => {
    const st = window.IFU_VIEWER_STATE || {};
    if (st.fps != null) {
      _dbgLine('renderer', st.frameMs, st.fps.toFixed(0) + ' fps');
    }
  }, 1000);
}

// Tree refresh on source change
fileSel.addEventListener('change', refreshTree);

// --- Tree search ------------------------------------------------------------
// Live-filter the tree as the user types. Matches any name (substring,
// case-insensitive); shows matching leaves and their ancestor path so the
// hierarchy stays readable. Empty query = show all.
const treeSearch = $('tree-search');
function filterTree(q) {
  q = (q || '').trim().toLowerCase();
  const allLi = treeRoot.querySelectorAll('li');
  if (!q) {
    allLi.forEach(li => li.classList.remove('filtered-out'));
    return;
  }
  // First pass: mark every li as filtered-out
  allLi.forEach(li => li.classList.add('filtered-out'));
  // Second pass: for each li whose name matches, un-filter it AND all ancestor li's
  allLi.forEach(li => {
    const row = li.querySelector(':scope > .tree-row');
    if (!row) return;
    const name = row.textContent.toLowerCase();
    if (name.includes(q)) {
      let cur = li;
      while (cur && cur.classList.contains('filtered-out')) {
        cur.classList.remove('filtered-out');
        cur = cur.parentElement?.closest('li');
      }
      // Also reveal direct descendants of a matched node so the user sees
      // what's inside the matched subtree.
      li.querySelectorAll('li').forEach(d => d.classList.remove('filtered-out'));
    }
  });
}
treeSearch.addEventListener('input', () => filterTree(treeSearch.value));
// Esc inside the search clears it (separate from the global Esc which
// clears selection -- only act on Esc if the search is focused and has content)
treeSearch.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && treeSearch.value) {
    treeSearch.value = '';
    filterTree('');
    e.stopPropagation();   // don't bubble to the global Esc-clears-selection
  }
});

// --- Layout (2D / Split / 3D) ----------------------------------------------
// Three-segment control replacing the old hidden "3D view-finder" toggle.
// Body class drives the grid (grid-template-areas reflow between layouts);
// the module script wakes / sleeps three.js based on whether the WebGL
// pane is currently visible.

const LAYOUTS = ['2d', 'split', '3d'];
let currentLayout = '2d';
function setLayout(name) {
  if (!LAYOUTS.includes(name)) return;
  currentLayout = name;
  document.body.classList.remove('layout-2d', 'layout-split', 'layout-3d');
  document.body.classList.add('layout-' + name);
  ['lay-2d', 'lay-split', 'lay-3d'].forEach(id => {
    $(id).classList.toggle('active', id === 'lay-' + name);
  });
  // tell three.js to (de)activate
  const show3d = (name === 'split' || name === '3d');
  window.IFU_VIEWER?.set3DActive?.(show3d);
}
$('lay-2d').addEventListener('click', () => setLayout('2d'));
$('lay-split').addEventListener('click', () => setLayout('split'));
$('lay-3d').addEventListener('click', () => setLayout('3d'));

// ---- Splitter drag (resize 2D / 3D panes in split layout) ----------
// The grid uses two CSS variables (--split-2d, --split-3d) for the
// two centre columns; dragging the splitter writes both to body
// style so the panes resize live.  Width is persisted to
// localStorage so the choice survives reloads.  Double-click resets.
(function setupPaneSplitter() {
  const splitter = document.getElementById('pane-splitter');
  if (!splitter) return;
  const STORAGE_KEY = 'ifu:split_2d_fraction';
  const MIN_FRACTION = 0.15;   // each pane keeps at least 15% of the central band

  function applyFraction(f) {
    const a = Math.max(MIN_FRACTION, Math.min(1 - MIN_FRACTION, f));
    document.body.style.setProperty('--split-2d', a + 'fr');
    document.body.style.setProperty('--split-3d', (1 - a) + 'fr');
    localStorage.setItem(STORAGE_KEY, a.toFixed(4));
    // Notify three.js so its renderer can resize its viewport
    setTimeout(() => {
      window.IFU_VIEWER?.resize3D?.();
    }, 0);
  }

  // Restore saved fraction (default 50/50)
  let saved = parseFloat(localStorage.getItem(STORAGE_KEY));
  if (!Number.isFinite(saved)) saved = 0.5;
  applyFraction(saved);

  let dragging = false;
  let startX = 0;
  let startA = 0.5;
  let totalCentralPx = 0;

  splitter.addEventListener('pointerdown', (e) => {
    if (!document.body.classList.contains('layout-split')) return;
    dragging = true;
    splitter.classList.add('is-dragging');
    startX = e.clientX;
    startA = parseFloat(localStorage.getItem(STORAGE_KEY)) || 0.5;
    // Measure the combined central band width (canvas-wrap + splitter
    // + webgl-wrap) so drag distance converts to fraction reliably
    // regardless of side-panel widths.
    const cw = document.getElementById('canvas-wrap');
    const ww = document.getElementById('webgl-wrap');
    totalCentralPx = (cw ? cw.getBoundingClientRect().width : 0)
                    + splitter.getBoundingClientRect().width
                    + (ww ? ww.getBoundingClientRect().width : 0);
    e.preventDefault();
  });
  // pointermove + pointerup live on the DOCUMENT so the drag tracks
  // even when the cursor moves off the splitter (the standard
  // resize-handle pattern -- much more reliable than setPointerCapture
  // which doesn't work for synthetic events in tests).
  document.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    if (totalCentralPx <= 0) return;
    const f = startA + dx / totalCentralPx;
    applyFraction(f);
  });
  function _endDrag() {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('is-dragging');
  }
  document.addEventListener('pointerup', _endDrag);
  document.addEventListener('pointercancel', _endDrag);

  // Double-click resets to 50/50
  splitter.addEventListener('dblclick', () => applyFraction(0.5));
})();

// --- Saved views --------------------------------------------------------
// Per-source list of {name, eye, target, up_axis} kept in localStorage so
// the camera angles a user has dialled in survive reloads.  No server
// involvement -- recall just snaps the 3D camera + Up: dropdown.

function _savedViewsKey(fid) { return 'savedViews_' + fid; }
function loadSavedViews(fid) {
  try {
    return JSON.parse(localStorage.getItem(_savedViewsKey(fid)) || '[]');
  } catch (_e) { return []; }
}
function persistSavedViews(fid, list) {
  localStorage.setItem(_savedViewsKey(fid), JSON.stringify(list));
}
function refreshSavedViews() {
  const ul = $('saved-views');
  ul.innerHTML = '';
  const list = loadSavedViews(fileSel.value);
  if (!list.length) {
    ul.innerHTML = '<li style="color:var(--muted); font-style:italic;">' +
                   'none yet — orbit the 3D, then click save</li>';
    return;
  }
  list.forEach((v, i) => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = v.name;
    name.title = 'click to recall';
    name.addEventListener('click', () => recallSavedView(v));
    const del = document.createElement('button');
    del.textContent = '×';
    del.title = 'delete';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      const cur = loadSavedViews(fileSel.value);
      cur.splice(i, 1);
      persistSavedViews(fileSel.value, cur);
      refreshSavedViews();
    });
    li.appendChild(name); li.appendChild(del);
    ul.appendChild(li);
  });
}
function recallSavedView(v) {
  // Make sure 3D is visible so OrbitControls can move
  if (!is3DCurrentlyShown()) setLayout('split');
  // Apply Up: rotation if different
  if (v.up_axis && upAxisSel.value !== v.up_axis) {
    upAxisSel.value = v.up_axis;
    upAxisSel.dispatchEvent(new Event('change'));
  }
  window.IFU_VIEWER?.snapCameraTo?.(v.eye, v.target);
}
function is3DCurrentlyShown() {
  return document.body.classList.contains('layout-split')
      || document.body.classList.contains('layout-3d');
}
$('btn-save-view').addEventListener('click', () => {
  const nameInput = $('view-name');
  const name = (nameInput.value || '').trim();
  if (!name) { nameInput.focus(); return; }
  const cam = window.IFU_VIEWER?.getCameraEyeTarget?.();
  if (!cam) { alert('Open the 3D pane first.'); return; }
  const entry = {
    name,
    eye:    cam.eye,
    target: cam.target,
    up_axis: upAxisSel.value,
  };
  const cur = loadSavedViews(fileSel.value);
  // Replace any same-named entry
  const existing = cur.findIndex(v => v.name === name);
  if (existing >= 0) cur[existing] = entry;
  else cur.push(entry);
  persistSavedViews(fileSel.value, cur);
  nameInput.value = '';
  refreshSavedViews();
});
fileSel.addEventListener('change', refreshSavedViews);

// --- Figures (Phase A) -----------------------------------------------
// A figure is the full editor state (camera + selection + per-part
// styles + layer toggles + notes) saved as a JSON file in
// out/figures/.  Lives on the server, not in localStorage.

const figuresList = document.getElementById('figures-list');

async function listFigures() {
  if (typeof API_BASE !== 'string') return [];
  try {
    const r = await fetch(API_BASE + '/api/figures');
    if (!r.ok) return [];
    return (await r.json()).figures || [];
  } catch (_e) { return []; }
}

function _gatherCurrentState(opts) {
  // Snapshot everything the editor currently shows so we can rehydrate
  // it later from the figure JSON.
  opts = opts || {};
  const fid = fileSel.value;
  const vid = viewSel.value;
  const st = getState(fid, vid);
  // Camera: by default use the loaded figure's OWN camera so autosave
  // (styles/selection edits) never rewrites the angle just because the
  // user orbited.  Pass {liveCamera:true} to PREFER the current orbit
  // camera (forking a new-angle variant), falling back to the stored
  // camera when the 3D pane isn't live.
  const _live = window.IFU_VIEWER?.getCameraEyeTarget?.();
  const _liveCam = _live
    ? { eye: _live.eye, target: _live.target, up_axis: upAxisSel.value }
    : null;
  const cam = opts.liveCamera
    ? (_liveCam || _loadedFigureCamera)
    : (_loadedFigureCamera || _liveCam);
  const layersOn = {};
  document.querySelectorAll('input[data-layer]').forEach(cb => {
    layersOn[cb.dataset.layer] = !!cb.checked;
  });
  // Stamp the source's solid count at save time.  Part styles + the
  // selection are keyed by integer part index, which is the
  // split_solids() enumeration order.  If the source is later
  // reconfigured/re-imported and that order changes, those indices
  // would silently point at different parts -- so we record the count
  // now and warn on load if it no longer matches (see _loadFigureIntoEditor).
  const srcParts = (typeof CATALOGUE !== 'undefined'
    ? CATALOGUE.find(x => x.file_id === fid) : null);
  return {
    source_id: fid,
    view_id: vid,
    camera: cam || null,
    selection: st.highlights ? [...st.highlights] : [],
    styles_per_part: loadPartStyles(fid),
    source_part_count: (srcParts && srcParts.parts)
      ? srcParts.parts.length : null,
    layers_on: layersOn,
    detail: parseFloat($('sty-width').value) >= 5 ? "fine" : "normal",
    annotations: (st.annotations || []),
    // Exploded view + 3D arrows + line-style preset (from the 3D editor).
    ...(() => {
      const a = window.IFU_VIEWER?.getAnnotationState?.() || {};
      return {
        explode: a.explode || {},
        arrows: a.arrows || [],
        preset_id: a.preset_id || null,
      };
    })(),
  };
}

// Clicking "generate 2D" after orbiting should ADD a new variant at the
// new angle (preserving the original figure), not overwrite it.  Forks a
// new figure under the SAME view carrying the live camera + the current
// highlighting, attaches it to the view, and navigates there (the editor
// auto-renders it on open).  Returns false when there's no view/project
// context to fork into (legacy editor) so the caller can fall back to an
// in-place render.
async function _forkNewAngleVariant() {
  const projId = (typeof AppState !== 'undefined') && AppState.currentProjectId;
  const viewId = (typeof AppState !== 'undefined') && AppState.currentViewId;
  if (!projId || !viewId) return false;
  const snap = _gatherCurrentState({ liveCamera: true });
  if (!snap.camera) return false;
  let nextN = 2;
  try {
    const figs = await (await fetch(API_BASE + '/api/views/'
      + encodeURIComponent(viewId) + '/figures')).json();
    nextN = ((figs.figures || []).length) + 1;
  } catch (_e) {}
  const name = 'Angle ' + nextN;
  let f;
  try {
    const r = await fetch(API_BASE + '/api/figures', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        source_id: snap.source_id,
        project_id: projId,
        view_id: snap.view_id,
        camera: snap.camera,
        selection: snap.selection,
        styles_per_part: snap.styles_per_part,
        layers_on: snap.layers_on,
        source_part_count: snap.source_part_count,
      }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    f = await r.json();
    await fetch(API_BASE + '/api/views/' + encodeURIComponent(viewId)
      + '/figures/' + encodeURIComponent(f.id), { method: 'POST' });
  } catch (e) {
    (window.IFU_UI?.toast || function(){})(
      'New-angle variant failed: ' + (e.message || e), 'error');
    return false;
  }
  (window.IFU_UI?.toast || function(){})(
    'Added "' + name + '" at this angle', 'success');
  if (typeof showCanvasLoading === 'function')
    showCanvasLoading('creating ' + name + '...');
  location.hash = '#/project/' + encodeURIComponent(projId)
    + '/view/' + encodeURIComponent(viewId)
    + '/figure/' + encodeURIComponent(f.id);
  return true;
}
window._forkNewAngleVariant = _forkNewAngleVariant;

async function saveCurrentAsFigure() {
  const nameInput = $('fig-name');
  const name = (nameInput.value || '').trim();
  if (!name) { nameInput.focus(); return; }
  const body = { name, ..._gatherCurrentState() };
  const r = await fetch(API_BASE + '/api/figures', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    alert('Save figure failed: ' + r.status);
    return;
  }
  nameInput.value = '';
  refreshFiguresList();
}

function _loadFigureIntoEditor(fig, opts) {
  opts = opts || {};
  // Restore: source -> view -> camera -> selection -> styles -> layers
  // Confirm before clobbering current state -- it's destructive and
  // there's no undo.  Skip the prompt when ``opts.skipConfirm`` (the
  // user JUST clicked into this figure via the project workspace, no
  // ambiguity about intent), or if the editor is in a "fresh" state.
  if (!opts.skipConfirm) {
    const curSt = getState(fileSel.value, viewSel.value);
    const curStyles = loadPartStyles(fileSel.value) || {};
    const hasWork = (curSt.highlights && curSt.highlights.size > 0)
                 || Object.keys(curStyles).length > 0;
    if (hasWork) {
      if (!confirm(`Loading "${fig.name}" will replace the current `
                  + `selection and applied styles.  Continue?`)) return;
    }
  }

  // If the figure's source isn't in the dropdown yet (e.g. a dynamic
  // Onshape import that landed AFTER the page loaded), pull /api/sources
  // and add an option for it.  Otherwise the value assignment below
  // silently no-ops and the user keeps staring at the wrong assembly.
  if (fig.source_id) {
    const hasOpt = Array.from(fileSel.options)
                         .some(o => o.value === fig.source_id);
    if (!hasOpt) {
      const opt = document.createElement('option');
      opt.value = fig.source_id;
      opt.textContent = fig.source_id;   // best-effort label until we
                                          // hear back from /api/sources
      fileSel.appendChild(opt);
      // Fire off a label lookup so the option shows the human name
      fetch(API_BASE + '/api/sources').then(r => r.json()).then(data => {
        const s = (data.sources || []).find(x => x.id === fig.source_id);
        if (s && s.label) opt.textContent = s.label;
      }).catch(() => {});
    }
  }

  if (fig.source_id && fig.source_id !== fileSel.value) {
    fileSel.value = fig.source_id;
    fileSel.dispatchEvent(new Event('change'));
  }
  // Only switch view if the figure's view actually exists for this
  // source.  An unknown id (e.g. saved __live__ from a previous
  // session that's since been wiped) would blank the canvas.
  if (fig.view_id && fig.view_id !== viewSel.value) {
    const valid = Array.from(viewSel.options)
                       .some(o => o.value === fig.view_id);
    if (valid) {
      viewSel.value = fig.view_id;
      viewSel.dispatchEvent(new Event('change'));
    } else {
      console.warn(`figure ${fig.name}: view_id ${fig.view_id} `
                  + `not available on source ${fig.source_id}; `
                  + `keeping current view`);
    }
  }
  if (fig.camera && fig.camera.eye && fig.camera.target) {
    window.IFU_VIEWER?.snapCameraTo?.(fig.camera.eye, fig.camera.target);
    if (fig.camera.up_axis && upAxisSel) {
      upAxisSel.value = fig.camera.up_axis;
      upAxisSel.dispatchEvent(new Event('change'));
    }
  }
  // Remember the figure's OWN camera.  A figure's angle is fixed at
  // creation; merely orbiting the 3D view must NOT rewrite it (the old
  // behaviour let autosave silently overwrite the saved camera on every
  // orbit).  To capture a new angle the user clicks Generate, which
  // forks a NEW variant.  _gatherCurrentState reads this stored camera
  // so autosave never clobbers the original angle.
  _loadedFigureCamera = (fig.camera && fig.camera.eye) ? fig.camera : null;
  const st = getState(fig.source_id, fig.view_id || viewSel.value);
  st.highlights = new Set(fig.selection || []);
  // The figure record is authoritative -- always sync localStorage
  // to what the figure says, including the empty case.  Without this
  // a brand-new variant (empty styles_per_part on the server) would
  // inherit the previous variant's per-part styles because both
  // figures share the same `partStyles_<source_id>` localStorage key.
  // Same logic when the user explicitly clears all styles and switches
  // figures: previous behaviour kept the cleared styles around.
  persistPartStyles(fig.source_id, fig.styles_per_part || {});

  // Shape-drift guard: if the source's solid count changed since this
  // figure was styled, the integer part indices in styles_per_part /
  // selection may now point at DIFFERENT parts.  Warn rather than
  // silently render highlights on the wrong geometry.
  try {
    const figCount = (typeof fig.source_part_count === 'number')
      ? fig.source_part_count : null;
    const srcEntry = (typeof CATALOGUE !== 'undefined')
      ? CATALOGUE.find(x => x.file_id === fig.source_id) : null;
    const curCount = (srcEntry && srcEntry.parts)
      ? srcEntry.parts.length : null;
    const styledCount = Object.keys(fig.styles_per_part || {}).length
      + ((fig.selection || []).length);
    if (figCount != null && curCount != null
        && figCount !== curCount && styledCount > 0) {
      (window.IFU_UI?.toast || function(){})(
        'Model changed since this figure was styled ('
        + figCount + '→' + curCount + ' parts). Highlights may be on '
        + 'the wrong parts — re-check before exporting.', 'error');
      (window._reportClientError || function(){})({
        level: 'warn', op: 'figure.shapeDrift',
        msg: 'part_count ' + figCount + ' -> ' + curCount
             + ' fig=' + (fig.id || '?'),
      });
    }
  } catch (_e) {}

  if (fig.layers_on) {
    document.querySelectorAll('input[data-layer]').forEach(cb => {
      const want = fig.layers_on[cb.dataset.layer];
      if (typeof want === 'boolean' && cb.checked !== want) {
        cb.checked = want;
        cb.dispatchEvent(new Event('change'));
      }
    });
  }
  // Refresh everything that responds to those changes
  applyStyleSheet();
  applyHighlights();

  // Restore exploded view + 3D arrows + line-style preset.  The preset
  // applies immediately (affects the next 2D render); explode/arrows are
  // stashed and re-applied when the 3D source's parts finish indexing.
  window.IFU_VIEWER?.restoreAnnotationState?.({
    explode: fig.explode || {},
    arrows: fig.arrows || [],
    preset_id: fig.preset_id || null,
  });
  // Reflect the restored preset in the figure-tools panel dropdown.
  if (fig.preset_id) window.IFU_ANNOT?.reloadPresets?.(fig.preset_id);

  // Auto-render the 2D base view for figures that came from a View
  // (i.e. they carry a camera).  Without this the user lands in the
  // editor, sees the 3D pane, and has to click "generate 2D" before
  // they can start highlighting -- not what you want for a new figure
  // that's supposed to inherit a parent View's drawing.
  (window._reportClientError || function(){})({
    level: 'info', op: 'editor.autoCheck',
    msg: 'autoGenerate=' + !!opts.autoGenerate
          + ' camera=' + !!(fig.camera && fig.camera.eye && fig.camera.target)
          + ' fn=' + (typeof generateLiveSVGForCamera === 'function'),
  });
  if (opts.autoGenerate && fig.camera && fig.camera.eye && fig.camera.target
      && typeof generateLiveSVGForCamera === 'function') {
    // Show the spinner BEFORE the delay so the user sees something
    // is happening while three.js settles and the render is in
    // flight.  generateLiveSVGForCamera hides it on success.
    if (typeof showCanvasLoading === 'function') {
      showCanvasLoading('rendering view...');
    }
    // Let three.js apply the snapped camera + the up_axis change
    // before the render fires, so the projector axes match what the
    // 3D pane is actually showing.
    setTimeout(() => {
      try { generateLiveSVGForCamera(fig.camera); }
      catch (e) {
        // Report -- this used to swallow silently and the auto-render
        // failure was invisible from outside the browser.
        (window._reportClientError || function(){})({
          level: 'err', op: 'editor.autoRender',
          msg: String((e && e.message) || e),
          stack: (e && e.stack || '').slice(0, 600),
        });
        console.error('[editor] generateLiveSVGForCamera threw:', e);
        if (typeof hideCanvasLoading === 'function') hideCanvasLoading();
      }
    }, 350);
  }
}

// Expose for tests + ad-hoc debugging
window._loadFigureIntoEditor = _loadFigureIntoEditor;

// ---- Loading overlay -----------------------------------------------
// Big spinner that floats over the 2D canvas while a render is in
// flight or a variant switch is loading.  The user reported that
// clicking a variant card felt like nothing was happening -- this is
// the missing feedback.
function _ensureLoadingOverlayStyles() {
  if (document.getElementById('_loading_overlay_styles')) return;
  const s = document.createElement('style');
  s.id = '_loading_overlay_styles';
  s.textContent = `
    .canvas-loading-overlay {
      position: absolute; inset: 0;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 10px;
      background: rgba(255,255,255,0.78);
      z-index: 50;
      font-family: var(--font-ui, Inter, sans-serif);
      color: var(--c-text-muted, #71717a);
      pointer-events: none;
      transition: opacity 0.18s ease;
    }
    .canvas-loading-overlay.is-hidden {
      opacity: 0; pointer-events: none;
    }
    .canvas-loading-spinner {
      width: 36px; height: 36px;
      border: 3px solid #d4d4d8;
      border-top-color: var(--c-accora, #00836a);
      border-radius: 50%;
      animation: canvas-spinner-rot 0.9s linear infinite;
    }
    .canvas-loading-label {
      font-size: 12px; font-weight: 500;
    }
    @keyframes canvas-spinner-rot { to { transform: rotate(360deg); } }
  `;
  document.head.appendChild(s);
}

function showCanvasLoading(label) {
  _ensureLoadingOverlayStyles();
  const wrap = document.getElementById('canvas-wrap');
  if (!wrap) return;
  // Make sure the container is positioned so absolute children land
  // over it -- canvas-wrap is already position:relative.
  let ov = wrap.querySelector(':scope > .canvas-loading-overlay');
  if (!ov) {
    ov = document.createElement('div');
    ov.className = 'canvas-loading-overlay';
    ov.innerHTML = '<div class="canvas-loading-spinner"></div>'
                 + '<div class="canvas-loading-label"></div>';
    wrap.appendChild(ov);
  }
  ov.classList.remove('is-hidden');
  ov.querySelector('.canvas-loading-label').textContent = label || 'loading...';
}

function hideCanvasLoading() {
  const wrap = document.getElementById('canvas-wrap');
  if (!wrap) return;
  const ov = wrap.querySelector(':scope > .canvas-loading-overlay');
  if (ov) ov.classList.add('is-hidden');
}
window.showCanvasLoading = showCanvasLoading;
window.hideCanvasLoading = hideCanvasLoading;


// ---- Shaded-outline loading badge ----------------------------------
// Small pill that floats in the bottom-left of the 2D pane while the
// server is still rastering the assembly footprint.  Avoids the
// "half-baked outline" the user used to see during the ~46s first-
// click delay.  Auto-rolls back to hidden once the closed-loop
// outline is ready.
function _ensureShadedOutlineStyles() {
  if (document.getElementById('_shaded_outline_styles')) return;
  const s = document.createElement('style');
  s.id = '_shaded_outline_styles';
  s.textContent = `
    .shaded-outline-loading {
      position: absolute;
      left: 12px;
      bottom: 12px;
      z-index: 60;
      display: flex; align-items: center; gap: 8px;
      padding: 6px 12px 6px 8px;
      background: rgba(255,255,255,0.95);
      border: 1px solid var(--c-line, #d4d4d8);
      border-radius: 16px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.08);
      font-family: var(--font-ui, Inter, sans-serif);
      font-size: 11px;
      color: var(--c-text, #18181b);
      pointer-events: none;
      transition: opacity 0.18s ease;
    }
    .shaded-outline-loading.is-hidden {
      opacity: 0;
      pointer-events: none;
    }
    .shaded-outline-loading .sp {
      width: 14px; height: 14px;
      border: 2px solid #d4d4d8;
      border-top-color: var(--c-accora, #00836a);
      border-radius: 50%;
      animation: shaded-spinner-rot 0.9s linear infinite;
    }
    @keyframes shaded-spinner-rot { to { transform: rotate(360deg); } }
  `;
  document.head.appendChild(s);
}

function showShadedOutlineLoading(nParts) {
  _ensureShadedOutlineStyles();
  const host = document.getElementById('canvas-wrap');
  if (!host) return;
  let el = host.querySelector(':scope > .shaded-outline-loading');
  if (!el) {
    el = document.createElement('div');
    el.className = 'shaded-outline-loading';
    el.innerHTML = '<div class="sp"></div><span class="lbl"></span>';
    host.appendChild(el);
  }
  el.classList.remove('is-hidden');
  el.querySelector('.lbl').textContent =
      'shaded outline computing for '
    + nParts + ' part' + (nParts === 1 ? '' : 's') + '...';
}
function hideShadedOutlineLoading() {
  const host = document.getElementById('canvas-wrap');
  if (!host) return;
  const el = host.querySelector(':scope > .shaded-outline-loading');
  if (el) el.classList.add('is-hidden');
}
window.showShadedOutlineLoading = showShadedOutlineLoading;
window.hideShadedOutlineLoading = hideShadedOutlineLoading;


// ---- Variant strip (subview mode) ----------------------------------
// Render a vertical strip of small cards, one per figure attached to
// the active View, with thumbnails.  The currently-open figure is
// marked is-active.  A leading "+" card creates a fresh figure under
// the same view (inherits view's camera + source).  Switching cards
// is a route navigation -- auto-save handles persisting the previous
// figure's edits before the swap.
async function _renderVariantStrip(projId, viewId, activeFigId) {
  const strip = document.getElementById('variants-strip');
  if (!strip) return;
  strip.innerHTML = '';

  // The "+" add-card always sits at the top
  const addCard = document.createElement('div');
  addCard.className = 'variant-card add';
  addCard.textContent = '+ new highlight variant';
  addCard.addEventListener('click', async () => {
    // Build a fresh figure name from the variant count
    let view, figs;
    try {
      view = await (await fetch(API_BASE + '/api/views/'
                                  + encodeURIComponent(viewId))).json();
      figs = await (await fetch(API_BASE + '/api/views/'
                                  + encodeURIComponent(viewId)
                                  + '/figures')).json();
    } catch (_e) {
      toast('Failed to load view', 'error');
      return;
    }
    const nextN = ((figs.figures || []).length) + 1;
    const defaultName = 'Variant ' + nextN;
    try {
      const r = await fetch(API_BASE + '/api/figures', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: defaultName,
          source_id: view.source_id,
          project_id: projId,
          view_id: viewId,
          camera: view.camera,
          configuration: view.configuration,
        }),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const f = await r.json();
      await fetch(API_BASE + '/api/views/' + encodeURIComponent(viewId)
                    + '/figures/' + encodeURIComponent(f.id),
                    { method: 'POST' });
      // Visual feedback while the new variant loads
      if (typeof showCanvasLoading === 'function') {
        showCanvasLoading('creating ' + defaultName + '...');
      }
      // Hop to the new variant -- editor will auto-render the view
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(f.id);
    } catch (e) {
      if (typeof hideCanvasLoading === 'function') hideCanvasLoading();
      toast('Create failed: ' + (e.message || e), 'error');
    }
  });
  strip.appendChild(addCard);

  // Fetch the figures under this view
  let figs = [];
  try {
    const r = await fetch(API_BASE + '/api/views/'
                            + encodeURIComponent(viewId) + '/figures');
    if (r.ok) figs = (await r.json()).figures || [];
  } catch (_e) {}

  for (const f of figs) {
    const card = document.createElement('div');
    card.className = 'variant-card'
                   + (f.id === activeFigId ? ' is-active' : '');
    const img = document.createElement('img');
    img.className = 'variant-thumb';
    img.src = API_BASE + '/api/figures/' + encodeURIComponent(f.id)
              + '/thumbnail?v=' + encodeURIComponent(f.updated_at || '');
    img.alt = '';
    img.onerror = () => {
      const ph = document.createElement('div');
      ph.className = 'variant-thumb placeholder';
      img.replaceWith(ph);
    };
    card.appendChild(img);
    const meta = document.createElement('div');
    meta.className = 'variant-meta';
    const nm = document.createElement('div');
    nm.className = 'variant-name';
    nm.textContent = f.name || '(untitled)';
    meta.appendChild(nm);
    const sub = document.createElement('div');
    sub.className = 'variant-sub';
    const sel = (f.selection || []).length;
    sub.textContent = sel + (sel === 1 ? ' part' : ' parts');
    meta.appendChild(sub);
    card.appendChild(meta);
    card.addEventListener('click', () => {
      if (f.id === activeFigId) return;   // already on this variant
      // Visual feedback: empty the SVG pane immediately and show the
      // spinner so the user doesn't stare at a stale variant while
      // the new one's render is in flight.
      const livePane = document.querySelector(
        '.svg-pane[data-file="' + (fileSel?.value || '') + '"][data-view="__live__"]');
      if (livePane) livePane.innerHTML = '';
      if (typeof showCanvasLoading === 'function') {
        showCanvasLoading('loading ' + (f.name || 'variant') + '...');
      }
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(f.id);
    });
    _attachVariantHoverPreview(card, f);
    strip.appendChild(card);
  }
}

// Hover preview: show a larger 256x180 thumbnail near the cursor after
// a short delay.  Helps distinguish subtly-different variants that look
// identical at 56x42 in the strip.
let _hoverPreviewEl = null;
let _hoverPreviewTimer = null;
function _attachVariantHoverPreview(card, fig) {
  card.addEventListener('mouseenter', (ev) => {
    if (_hoverPreviewTimer) clearTimeout(_hoverPreviewTimer);
    _hoverPreviewTimer = setTimeout(() => {
      if (!_hoverPreviewEl) {
        _hoverPreviewEl = document.createElement('div');
        _hoverPreviewEl.style.cssText =
            'position:fixed;z-index:99997;pointer-events:none;'
          + 'width:280px;height:200px;padding:6px;'
          + 'background:#fff;border:1px solid var(--c-line);'
          + 'border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.18);'
          + 'display:flex;flex-direction:column;gap:4px;';
        document.body.appendChild(_hoverPreviewEl);
      }
      _hoverPreviewEl.innerHTML = '';
      const img = document.createElement('img');
      img.src = API_BASE + '/api/figures/' + encodeURIComponent(fig.id)
                + '/thumbnail?v=' + encodeURIComponent(fig.updated_at || '');
      img.style.cssText =
          'flex:1;min-height:0;width:100%;object-fit:contain;'
        + 'background:var(--c-surface-1);border-radius:4px;';
      img.onerror = () => { img.style.display = 'none'; };
      const label = document.createElement('div');
      label.textContent = fig.name || '(untitled)';
      label.style.cssText =
          'font-size:12px;font-weight:600;color:#18181b;'
        + 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      _hoverPreviewEl.appendChild(img);
      _hoverPreviewEl.appendChild(label);
      // Position to the right of the card, top-aligned with the cursor.
      const r = card.getBoundingClientRect();
      const x = Math.min(r.right + 8, window.innerWidth - 290);
      const y = Math.min(r.top, window.innerHeight - 210);
      _hoverPreviewEl.style.left = x + 'px';
      _hoverPreviewEl.style.top = Math.max(8, y) + 'px';
      _hoverPreviewEl.style.display = 'flex';
    }, 250);
  });
  card.addEventListener('mouseleave', () => {
    if (_hoverPreviewTimer) {
      clearTimeout(_hoverPreviewTimer);
      _hoverPreviewTimer = null;
    }
    if (_hoverPreviewEl) _hoverPreviewEl.style.display = 'none';
  });
}
window._renderVariantStrip = _renderVariantStrip;

async function refreshFiguresList() {
  if (!figuresList) return;
  const figs = await listFigures();
  figuresList.innerHTML = '';
  if (!figs.length) {
    figuresList.innerHTML = '<li style="color:var(--muted); font-style:italic; padding:4px 0;">no figures yet</li>';
    return;
  }
  for (const fig of figs) {
    const li = document.createElement('li');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'name';
    nameSpan.dataset.figId = fig.id;
    nameSpan.textContent = fig.name;
    nameSpan.title = `Source: ${fig.source_id}  -  ${fig.view_id}\\n` +
                      `Updated: ${fig.updated_at || '?'}\\n` +
                      `${(fig.selection || []).length} parts selected`;
    nameSpan.addEventListener('click', () => _loadFigureIntoEditor(fig));
    li.appendChild(nameSpan);

    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.title = 'Delete this figure (cannot be undone)';
    delBtn.style.color = '#c44';
    delBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete figure "${fig.name}"?`)) return;
      await fetch(API_BASE + '/api/figures/' + encodeURIComponent(fig.id),
                   { method: 'DELETE' });
      refreshFiguresList();
    });
    li.appendChild(delBtn);
    figuresList.appendChild(li);
  }
}

$('btn-fig-save').addEventListener('click', saveCurrentAsFigure);
$('fig-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') saveCurrentAsFigure();
});
$('btn-fig-save-as').addEventListener('click', () =>
  saveCurrentAsFigure({ forceNew: true }));

// ---- Dirty-state indicator -----------------------------------------
// When a figure is loaded (via /#/project/.../figure/<fid>) we track
// the state we loaded and compare to the live state.  Drift = unsaved
// changes.  The status line under the save button surfaces this so
// the user knows when to hit save.
let _loadedFigureBaseline = null;     // JSON snapshot at load-time
let _lastSavedAt = null;              // ISO time string
let _loadedFigureCamera = null;       // the loaded figure's own camera

function _stateSig() {
  // Cheap hash of the parts of state we persist.  JSON.stringify is
  // fine here -- the keys are small dicts / short arrays.
  try {
    const s = _gatherCurrentState();
    // Don't compare layers when the cb wiring hasn't booted yet
    return JSON.stringify({
      source_id: s.source_id,
      view_id:   s.view_id,
      camera:    s.camera,
      selection: (s.selection || []).slice().sort((a, b) => a - b),
      styles:    s.styles_per_part || {},
      layers:    s.layers_on || {},
      annot:     (s.annotations || []).length,
      explode:   s.explode || {},
      arrows:    (s.arrows || []).length,
      preset_id: s.preset_id || null,
    });
  } catch (_e) { return ''; }
}

function _markLoadedFigureBaseline() {
  // Capture the moment after _loadFigureIntoEditor finishes restoring
  // the figure -- the editor's state IS the loaded figure now, so
  // dirty=false until the user touches something.
  setTimeout(() => { _loadedFigureBaseline = _stateSig(); }, 800);
}

// Auto-save state.  Three knobs:
//   _autoSaveOn        -- master switch (user can disable in settings later)
//   _autoSaveDelayMs   -- debounce delay; restarts on every detected
//                          change so we only save once after a burst
//                          of tweaks settles
//   _autoSaveInFlight  -- true while a PUT is awaiting response; we
//                          skip the dirty check during this window so
//                          the indicator stays on "saving..." and we
//                          don't fire concurrent saves
let _autoSaveOn = true;
const _AUTO_SAVE_DELAY_MS = 1800;
let _autoSaveTimer = null;
let _autoSaveLastDirtySig = null;
let _autoSaveInFlight = false;

async function _autoSaveFire() {
  _autoSaveTimer = null;
  if (!_autoSaveOn || _autoSaveInFlight) return;
  if (!AppState.currentFigureId) return;
  if (_loadedFigureBaseline == null) return;
  const sig = _stateSig();
  if (sig === _loadedFigureBaseline) return;
  _autoSaveInFlight = true;
  try {
    await saveCurrentAsFigure({ silent: true });
  } finally {
    _autoSaveInFlight = false;
  }
}

// Flush any pending auto-save BEFORE the route changes / EditorScreen
// teardown clears AppState.currentFigureId.  Otherwise the timer fires
// after teardown and saveCurrentAsFigure POSTs a new figure (or errors).
// Called by EditorScreen's teardown -- awaited so the route change
// doesn't proceed until persistence is settled.
async function _flushAutoSave() {
  if (_autoSaveTimer) {
    clearTimeout(_autoSaveTimer);
    _autoSaveTimer = null;
  }
  // Nothing scheduled and nothing in flight -> nothing to do.
  if (!_autoSaveInFlight && _autoSaveLastDirtySig == null) {
    // Re-check the dirty state in case the user edited fast enough that
    // the indicator hadn't ticked yet.  Bail if there's nothing dirty.
    if (_loadedFigureBaseline == null) return;
    if (_stateSig() === _loadedFigureBaseline) return;
  }
  // Wait out any in-flight save first, then fire one final save.
  // Two short waits: cap total flush at ~3s so we never block the
  // route change forever if the server hangs.
  const deadline = Date.now() + 3000;
  while (_autoSaveInFlight && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 50));
  }
  // Final save -- _autoSaveFire is idempotent (checks dirty signature)
  if (AppState.currentFigureId && _autoSaveOn) {
    await _autoSaveFire();
  }
}

window._flushAutoSave = _flushAutoSave;

function _updateSaveStatus() {
  const el = document.getElementById('fig-save-status');
  if (!el) return;
  const inFigure = (typeof AppState !== 'undefined')
                     && !!AppState.currentFigureId;
  if (!inFigure) {
    el.style.display = 'none';
    return;
  }
  el.style.display = 'block';
  if (_autoSaveInFlight) {
    el.textContent = 'saving...';
    el.style.color = 'var(--accora-teal)';
    return;
  }
  const sig = _stateSig();
  const dirty = _loadedFigureBaseline != null
                && sig !== _loadedFigureBaseline;
  if (dirty) {
    el.textContent = _autoSaveOn
      ? '● unsaved changes (auto-save in '
        + Math.ceil(_AUTO_SAVE_DELAY_MS / 1000) + 's)'
      : '● unsaved changes';
    el.style.color = '#b54708';     // amber
    // Schedule / refresh the auto-save debounce so we save once after
    // the user stops changing things.  If the dirty signature is the
    // same as last tick, leave the existing timer running.
    if (_autoSaveOn && sig !== _autoSaveLastDirtySig) {
      _autoSaveLastDirtySig = sig;
      if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
      _autoSaveTimer = setTimeout(_autoSaveFire, _AUTO_SAVE_DELAY_MS);
    }
  } else if (_lastSavedAt) {
    el.textContent = 'saved ' + _humanAgo(_lastSavedAt);
    el.style.color = 'var(--muted)';
    _autoSaveLastDirtySig = null;
  } else {
    el.textContent = 'loaded - no changes yet';
    el.style.color = 'var(--muted)';
    _autoSaveLastDirtySig = null;
  }
}

function _humanAgo(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 5000) return 'just now';
  if (ms < 60000) return Math.floor(ms / 1000) + 's ago';
  if (ms < 3600000) return Math.floor(ms / 60000) + 'm ago';
  return Math.floor(ms / 3600000) + 'h ago';
}

// Poll for state drift.  1s is fine -- the indicator doesn't need to
// react instantly, and we want to keep this cheap.
setInterval(_updateSaveStatus, 1000);

// Expose so EditorScreen / saveCurrentAsFigure can poke them
window._markLoadedFigureBaseline = _markLoadedFigureBaseline;
window._setLastSavedAt = (iso) => {
  _lastSavedAt = iso || new Date().toISOString();
  // Reset baseline to the just-saved state so the indicator flips
  // back to "saved Xs ago" right away.
  _loadedFigureBaseline = _stateSig();
  _updateSaveStatus();
};

// ---- Thumbnail capture ---------------------------------------------
// Rasterize the currently-active SVG pane into a small PNG and PUT
// it to /api/figures/<fid>/thumbnail.  The Project workspace cards
// use this as their preview image.  Fire-and-forget: the figure's
// save path still completes if thumbnail capture fails.
async function _captureFigureThumbnail() {
  // Prefer the LIVE editor pane (what the figure renders into).  Using
  // activePane() alone failed for some views where it didn't resolve to
  // the live pane (active-layout state differs) -> capture returned
  // null and the thumbnail never generated.  Fall back to activePane().
  const pane = document.querySelector(".svg-pane[data-view='__live__']")
    || (typeof activePane === 'function' ? activePane() : null);
  const svg = pane?.querySelector('svg');
  if (!svg) return null;
  // Strip any pan/zoom view-transform group temporarily so the
  // thumbnail captures the WHOLE figure, not whatever the user
  // happens to have panned to.
  const viewG = svg.querySelector(':scope > g.view-transform');
  const prevTransform = viewG?.getAttribute('transform');
  if (viewG) viewG.removeAttribute('transform');
  let outDataUrl = null;
  try {
    const xml = new XMLSerializer().serializeToString(svg);
    const blob = new Blob([xml], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    try {
      const img = await new Promise((res, rej) => {
        const i = new Image();
        i.onload = () => res(i);
        i.onerror = rej;
        i.src = url;
      });
      const W = 320, H = 240;
      const canvas = document.createElement('canvas');
      canvas.width = W; canvas.height = H;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, W, H);
      // Fit-inside, preserve aspect, centred
      const iw = img.width || W, ih = img.height || H;
      const ar = iw / ih;
      let dw = W, dh = H, dx = 0, dy = 0;
      if (ar > W / H) { dh = W / ar; dy = (H - dh) / 2; }
      else { dw = H * ar; dx = (W - dw) / 2; }
      ctx.drawImage(img, dx, dy, dw, dh);
      outDataUrl = canvas.toDataURL('image/png');
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch (e) {
    console.warn('[thumbnail] capture failed:', e?.message || e);
    outDataUrl = null;
  }
  if (viewG && prevTransform !== null) {
    viewG.setAttribute('transform', prevTransform);
  }
  return outDataUrl;
}
window._captureFigureThumbnail = _captureFigureThumbnail;

// Debounce wrapper -- many auto-saves can fire close together.  We
// only want one thumbnail PUT per "burst".
let _thumbTimer = null;
function _scheduleThumbnailUpload(figId) {
  if (!figId) return;
  if (_thumbTimer) clearTimeout(_thumbTimer);
  _thumbTimer = setTimeout(async () => {
    _thumbTimer = null;
    const durl = await _captureFigureThumbnail();
    if (!durl) return;
    try {
      await fetch(API_BASE + '/api/figures/'
                    + encodeURIComponent(figId) + '/thumbnail',
                    { method: 'PUT',
                       headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify({ data_url: durl }) });
    } catch (e) {
      console.warn('[thumbnail] upload failed:', e?.message || e);
    }
  }, 800);
}
window._scheduleThumbnailUpload = _scheduleThumbnailUpload;

// Capture the active pane ONCE and upload it as BOTH the figure's and
// (if known) the parent view's thumbnail.  Views are the big preview
// gap -- there are far more views than figures and they never had a
// capture path of their own -- so we piggy-back the figure capture
// onto the view too (a view's preview is just one of its variants).
async function _captureAndUploadAll(figId, viewId) {
  const durl = await _captureFigureThumbnail();
  if (!durl) return false;
  const body = JSON.stringify({ data_url: durl });
  const hdr = { 'Content-Type': 'application/json' };
  const puts = [];
  if (figId) puts.push(fetch(API_BASE + '/api/figures/'
      + encodeURIComponent(figId) + '/thumbnail',
      { method: 'PUT', headers: hdr, body }).catch(() => null));
  if (viewId) puts.push(fetch(API_BASE + '/api/views/'
      + encodeURIComponent(viewId) + '/thumbnail',
      { method: 'PUT', headers: hdr, body }).catch(() => null));
  if (!puts.length) return false;
  try { await Promise.all(puts); return true; }
  catch (_e) { return false; }
}
window._captureAndUploadAll = _captureAndUploadAll;

// Capture-on-open: a beat after a live render settles, snapshot the
// current figure (and its view) so browsing fills in tile previews.
// Debounced so rapid navigation doesn't spam captures.
let _openThumbTimer = null;
function _scheduleOpenThumbnail() {
  if (_openThumbTimer) clearTimeout(_openThumbTimer);
  _openThumbTimer = setTimeout(() => {
    _openThumbTimer = null;
    let fid = null, vid = null;
    try { fid = AppState && AppState.currentFigureId; } catch (_e) {}
    try { vid = AppState && AppState.currentViewId; } catch (_e) {}
    if (fid || vid) _captureAndUploadAll(fid, vid);
  }, 1500);
}
window._scheduleOpenThumbnail = _scheduleOpenThumbnail;

// Initial load (once the server probe says we're online)
// probeServer fires its .then before this; refresh manually after a beat.
setTimeout(refreshFiguresList, 1500);

// --- Projects (Phase B) ----------------------------------------------
const projectSel = document.getElementById('project-sel');

async function listProjects() {
  if (typeof API_BASE !== 'string') return [];
  try {
    const r = await fetch(API_BASE + '/api/projects');
    if (!r.ok) return [];
    return (await r.json()).projects || [];
  } catch (_e) { return []; }
}

async function refreshProjectsList() {
  if (!projectSel) return;
  const projs = await listProjects();
  const current = projectSel.value;
  projectSel.innerHTML = '<option value="">— All figures —</option>'
    + '<option value="__orphans__">  (Unfiled)</option>';
  for (const p of projs) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    if (p.id === current) opt.selected = true;
    projectSel.appendChild(opt);
  }
  // If the previously-selected project was deleted, reset to All
  if (current && current !== '__orphans__'
      && !projs.some(p => p.id === current)) {
    projectSel.value = '';
  }
}

// Override figures list to filter by currently-selected project
const _origRefreshFiguresList = refreshFiguresList;
refreshFiguresList = async function() {
  if (!figuresList) return;
  const pid = projectSel?.value || '';
  let figs;
  if (pid === '__orphans__') {
    try {
      const r = await fetch(API_BASE + '/api/figures/orphans');
      figs = r.ok ? (await r.json()).figures || [] : [];
    } catch (_e) { figs = []; }
  } else if (pid) {
    try {
      const r = await fetch(API_BASE + '/api/projects/'
                              + encodeURIComponent(pid) + '/figures');
      figs = r.ok ? (await r.json()).figures || [] : [];
    } catch (_e) { figs = []; }
  } else {
    figs = await listFigures();
  }
  figuresList.innerHTML = '';
  if (!figs.length) {
    figuresList.innerHTML = '<li style="color:var(--muted); font-style:italic; padding:4px 0;">no figures here</li>';
    return;
  }
  for (const fig of figs) {
    const li = document.createElement('li');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'name';
    nameSpan.dataset.figId = fig.id;
    nameSpan.textContent = fig.name;
    nameSpan.title = `Source: ${fig.source_id}  -  ${fig.view_id}\\n` +
                      `Updated: ${fig.updated_at || '?'}\\n` +
                      `${(fig.selection || []).length} parts selected`;
    nameSpan.addEventListener('click', () => _loadFigureIntoEditor(fig));
    li.appendChild(nameSpan);

    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.title = 'Delete this figure (cannot be undone)';
    delBtn.style.color = '#c44';
    delBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete figure "${fig.name}"?`)) return;
      await fetch(API_BASE + '/api/figures/' + encodeURIComponent(fig.id),
                   { method: 'DELETE' });
      refreshFiguresList();
    });
    li.appendChild(delBtn);
    figuresList.appendChild(li);
  }
};

// Override save so it sets project_id from the current selection.
// Also: if the user navigated in via /#/project/<pid>/figure/<fid>,
// "save" should UPDATE that figure (PUT) rather than create a new
// one (POST).  The previous behaviour silently spammed duplicate
// figures whenever you tweaked styles and pressed save.
const _origSaveCurrentAsFigure = saveCurrentAsFigure;
saveCurrentAsFigure = async function(opts) {
  opts = opts || {};
  const nameInput = $('fig-name');
  // Bind: if an existing figure is loaded (via figure route),
  //   default to that name and UPDATE in place.
  // Free-form: caller passed forceNew, OR no figure is loaded ->
  //   require a name from the input and POST a new figure.
  const loadedFigId = (typeof AppState !== 'undefined')
                       ? AppState.currentFigureId : null;
  const updatingExisting = !!loadedFigId && !opts.forceNew;

  let name = (nameInput.value || '').trim();
  if (!name && updatingExisting) {
    // Look up the figure's stored name so a save with an empty
    // input doesn't blank the name field.
    try {
      const r0 = await fetch(API_BASE + '/api/figures/'
                              + encodeURIComponent(loadedFigId));
      if (r0.ok) name = (await r0.json()).name || '';
    } catch (_e) {}
  }
  if (!name) { nameInput.focus(); return; }

  const body = { name, ..._gatherCurrentState() };
  const pid = projectSel?.value || '';
  if (pid && pid !== '__orphans__') body.project_id = pid;

  let url, method;
  if (updatingExisting) {
    url = API_BASE + '/api/figures/' + encodeURIComponent(loadedFigId);
    method = 'PUT';
    body.id = loadedFigId;
  } else {
    url = API_BASE + '/api/figures';
    method = 'POST';
  }
  let r;
  try {
    r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    (window.IFU_UI?.toast || function(){})(
      'Save failed: ' + (e.message || e), 'error');
    return;
  }
  if (!r.ok) {
    (window.IFU_UI?.toast || function(){})(
      'Save failed: HTTP ' + r.status, 'error');
    return;
  }
  if (updatingExisting) {
    if (!opts.silent) {
      (window.IFU_UI?.toast || function(){})(
        'Saved \"' + name + '\"', 'success');
    }
    // Update the breadcrumb in case the name changed
    const crumb = document.querySelector('#editor-breadcrumb .current');
    if (crumb) crumb.textContent = name;
    if (window._setLastSavedAt) window._setLastSavedAt();
    // Re-capture the thumbnail so workspace cards stay in sync with
    // whatever the user has been styling.  Fire-and-forget.
    if (window._scheduleThumbnailUpload) {
      window._scheduleThumbnailUpload(loadedFigId);
    }
  } else {
    (window.IFU_UI?.toast || function(){})(
      'Created \"' + name + '\"', 'success');
    nameInput.value = '';
    refreshFiguresList();
    // If we just forked, hop into the new figure so subsequent
    // saves update it.  /api/figures returns the new record.
    if (opts.forceNew) {
      try {
        const fig = await r.json();
        if (fig && fig.id && pid) {
          location.hash = '#/project/' + encodeURIComponent(pid)
                          + '/figure/' + encodeURIComponent(fig.id);
        }
      } catch (_e) {}
    }
  }
};

$('btn-project-new').addEventListener('click', async () => {
  const name = prompt('Project name:');
  if (!name) return;
  const r = await fetch(API_BASE + '/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) { alert('Create project failed: ' + r.status); return; }
  const proj = await r.json();
  await refreshProjectsList();
  projectSel.value = proj.id;
  refreshFiguresList();
});

$('btn-project-del').addEventListener('click', async () => {
  const pid = projectSel.value;
  if (!pid || pid === '__orphans__') {
    alert('Pick a project first');
    return;
  }
  if (!confirm('Delete project? Its figures will become Unfiled.')) return;
  await fetch(API_BASE + '/api/projects/' + encodeURIComponent(pid),
               { method: 'DELETE' });
  projectSel.value = '';
  await refreshProjectsList();
  refreshFiguresList();
});

projectSel.addEventListener('change', refreshFiguresList);

setTimeout(refreshProjectsList, 1200);

// --- Revisions (Phase C) ---------------------------------------------
// Per-figure revision-status badge.  The figure JSON carries an
// optional ``bound_revision`` (set at save time -- Phase D for full
// wiring); the server computes "versions behind" by comparing the
// bound id to the cached Versions list.

const revsStatus = document.getElementById('revs-status');

async function refreshVersionsForActiveSource() {
  const fid = fileSel.value;
  if (!fid) return;
  if (revsStatus) revsStatus.textContent = '…';
  try {
    const r = await fetch(API_BASE + '/api/sources/'
                           + encodeURIComponent(fid)
                           + '/versions/refresh', { method: 'POST' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      if (revsStatus) revsStatus.textContent =
        '✗ ' + (err.error || ('HTTP ' + r.status));
      return;
    }
    const env = await r.json();
    const n = (env.versions || []).length;
    if (revsStatus) revsStatus.textContent =
      `✓ ${n} versions cached`;
    // Re-render figures list to update badges
    refreshFiguresList();
  } catch (e) {
    if (revsStatus) revsStatus.textContent = '✗ ' + (e.message || 'failed');
  }
}

$('btn-revs-refresh').addEventListener('click', refreshVersionsForActiveSource);

// Browser-prompt-based version picker.  Lists the cached Versions
// newest-first and asks the user which one to bind to.  Single-user
// local tool -- not worth a fancy modal yet.
async function _promptBindRevision(figureId, versions) {
  if (!versions || !versions.length) {
    alert('No cached Versions for this source. Refresh first.');
    return;
  }
  const lines = versions.map((v, i) =>
    `${i + 1}. ${v.name || '?'}  (${(v.created_at || '').slice(0, 10)})`
  ).join('\\n');
  const pick = prompt(
    'Bind to which Version?\\n\\n' + lines + '\\n\\nEnter number (1-'
      + versions.length + ') or blank to cancel:');
  if (!pick) return;
  const idx = parseInt(pick) - 1;
  if (Number.isNaN(idx) || idx < 0 || idx >= versions.length) {
    alert('Invalid choice.');
    return;
  }
  const target = versions[idx];
  const r = await fetch(API_BASE + '/api/figures/'
                          + encodeURIComponent(figureId) + '/bind_revision', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version_id: target.id }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert('Bind failed: ' + (err.error || ('HTTP ' + r.status)));
    return;
  }
  refreshFiguresList();
}

// Re-render the figures list with revision badges.  Wraps the prior
// refresh so we can stamp a small ⬆/✓ on each <li>.
const _refreshFiguresList_phaseC = refreshFiguresList;
refreshFiguresList = async function() {
  await _refreshFiguresList_phaseC();
  // For each rendered li, fetch its revision_status and append a badge
  if (!figuresList) return;
  const lis = figuresList.querySelectorAll('li');
  for (const li of lis) {
    const nameSpan = li.querySelector('.name');
    if (!nameSpan || !nameSpan.dataset.figId) continue;
    try {
      const r = await fetch(API_BASE + '/api/figures/'
                              + encodeURIComponent(nameSpan.dataset.figId)
                              + '/revision_status');
      if (!r.ok) continue;
      const s = await r.json();
      const badge = document.createElement('span');
      badge.style.fontSize = '10px';
      badge.style.marginRight = '4px';
      if (s.versions_behind === null || s.versions_behind === undefined) {
        // No bound revision OR no cache -- show a "bind" button if there
        // ARE cached versions for the source, otherwise skip.
        const vresp = await fetch(API_BASE + '/api/sources/'
                                    + encodeURIComponent(s.source_id || '')
                                    + '/versions').catch(() => null);
        const versions = vresp && vresp.ok
          ? ((await vresp.json()).versions || []) : [];
        if (versions.length === 0) continue;
        badge.textContent = '⚓';
        badge.style.color = '#888';
        badge.style.cursor = 'pointer';
        badge.title = `Bind this figure to a Version. ${versions.length} cached.`;
        badge.addEventListener('click', (e) => {
          e.stopPropagation();
          _promptBindRevision(s.figure_id, versions);
        });
      } else if (s.versions_behind === 0) {
        badge.textContent = '✓';
        badge.style.color = '#0a8';
        badge.style.cursor = 'pointer';
        badge.title = 'Bound to the latest Version. Click to re-bind.';
        badge.addEventListener('click', async (e) => {
          e.stopPropagation();
          const vresp = await fetch(API_BASE + '/api/sources/'
                                       + encodeURIComponent(s.source_id)
                                       + '/versions');
          const versions = (await vresp.json()).versions || [];
          _promptBindRevision(s.figure_id, versions);
        });
      } else {
        badge.textContent = '⬆' + s.versions_behind;
        badge.style.color = '#c70';
        badge.style.cursor = 'pointer';
        badge.title = `Bound to ${s.bound_revision?.name || '?'}; latest is `
                    + `${s.latest_revision?.name || '?'}. Click to re-bind.`;
        badge.addEventListener('click', async (e) => {
          e.stopPropagation();
          const vresp = await fetch(API_BASE + '/api/sources/'
                                       + encodeURIComponent(s.source_id)
                                       + '/versions');
          const versions = (await vresp.json()).versions || [];
          _promptBindRevision(s.figure_id, versions);
        });
      }
      li.insertBefore(badge, li.firstChild);
    } catch (_e) {}
  }
};

// nameSpan.dataset.figId is set inside each list-builder so the
// phaseC wrapper above can look up revision status by id.

// --- Per-part styling ---------------------------------------------------
// Per-source dict of part_idx -> {stroke, width, opacity, dash}
// Persisted in localStorage, rebuilt into a <style> tag on every refresh
// so the rules apply to live + baked SVGs alike.

// Per-part applied styles are a property of the FIGURE, not the source.
// Two figures (variants) built on the same source must NOT share a style
// map -- keying by source_id was the root of variant style carry-over.
// We key localStorage by the current figure id; the server figure record
// (styles_per_part) remains the source of truth and is re-synced on every
// load, so switching the cache key is transparent.  Callers still pass a
// `fid` argument (historically the source id) -- it is ignored in favour
// of the active figure so every read/write is automatically figure-scoped.
function _figId() {
  try {
    if (typeof AppState !== 'undefined' && AppState.currentFigureId)
      return 'fig_' + AppState.currentFigureId;
  } catch (_e) {}
  // Fallback for transient states with no loaded figure (e.g. ad-hoc
  // "generate 2D" before saving): scope by source so behaviour is sane.
  try { return 'src_' + fileSel.value; } catch (_e) { return 'src_'; }
}
function _styleKey(_fid) { return 'partStyles_' + _figId(); }
function loadPartStyles(_fid) {
  try {
    return JSON.parse(localStorage.getItem(_styleKey()) || '{}');
  } catch (_e) { return {}; }
}
function persistPartStyles(_fid, m) {
  localStorage.setItem(_styleKey(), JSON.stringify(m));
}
// Test/diagnostic accessor: the current figure's applied-style map,
// independent of how the cache key is built (so tests don't hard-code
// the key format).
window._figStyles = () => loadPartStyles();
window._figStyleKey = () => _styleKey();
// applyStyleSheet is a hoisted function declaration further down; expose
// it so tests can force a clean re-render after clearing styles.
window.applyStyleSheet = applyStyleSheet;

// Persistent silhouette overlay: for each applied part, draw the SAME
// closed-loop polygon the live highlight uses (from footprint cache or
// fallback to outline_v / sharp_v paths), at the user's chosen stroke
// & fill.  Stays on screen across selections, layered just under the
// transient silhouette overlay so live selection still wins on top.
// (Removed: the per-style "group union" silhouette cache + server fetch.
// The persistent overlay now renders each part's OWN footprint, so the
// server-computed union -- which could omit an instance and leave it
// styled-but-invisible -- is no longer used.  _styleGroupKey below is
// retained purely to batch same-styled parts into one DOM <path>.)
//
// Used by renderPersistentSilhouettes to package styles into a stable
// hash key for grouping.  Two parts with the same serialised style
// will merge into one combined outline.
//
// NB: renamed from _styleKey to _styleGroupKey to avoid clobbering
// the long-standing _styleKey(fid) localStorage helper -- both were
// hoisted function declarations, the later one wins, and the
// localStorage path silently broke (loadPartStyles + persistPartStyles
// were reading/writing the wrong key, which also bailed out
// _loadFigureIntoEditor before the auto-render could fire).
function _styleGroupKey(style) {
  return [
    style.stroke || '#00836a',
    String(style.width ?? 3),
    String(style.opacity ?? 1),
    style.fillOn ? '1' : '0',
    style.fillColor || '',
    String(style.fillAlpha ?? 0.3),
    style.dash || '',
  ].join('|');
}


function renderPersistentSilhouettes() {
  document.querySelectorAll('.svg-pane').forEach(pane => {
    // Styles are figure-scoped now, and a figure is edited in the live
    // pane.  Only render the applied-style overlay there so hidden baked
    // panes (possibly a different source) don't get this figure's
    // silhouettes drawn into them.
    if (pane.dataset.view !== '__live__') return;
    const svg = pane.querySelector('svg');
    if (!svg) return;
    const scaleG = svg.querySelector('g[transform="scale(1,-1)"]')
                || svg.querySelector('.view-transform > g')
                || svg.querySelector(':scope > g');
    if (!scaleG) return;
    scaleG.querySelector(':scope > g.layer-persistent-silhouette')?.remove();

    const fid = pane.dataset.file;
    const vid = pane.dataset.view;
    const m = loadPartStyles(fid);
    if (!Object.keys(m).length) return;

    // Group parts by serialised style so adjacent parts that share a
    // preset merge into a single combined outline instead of N per-
    // part loops with visible seams between them.  Server takes the
    // union of each group's pixel masks and traces ONE contour.
    const groups = {};                // gkey -> [idx, ...]
    const styleByGroup = {};          // gkey -> style dict
    for (const [idxStr, style] of Object.entries(m)) {
      const gkey = _styleGroupKey(style);
      (groups[gkey] = groups[gkey] || []).push(parseInt(idxStr));
      styleByGroup[gkey] = style;
    }

    const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    layer.setAttribute('class', 'layer-persistent-silhouette');
    layer.setAttribute('pointer-events', 'none');

    for (const [gkey, idxs] of Object.entries(groups)) {
      const style = styleByGroup[gkey];
      // Draw EACH styled part's own footprint (with a baked-outline
      // fallback).  Every part is guaranteed to contribute ink, so a
      // styled part can never silently fail to render.
      //
      // NOTE: we used to substitute a single server-computed "group
      // union" contour here to merge adjacent same-styled parts and
      // hide their shared seams.  That union sometimes OMITTED an
      // instance (e.g. one of several identical air-springs), and
      // because the union branch skipped the per-part fallback, the
      // missing instance became invisible even though it was styled.
      // Correctness first: always render per-part.  (Phase 2 can re-add
      // seam-merging once the union is verified to cover every part.)
      let subpaths = [];
      for (const idx of idxs) {
        const fp = _getFootprint(fid, vid, idx);
        if (fp && fp.length) {
          fp.forEach(pl => {
            if (!pl || pl.length < 2) return;
            subpaths.push(
              'M ' + pl.map(p => p[0].toFixed(2) + ' ' + p[1].toFixed(2))
                      .join(' L ') + ' Z'
            );
          });
        } else {
          const partCls = '.part-' + String(idx).padStart(3, '0');
          svg.querySelectorAll(
            '.layer-outline_v ' + partCls + ' path, '
            + '.layer-sharp_v ' + partCls + ' path'
          ).forEach(p => {
            const d = (p.getAttribute('d') || '').trim();
            if (d) subpaths.push(d);
          });
        }
      }
      if (!subpaths.length) continue;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', subpaths.join(' '));
      path.setAttribute('fill', style.fillOn ? (style.fillColor || '#cce6e0') : 'none');
      path.setAttribute('fill-opacity', String(style.fillAlpha ?? 0.3));
      path.setAttribute('fill-rule', 'evenodd');
      path.setAttribute('stroke', style.stroke || '#00836a');
      path.setAttribute('stroke-width', String(style.width ?? 3));
      path.setAttribute('stroke-opacity', String(style.opacity ?? 1));
      if (style.dash) path.setAttribute('stroke-dasharray', style.dash);
      path.setAttribute('stroke-linejoin', 'round');
      path.setAttribute('stroke-linecap', 'round');
      layer.appendChild(path);
    }
    // Place near the front: just before the transient silhouette layer
    // and click-hit layers so it draws on top of the line art.
    scaleG.appendChild(layer);
  });
}

function applyStyleSheet() {
  const fid = fileSel.value;
  const m = loadPartStyles(fid);
  // Group parts by serialised rules so identical styles share one
  // selector list -- when the user applies a preset to 50 parts at
  // once, the old code wrote 50 individual rules with 50 selectors;
  // the browser then re-resolved each selector across the entire
  // SVG.  One rule with a 50-item selector list is dramatically
  // cheaper for the style engine to apply.
  const rulesByKey = new Map();   // rulesKey -> [selectors]
  for (const [idx, st] of Object.entries(m)) {
    const sel = `.svg-pane[data-file="${fid}"] svg .part.part-${String(idx).padStart(3, '0')} path`;
    const rules = [];
    if (st.opacity != null && st.opacity !== 1) {
      rules.push(`opacity: ${st.opacity}`);
    }
    if (!rules.length) continue;
    const key = rules.join('; ');
    if (!rulesByKey.has(key)) rulesByKey.set(key, []);
    rulesByKey.get(key).push(sel);
  }
  let css = '';
  for (const [rules, selectors] of rulesByKey) {
    css += `${selectors.join(',\\n')} { ${rules} !important; }\n`;
  }
  let styleEl = document.getElementById('per-part-styles');
  if (!styleEl) {
    styleEl = document.createElement('style');
    styleEl.id = 'per-part-styles';
    document.head.appendChild(styleEl);
  }
  styleEl.textContent = css;
  // Push to 3D pane too
  window.IFU_VIEWER?.applyPartStyles3D?.(m);
  // Re-render the persistent silhouette overlays and refresh the list
  renderPersistentSilhouettes();
  renderAppliedStylesList();
}

$('sty-width').addEventListener('input', (e) => {
  $('sty-width-val').textContent = parseFloat(e.target.value).toFixed(1);
});

// ===== Drawing weights + shading: live CSS overrides ================
// The baked SVG carries default stroke-widths per layer category.
// Rather than re-render server-side on every slider drag, we inject a
// per-source <style> block that targets the layer classes with
// !important so the SVG looks how the user dialled it.
//
// Persisted in localStorage keyed by source_id; restored on figure load.
const _DRAW_DEFAULTS = {
  outline_w: 0.70,
  sharp_w:   0.30,
  smooth_w:  0.20,
  hidden_w:  0.30,
  hl_w:      3.0,
  contrast:  1.0,
  smooth_alpha: 1.0,
  line_color: 'black',
  paper:     'white',
};
const _LINE_COLOR_VALUES = {
  black: '#000000',
  ink:   '#1a1f24',
  teal:  '#00836a',
  grey:  '#5a5a5e',
};
const _PAPER_COLORS = {
  white: '#ffffff',
  cream: '#fbf7ec',
  cool:  '#eef1f4',
  dark:  '#1c1c1f',
};

function _drawKey(fid) { return 'drawSettings_' + fid; }
function _drawDefaultKey() { return 'drawSettings__default'; }

function _loadDrawSettings(fid) {
  try {
    const perSrc = JSON.parse(localStorage.getItem(_drawKey(fid)) || 'null');
    if (perSrc) return { ..._DRAW_DEFAULTS, ...perSrc };
    const dflt = JSON.parse(localStorage.getItem(_drawDefaultKey()) || 'null');
    if (dflt) return { ..._DRAW_DEFAULTS, ...dflt };
  } catch (_e) {}
  return { ..._DRAW_DEFAULTS };
}
function _saveDrawSettings(fid, s) {
  try { localStorage.setItem(_drawKey(fid), JSON.stringify(s)); } catch (_e) {}
}

function _readDrawSettingsFromUI() {
  return {
    outline_w: parseFloat($('draw-outline-w').value),
    sharp_w:   parseFloat($('draw-sharp-w').value),
    smooth_w:  parseFloat($('draw-smooth-w').value),
    hidden_w:  parseFloat($('draw-hidden-w').value),
    hl_w:      parseFloat($('draw-hl-w').value),
    contrast:  parseFloat($('draw-contrast').value),
    smooth_alpha: parseFloat($('draw-smooth-alpha').value),
    line_color: $('draw-line-color').value,
    paper:      $('draw-paper').value,
  };
}
function _writeDrawSettingsToUI(s) {
  $('draw-outline-w').value = s.outline_w;
  $('draw-outline-w-val').textContent = s.outline_w.toFixed(2);
  $('draw-sharp-w').value = s.sharp_w;
  $('draw-sharp-w-val').textContent = s.sharp_w.toFixed(2);
  $('draw-smooth-w').value = s.smooth_w;
  $('draw-smooth-w-val').textContent = s.smooth_w.toFixed(2);
  $('draw-hidden-w').value = s.hidden_w;
  $('draw-hidden-w-val').textContent = s.hidden_w.toFixed(2);
  $('draw-hl-w').value = s.hl_w;
  $('draw-hl-w-val').textContent = s.hl_w.toFixed(1);
  $('draw-contrast').value = s.contrast;
  $('draw-contrast-val').textContent = s.contrast.toFixed(2);
  $('draw-smooth-alpha').value = s.smooth_alpha;
  $('draw-smooth-alpha-val').textContent = s.smooth_alpha.toFixed(2);
  $('draw-line-color').value = s.line_color;
  $('draw-paper').value = s.paper;
  // Keep the per-part highlight slider in sync so existing
  // applySilhouetteFill consumers see the same value.
  $('sty-width').value = s.hl_w;
  $('sty-width-val').textContent = s.hl_w.toFixed(1);
}

function _applyDrawSettings(fid) {
  const s = _readDrawSettingsFromUI();
  _saveDrawSettings(fid, s);

  const lineCol = _LINE_COLOR_VALUES[s.line_color] || '#000000';
  const paperCol = _PAPER_COLORS[s.paper] || '#ffffff';
  const k = s.contrast;

  // Compose the CSS so it targets ONLY this source's panes (the SVG
  // is matched via the .svg-pane[data-file=...] wrapper).  !important
  // beats the inline stroke-width attribute set at bake time.
  //
  // Class name: the bake emits <g class="layer layer-outline_v"> --
  // two SEPARATE classes, NOT one ".layer.outline_v".  Targeting
  // .layer-outline_v (or g.layer-outline_v) is what actually matches.
  // We set stroke-width on the <g> wrapper so the paths inherit
  // correctly without us having to walk every <path>.
  // Silhouette slider drives BOTH the per-part outline_v layer AND
  // (when present) the assembly_silhouette layer at the same width.
  // The assembly_silhouette only loads after the server returns it
  // (requires restart for the new endpoint payload).  If it isn't
  // there, the per-part outline_v still renders at the Silhouette
  // weight -- same as the pre-combined behaviour.  When it IS there,
  // it draws on top.  A future toggle can suppress outline_v in
  // favour of the combined version; for now keep both visible.
  const css = `
    .svg-pane[data-file="${fid}"] {
      background: ${paperCol};
    }
    .svg-pane[data-file="${fid}"] svg g.layer-outline_v,
    .svg-pane[data-file="${fid}"] svg g.layer-assembly_silhouette {
      stroke: ${lineCol} !important;
      stroke-width: ${(s.outline_w * k).toFixed(3)} !important;
    }
    .svg-pane[data-file="${fid}"] svg g.layer-assembly_silhouette {
      fill: none !important;
    }
    .svg-pane[data-file="${fid}"] svg g.layer-sharp_v {
      stroke: ${lineCol} !important;
      stroke-width: ${(s.sharp_w * k).toFixed(3)} !important;
    }
    .svg-pane[data-file="${fid}"] svg g.layer-smooth_v {
      stroke-width: ${(s.smooth_w * k).toFixed(3)} !important;
      opacity: ${s.smooth_alpha.toFixed(2)} !important;
    }
    .svg-pane[data-file="${fid}"] svg g.layer-hidden_outline,
    .svg-pane[data-file="${fid}"] svg g.layer-hidden_sharp {
      stroke-width: ${(s.hidden_w * k).toFixed(3)} !important;
    }
  `;
  let el = document.getElementById('draw-settings-style');
  if (!el) {
    el = document.createElement('style');
    el.id = 'draw-settings-style';
    document.head.appendChild(el);
  }
  el.textContent = css;

  // Re-render persistent silhouettes so the highlight outline width
  // (hl_w) picks up the new value -- applySilhouetteFill reads
  // sty-width which we've kept in sync.
  if (typeof renderPersistentSilhouettes === 'function') {
    renderPersistentSilhouettes();
  }
}

// Wire each control to live updates.  Sliders show value as they
// move; everything writes to localStorage + re-applies CSS.
function _onDrawSettingChange() {
  const fid = fileSel.value;
  if (!fid) return;
  // Tick the visible-value labels
  $('draw-outline-w-val').textContent = parseFloat($('draw-outline-w').value).toFixed(2);
  $('draw-sharp-w-val').textContent   = parseFloat($('draw-sharp-w').value).toFixed(2);
  $('draw-smooth-w-val').textContent  = parseFloat($('draw-smooth-w').value).toFixed(2);
  $('draw-hidden-w-val').textContent  = parseFloat($('draw-hidden-w').value).toFixed(2);
  $('draw-hl-w-val').textContent      = parseFloat($('draw-hl-w').value).toFixed(1);
  $('draw-contrast-val').textContent  = parseFloat($('draw-contrast').value).toFixed(2);
  $('draw-smooth-alpha-val').textContent = parseFloat($('draw-smooth-alpha').value).toFixed(2);
  // Keep highlight slider mirror in sync
  $('sty-width').value = $('draw-hl-w').value;
  $('sty-width-val').textContent = parseFloat($('draw-hl-w').value).toFixed(1);
  _applyDrawSettings(fid);
}

[
  'draw-outline-w', 'draw-sharp-w', 'draw-smooth-w', 'draw-hidden-w',
  'draw-hl-w', 'draw-contrast', 'draw-smooth-alpha',
  'draw-line-color', 'draw-paper',
].forEach(id => {
  const el = $(id);
  if (!el) return;
  el.addEventListener('input', _onDrawSettingChange);
  el.addEventListener('change', _onDrawSettingChange);
});

$('btn-draw-reset')?.addEventListener('click', () => {
  _writeDrawSettingsToUI({ ..._DRAW_DEFAULTS });
  _applyDrawSettings(fileSel.value);
});

$('btn-draw-save-default')?.addEventListener('click', () => {
  const s = _readDrawSettingsFromUI();
  try {
    localStorage.setItem(_drawDefaultKey(), JSON.stringify(s));
    toast('Saved as default weights for new figures', 'success');
  } catch (_e) {
    toast('Could not save defaults', 'error');
  }
});

// Restore drawing settings whenever the active source changes.
function _restoreDrawSettingsForActiveSource() {
  const fid = fileSel.value;
  if (!fid) return;
  const s = _loadDrawSettings(fid);
  _writeDrawSettingsToUI(s);
  _applyDrawSettings(fid);
}
fileSel.addEventListener('change', _restoreDrawSettingsForActiveSource);
// Also call on first load once the DOM has the file picked
setTimeout(_restoreDrawSettingsForActiveSource, 0);
// ===== end Drawing weights =========================================
$('sty-opacity').addEventListener('input', (e) => {
  $('sty-opacity-val').textContent = parseFloat(e.target.value).toFixed(2);
});
$('sty-fill-opacity').addEventListener('input', (e) => {
  $('sty-fill-opacity-val').textContent = parseFloat(e.target.value).toFixed(2);
  restyleSilhouetteOnly();
});
// Style-control changes refresh ONLY the silhouette overlay -- we don't
// re-walk all 678 part nodes on every slider input.  rAF-coalesce so
// drag events get a single update per frame.
let _restylePending = false;
function restyleSilhouetteOnly() {
  if (_restylePending) return;
  _restylePending = true;
  requestAnimationFrame(() => {
    _restylePending = false;
    const svg = activeSvg();
    if (!svg) return;
    const st = getState(fileSel.value, viewSel.value);
    const set = st.highlights || new Set();
    if (!set.size) return;
    applySilhouetteFill(
      svg, set,
      $('sty-fill-on').checked,
      $('sty-fill').value,
      parseFloat($('sty-fill-opacity').value),
      $('sty-stroke').value,
      parseFloat($('sty-width').value),
    );
  });
}
['sty-stroke', 'sty-width', 'sty-fill', 'sty-fill-on'].forEach(id => {
  $(id).addEventListener('input', restyleSilhouetteOnly);
  $(id).addEventListener('change', restyleSilhouetteOnly);
});

// --- Convex hull silhouette for fill / closed-profile highlighting --------
// IFU-style highlighting: fill the part with a tint and bold its outline,
// including the borders shared with occluding parts (so the profile is a
// CLOSED loop).  Approximated by the convex hull of all the part's
// polyline points -- exact for tube/panel/bracket shapes, slightly
// generous for concave parts.
// Server-fetched true silhouettes (per-part HLR with NO occluders).
// Keyed by (file_id|view_id|idx).  When present, used INSTEAD of the
// local outline_v polylines so the bold edge is closed even where the
// part is partially blocked by neighbours.  Populated by
// fetchTrueSilhouettes() and refreshed whenever camera changes.
const _trueSilCache = new Map();
function _silCacheKey(fid, vid, idx) { return fid + '|' + vid + '|' + idx; }
function _setTrueSil(fid, vid, idx, polys) {
  _trueSilCache.set(_silCacheKey(fid, vid, idx), polys || []);
}
function _getTrueSil(fid, vid, idx) {
  return _trueSilCache.get(_silCacheKey(fid, vid, idx)) || null;
}
// Visible-footprint cache (server-rasterized).  Keyed by (fid, vid, idx).
// Used for BOTH (a) the bold-edge closed loop tracing the part's actually
// visible 2D region, and (b) the click-anywhere hit area.
const _footprintCache = new Map();
function _fpKey(fid, vid, idx) { return fid + '|' + vid + '|' + idx; }
function _setFootprint(fid, vid, idx, polys) {
  _footprintCache.set(_fpKey(fid, vid, idx), polys || []);
}
function _getFootprint(fid, vid, idx) {
  return _footprintCache.get(_fpKey(fid, vid, idx)) || null;
}

// Assembly silhouette cache: the closed outline of the WHOLE assembly
// as seen from this camera.  When present, the SVG draws ONE bold loop
// around the union of all parts instead of N per-part outlines that
// expose the seams between adjacent parts.  Keyed by (fid, vid).
const _assemblySilhouetteCache = new Map();
function _asKey(fid, vid) { return fid + '|' + vid; }
function _setAssemblySilhouette(fid, vid, polys) {
  _assemblySilhouetteCache.set(_asKey(fid, vid), polys || []);
}
function _getAssemblySilhouette(fid, vid) {
  return _assemblySilhouetteCache.get(_asKey(fid, vid)) || null;
}
// Track which views we've already fetched the assembly raster for so we
// don't re-request when the user clicks more parts in the same view.
const _footprintViewFetched = new Set();
function _fpViewKey(fid, vid) { return fid + '|' + vid; }

// Group-mode silhouette cache: keyed by (fid, vid, sorted-index-tuple)
const _groupSilCache = new Map();
function _groupKey(fid, vid, idxList) {
  return fid + '|' + vid + '|' + idxList.slice().sort((a,b)=>a-b).join(',');
}
function _setGroupSil(fid, vid, idxList, polys) {
  _groupSilCache.set(_groupKey(fid, vid, idxList), polys || []);
}
function _getGroupSil(fid, vid, idxList) {
  return _groupSilCache.get(_groupKey(fid, vid, idxList)) || null;
}

// Inject (or refresh) a filled silhouette + bold edge for every
// highlighted part.  Prefers the server-fetched TRUE silhouette (closed
// loops, no occlusion holes); falls back to the local outline_v
// polylines from the baked SVG when the server hasn't responded yet
// (or isn't running at all).
function applySilhouetteFill(svg, highlights, fillOn, fillColor, fillAlpha,
                              strokeColor, strokeWidth, opts) {
  opts = opts || {};
  // applyTransform() wraps everything in <g class="view-transform">
  // around the original <g transform="scale(1,-1)">.  The silhouette
  // layer has to sit *inside* the scale-flip group, otherwise its
  // raw (u,v) coordinates draw off-screen.
  const scaleG = svg.querySelector('g[transform="scale(1,-1)"]')
              || svg.querySelector('.view-transform > g')
              || svg.querySelector(':scope > g');
  if (!scaleG) return;
  scaleG.querySelector(':scope > g.layer-silhouette')?.remove();
  if (!highlights || !highlights.size) return;

  const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  layer.setAttribute('class', 'layer-silhouette');
  layer.setAttribute('pointer-events', 'none');

  const fid = fileSel.value, vid = viewSel.value;
  const groupOn = $('sty-group-mode')?.checked ?? false;
  const idxList = [...highlights];

  // ---- 1) FILL polygon (only if shade is on) -----------------------
  // The fill uses the per-part visible-footprint polygon (closed
  // boundary tracing only what the user actually sees), so the fill
  // never bleeds into occluder areas.  Same data already cached for
  // the bold-edge stroke -- no extra fetch.
  if (fillOn) {
    const fillSubpaths = [];
    const pushPolylines = (polys) => {
      polys.forEach(pl => {
        if (!pl || pl.length < 2) return;
        const d = 'M ' + pl.map(p => p[0].toFixed(2) + ' ' + p[1].toFixed(2))
                          .join(' L ') + ' Z';
        fillSubpaths.push(d);
      });
    };
    for (const idx of idxList) {
      const fp = _getFootprint(fid, vid, idx);
      if (fp && fp.length) pushPolylines(fp);
    }
    if (fillSubpaths.length) {
      const fillPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      fillPath.setAttribute('d', fillSubpaths.join(' '));
      fillPath.setAttribute('fill', fillColor);
      fillPath.setAttribute('fill-opacity', String(fillAlpha));
      fillPath.setAttribute('fill-rule', 'evenodd');
      fillPath.setAttribute('stroke', 'none');
      layer.appendChild(fillPath);
    }
  }

  // ---- 2) BOLD EDGE stroke ----------------------------------------
  // Use the rasterized FOOTPRINT polygon (closed loop per visible
  // piece).  When a footprint hasn't arrived yet we DON'T draw the
  // old open-polyline fallback -- that "half-baked outline" was
  // misleading.  Instead we surface a "loading shaded outline..."
  // status near the canvas so the user knows the closed loop is
  // still computing, and only draw real closed loops here.
  const strokeSubpaths = [];
  const _waitingIdx = [];
  let _withFp = 0;
  for (const idx of idxList) {
    const fp = _getFootprint(fid, vid, idx);
    if (fp && fp.length) {
      _withFp++;
      fp.forEach(pl => {
        if (!pl || pl.length < 2) return;
        strokeSubpaths.push(
          'M ' + pl.map(p => p[0].toFixed(2) + ' ' + p[1].toFixed(2))
                  .join(' L ') + ' Z'
        );
      });
    } else {
      _waitingIdx.push(idx);
    }
  }
  if (_waitingIdx.length && typeof showShadedOutlineLoading === 'function') {
    showShadedOutlineLoading(_waitingIdx.length);
  } else if (!_waitingIdx.length
              && typeof hideShadedOutlineLoading === 'function') {
    hideShadedOutlineLoading();
  }
  if (strokeSubpaths.length) {
    const strokePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    strokePath.setAttribute('d', strokeSubpaths.join(' '));
    strokePath.setAttribute('fill', 'none');
    strokePath.setAttribute('stroke', strokeColor);
    strokePath.setAttribute('stroke-width', String(strokeWidth));
    strokePath.setAttribute('stroke-linejoin', 'round');
    strokePath.setAttribute('stroke-linecap', 'round');
    // Dashed indicator for transient (click-feedback) silhouette so the
    // user can tell at a glance that it's "you've selected this" and
    // not "this is the final styled output".  The persistent silhouette
    // (renderPersistentSilhouettes) leaves opts unset -> solid stroke.
    if (opts.dashed) {
      const dash = Math.max(0.8, strokeWidth * 3);
      const gap  = Math.max(0.4, strokeWidth * 1.5);
      strokePath.setAttribute('stroke-dasharray', dash + ' ' + gap);
    }
    layer.appendChild(strokePath);
  }

  // Sit BEHIND visible edge layers so the rest of the edges still draw
  // on top, but in front of the hidden layers.
  scaleG.insertBefore(layer, scaleG.firstChild);
}

// Pre-fetch the visible-footprint polygons for EVERY part in the current
// view.  Server rasterizes the assembly once per view (~2-5s), then
// every per-part lookup is cached.  We then inject a transparent
// hit-fill layer so clicks land anywhere inside a part, not just on
// its edges.  Closed-loop bold stroke uses the same data.
// Per-view serialisation: only one POST /api/part_footprints at a
// time per (fid, vid).  Without this, the editor's three concurrent
// callers (prefetch, selection footprints, group silhouettes) all
// fire on figure load, each kicks off a raster server-side, and they
// queue on _HLR_LOCK.  If any one stalls (we saw OCCT meshing get
// stuck in C++), the whole pipeline stops responding.  Serialising
// at the client makes the first request warm the server cache; the
// next two are guaranteed cache hits.
const _footprintInflight = new Map();   // key = fid|vid -> Promise
async function _runFootprintRequest(fid, vid, body) {
  const key = fid + '|' + vid;
  const prior = _footprintInflight.get(key);
  const next = (async () => {
    if (prior) {
      try { await prior; } catch (_e) {}
    }
    return fetch(API_BASE + '/api/part_footprints', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  })();
  _footprintInflight.set(key, next);
  try {
    return await next;
  } finally {
    if (_footprintInflight.get(key) === next) {
      _footprintInflight.delete(key);
    }
  }
}

// Fetch the visible-footprint polygon for ONLY the currently-selected
// parts.  First request in a view pays the full assembly raster
// cost (~5-30s depending on source) but the server caches every
// part's footprint after that, so further calls are instant.  This
// is what powers the bold-edge "broken into pieces" rendering.
async function fetchSelectedFootprints() {
  const fid = fileSel.value, vid = viewSel.value;
  const st = getState(fid, vid);
  if (!st.highlights || !st.highlights.size) return;
  if (typeof API_BASE !== 'string') return;
  const apiBase = API_BASE;

  const missing = [];
  for (const idx of st.highlights) {
    if (!_getFootprint(fid, vid, idx)) missing.push(idx);
  }
  if (!missing.length) {
    if (_DBG_ON) console.log('[footprint] no missing parts, skipping fetch');
    return;
  }

  // FAST PATH: opt-in GPU raster.  Browser-side render of the GLB
  // into an ID-coloured texture, ~50-200 ms for a typical assembly
  // vs 30-60 s server-side.  On failure (any of the coordinate
  // assumptions miss), returns null and falls through to the server
  // path so the bold edge still draws -- just more slowly.
  if (_GPU_RASTER_ON) {
    try {
      const t0 = performance.now();
      const polys = _gpuRasterFootprints(fid, vid, missing);
      if (polys) {
        let n = 0;
        for (const [idx, pl] of Object.entries(polys)) {
          _setFootprint(fid, vid, parseInt(idx), pl);
          n += pl.length;
        }
        const dt = performance.now() - t0;
        console.log(`[footprint] GPU raster ${missing.length} parts in `
                    + `${dt.toFixed(0)}ms, ${n} polylines`);
        applyHighlights();
        return;
      }
    } catch (e) {
      console.warn('[footprint] GPU raster failed, falling back:', e);
    }
  }

  console.log('[footprint] fetching ' + missing.length + ' parts: '
              + JSON.stringify(missing) + ' for fid=' + fid + ' vid=' + vid);

  // Camera body (same logic as the other fetchers)
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  const body = { file_id: fid };
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  } else if (ve && ve.view_dir) {
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  } else {
    console.warn('[footprint] no camera context for fid=' + fid + ' vid=' + vid
                  + ' -- bailing');
    return;
  }
  body.part_indices = missing;
  console.log('[footprint] camera body:', JSON.stringify(body));
  try {
    const r = await _runFootprintRequest(fid, vid, body);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (fileSel.value !== fid || viewSel.value !== vid) return;
    let _empty = 0, _nonempty = 0;
    for (const [idxStr, polys] of Object.entries(data.polylines || {})) {
      if (!polys || !polys.length) _empty++;
      else _nonempty++;
      _setFootprint(fid, vid, parseInt(idxStr), polys);
    }
    console.log('[footprint] returned ' + _nonempty + ' parts with polys, '
                + _empty + ' empty.  Stats: '
                + JSON.stringify(data.stats || {}));
    applyHighlights();   // re-render bold edge with the new footprints
  } catch (e) {
    console.warn('[footprint] fetch failed:', e.message || e);
  }
}

async function prefetchFootprintsForCurrentView() {
  const fid = fileSel.value, vid = viewSel.value;
  const vkey = _fpViewKey(fid, vid);
  if (_footprintViewFetched.has(vkey)) return;
  if (typeof API_BASE !== 'string') return;
  const apiBase = API_BASE;
  // Resolve camera body (same logic as fetchTrueSilhouettes)
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  const body = { file_id: fid };
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  } else if (ve && ve.view_dir) {
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  } else {
    return;
  }
  body.part_indices = fe.parts.map(p => p.idx);
  // Ask the server for the combined-assembly silhouette too; cheap on
  // top of the raster the server is already doing.  We inject it as a
  // separate layer so the baseline Silhouette slider drives ONE bold
  // closed loop around the union -- not per-part outlines with seams.
  body.want_assembly = true;
  _footprintViewFetched.add(vkey);   // claim BEFORE the await
  try {
    const r = await _runFootprintRequest(fid, vid, body);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (fileSel.value !== fid || viewSel.value !== vid) return;   // stale
    for (const [idxStr, polys] of Object.entries(data.polylines || {})) {
      _setFootprint(fid, vid, parseInt(idxStr), polys);
    }
    if (Array.isArray(data.assembly)) {
      _setAssemblySilhouette(fid, vid, data.assembly);
      _renderAssemblySilhouetteLayer(fid, vid);
    }
    injectHitFillLayer(fid, vid);
    applyHighlights();   // re-render bold stroke using footprints
  } catch (e) {
    console.warn('[footprint] prefetch failed:', e.message || e);
    _footprintViewFetched.delete(vkey);
  }
}

// Inject (or refresh) the combined assembly silhouette layer in the
// currently-active SVG.  The layer sits INSIDE the scale(1,-1) wrapper
// so its (u, v) polylines render correctly.  Drawing-weight CSS targets
// .layer-assembly_silhouette so the Silhouette slider controls its width.
function _renderAssemblySilhouetteLayer(fid, vid) {
  document.querySelectorAll(
    `.svg-pane[data-file="${fid}"][data-view="${vid}"]`
  ).forEach(pane => {
    const svg = pane.querySelector('svg');
    if (!svg) return;
    const scaleG = svg.querySelector('g[transform="scale(1,-1)"]')
                || svg.querySelector('.view-transform > g')
                || svg.querySelector(':scope > g');
    if (!scaleG) return;
    scaleG.querySelector(':scope > g.layer-assembly_silhouette')?.remove();
    const polys = _getAssemblySilhouette(fid, vid);
    if (!polys || !polys.length) return;
    const layer = document.createElementNS(
      'http://www.w3.org/2000/svg', 'g');
    layer.setAttribute('class', 'layer layer-assembly_silhouette');
    layer.setAttribute('fill', 'none');
    // stroke + width are set by the drawing-weight CSS; defaults
    // here as a fallback for the first paint.
    layer.setAttribute('stroke', '#000');
    layer.setAttribute('stroke-width', '0.7');
    layer.setAttribute('stroke-linejoin', 'round');
    layer.setAttribute('stroke-linecap', 'round');
    layer.setAttribute('pointer-events', 'none');
    const d = polys.map(pl =>
      'M ' + pl.map(p => p[0].toFixed(2) + ' ' + p[1].toFixed(2))
              .join(' L ') + ' Z'
    ).join(' ');
    const path = document.createElementNS(
      'http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    layer.appendChild(path);
    // Sit ON TOP of all other line layers so the combined outline
    // visually dominates the seams between parts.
    scaleG.appendChild(layer);
  });
}

// Hit-fill click-anywhere layer was here -- removed because the
// rasterized footprints sometimes leak pixels into neighbour parts,
// which made clicks land on the wrong part.  Click targeting now goes
// through the existing 3mm-stroke hit layer (always present in the
// baked SVG); user clicks near any visible edge to select.  The
// FOOTPRINT data is still used for the bold-edge closed loop --
// that's read-only display, no click logic depends on it.
function injectHitFillLayer(_fid, _vid) { /* no-op (reverted) */ }

// ===== GPU footprint rasteriser (opt-in via ?gpu_raster=1) ===========
// Replaces a 30-60 s server-side raster with a ~50-200 ms browser
// readback.  The browser already has the GLB loaded for the 3D pane;
// we can re-render it into an off-screen RGB-encoded ID buffer and
// trace contours per part_idx without ever touching the OCCT shape.
//
// CRITICAL coordinate-system contract -- match this and the outlines
// land on the SVG; miss it and you'll see the 322mm-offset bug again:
//   * Active SVG's viewBox is "{u_min} {-v_max} {w} {h}" with a
//     <g transform="scale(1,-1)"> wrapper, so polylines inside the SVG
//     are in (u, v).  Our cached footprints must also be in (u, v).
//   * We configure an OrthographicCamera with left=u_min, right=u_max,
//     top=v_max, bottom=v_min so each rendered pixel corresponds to a
//     known (u, v) sample.
//   * Camera position = focal - view_dir * far_offset; camera.lookAt(focal)
//     so the depth axis matches OCCT's projector frame.
const _GPU_RASTER_ON =
    (new URLSearchParams(location.search)).get('gpu_raster') === '1';
let _gpuRTarget = null;
let _gpuOffscreenScene = null;
let _gpuOffscreenCam = null;

function _readViewBoxFromActiveSvg() {
  const pane = activePane();
  const svg = pane && pane.querySelector('svg');
  if (!svg) return null;
  const vb = svg.getAttribute('viewBox');
  if (!vb) return null;
  const parts = vb.split(/\\s+/).map(parseFloat);
  if (parts.length !== 4 || parts.some(Number.isNaN)) return null;
  // viewBox = (x0, y0, w, h) where x0 = u_min, y0 = -v_max
  const [x0, y0, w, h] = parts;
  return {
    u_min: x0, u_max: x0 + w,
    v_min: -(y0 + h), v_max: -y0,
  };
}

// Encode a part_idx into a unique RGB triplet that's recoverable from
// a Uint8 readback.  +1 offset so idx 0 doesn't collide with the
// black background (no part).
function _encodePartColor(idx) {
  const v = idx + 1;
  return ((v & 0xff) << 16) | (((v >> 8) & 0xff) << 8) | ((v >> 16) & 0xff);
}
function _decodePartIdFromRgb(r, g, b) {
  const v = (r << 16) | (g << 8) | b;
  return v ? v - 1 : -1;
}

function _gpuRasterFootprints(fid, vid, partIndices) {
  // Returns null on any failure -> caller falls back to server path.
  if (!_GPU_RASTER_ON) return null;
  if (!renderer || !active) return null;
  const bounds = _readViewBoxFromActiveSvg();
  if (!bounds) return null;
  const vidx = new Set(partIndices);
  if (!vidx.size) return {};

  // Resolve the view direction the SVG was rendered with.
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  let viewDir = ve?.view_dir;
  // For live SVGs the catalogue carries the latest direction set by
  // injectLiveSVG; the existing flow already keeps it current.
  if (!viewDir || viewDir.length !== 3) return null;

  // Render target: 1500x1500 RGB ID buffer.  PixelType.UnsignedByte
  // keeps the readback fast and is plenty of precision for IDs up to
  // 16.7M parts.
  const RES = 1500;
  if (!_gpuRTarget || _gpuRTarget.width !== RES) {
    if (_gpuRTarget) _gpuRTarget.dispose();
    _gpuRTarget = new THREE.WebGLRenderTarget(RES, RES, {
      type: THREE.UnsignedByteType,
      format: THREE.RGBAFormat,
      depthBuffer: true,
      stencilBuffer: false,
    });
  }

  // Build (or reuse) a parallel scene that holds id-coloured meshes
  // for the active source.  We can't reuse 'scene' because we'd have
  // to swap every material's color/material on every raster.  Clone
  // the meshes' geometries (cheap) and give each its own BasicMaterial.
  if (!_gpuOffscreenScene
      || _gpuOffscreenScene.userData.file_id !== fid) {
    if (_gpuOffscreenScene) {
      _gpuOffscreenScene.traverse(o => {
        if (o.isMesh) {
          o.material.dispose();
        }
      });
    }
    _gpuOffscreenScene = new THREE.Scene();
    _gpuOffscreenScene.background = new THREE.Color(0x000000);
    _gpuOffscreenScene.userData.file_id = fid;
    // Walk the active group and clone every mesh with a flat
    // id-coloured BasicMaterial; preserve world transform.
    active.updateMatrixWorld();
    active.traverse(o => {
      if (!o.isMesh) return;
      const idx = _partIdxOf(o);
      if (idx == null) return;
      const color = new THREE.Color(_encodePartColor(idx));
      const mat = new THREE.MeshBasicMaterial({
        color, side: THREE.DoubleSide,
        // No tone-mapping, no IBL: the colour MUST come out byte-
        // identical to what we asked for.
        toneMapped: false,
      });
      const m = new THREE.Mesh(o.geometry, mat);
      m.applyMatrix4(o.matrixWorld);
      _gpuOffscreenScene.add(m);
    });
  }

  // Camera: OrthographicCamera matched to the SVG's viewBox so each
  // rendered pixel is a known (u, v) sample.
  if (!_gpuOffscreenCam) {
    _gpuOffscreenCam = new THREE.OrthographicCamera(-1, 1, 1, -1, -1e6, 1e6);
  }
  _gpuOffscreenCam.left   = bounds.u_min;
  _gpuOffscreenCam.right  = bounds.u_max;
  _gpuOffscreenCam.top    = bounds.v_max;
  _gpuOffscreenCam.bottom = bounds.v_min;
  // Place the camera along the view direction, looking at the bbox
  // centre.  Distance is large because the ortho frustum has its own
  // near/far.
  const cu = (bounds.u_min + bounds.u_max) / 2;
  const cv = (bounds.v_min + bounds.v_max) / 2;
  // Reconstruct a 3D centre that, when projected onto x_axis/y_axis,
  // lands at (cu, cv).  Since OCCT's HLR uses x_axis = up x view_dir
  // (normalised) and y_axis = view_dir x x_axis, we'd need those
  // axes here too.  Easier: use the bounds bbox centre in WORLD via
  // the existing 'active' group's bbox + the projection axes.
  // ... For first cut, target the world origin and let lookAt do the
  // rotation; the ortho frustum is sized in (u, v), so framing is
  // already correct.
  const vd = new THREE.Vector3(viewDir[0], viewDir[1], viewDir[2])
    .normalize();
  _gpuOffscreenCam.position.copy(vd).multiplyScalar(1e4);
  _gpuOffscreenCam.up.set(0, 0, 1);  // matches OCCT projector convention
  _gpuOffscreenCam.lookAt(0, 0, 0);
  _gpuOffscreenCam.updateProjectionMatrix();

  // Render and read back.
  const prevTarget = renderer.getRenderTarget();
  renderer.setRenderTarget(_gpuRTarget);
  renderer.clear();
  renderer.render(_gpuOffscreenScene, _gpuOffscreenCam);
  renderer.setRenderTarget(prevTarget);

  const buf = new Uint8Array(RES * RES * 4);
  renderer.readRenderTargetPixels(_gpuRTarget, 0, 0, RES, RES, buf);

  // Build a Uint32 id map (per pixel) for fast lookup.
  const ids = new Int32Array(RES * RES);
  for (let i = 0, p = 0; i < ids.length; i++, p += 4) {
    ids[i] = _decodePartIdFromRgb(buf[p], buf[p + 1], buf[p + 2]);
  }

  // For each requested part_idx, trace closed contour(s) of its mask.
  const px_per_mm_u = (RES - 2) / (bounds.u_max - bounds.u_min);
  const px_per_mm_v = (RES - 2) / (bounds.v_max - bounds.v_min);
  const result = {};
  for (const idx of partIndices) {
    const polys = _traceContoursFromIdMask(ids, RES, RES, idx);
    // Convert pixel -> (u, v).  The GL coordinate system has Y up,
    // so row 0 is at the bottom; v = v_min + row / px_per_mm_v.
    const uvPolys = polys.map(pl =>
      pl.map(([px, py]) => [
        bounds.u_min + px / px_per_mm_u,
        bounds.v_min + py / px_per_mm_v,
      ])
    );
    result[idx] = uvPolys;
  }
  return result;
}

// Moore-neighbour contour trace: for each part_idx, find connected
// components in the ID buffer and walk each component's boundary in
// order to produce a closed polyline.  Coarse-grained -- emits one
// polyline per component, optionally simplified with DP later.
function _traceContoursFromIdMask(ids, w, h, partIdx) {
  // Build a binary mask in-place
  const want = partIdx;
  const seen = new Uint8Array(w * h);
  const polys = [];
  // Direction tables for Moore-neighbour walking (clockwise from N)
  const DX = [0, 1, 1, 1, 0, -1, -1, -1];
  const DY = [1, 1, 0, -1, -1, -1, 0, 1];

  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      if (seen[i] || ids[i] !== want) continue;
      // Component-seed: only start from the topmost-leftmost pixel
      // of the component (skip pixels with a same-id neighbour above
      // or left to avoid re-tracing).
      const upMatch = (y > 0 && ids[i - w] === want);
      const leftMatch = (x > 0 && ids[i - 1] === want);
      if (upMatch || leftMatch) {
        seen[i] = 1;
        continue;
      }
      // Trace boundary starting here, walking the perimeter.
      // We start by entering from the west (direction 6 = -x).
      const polyline = [];
      let cx = x, cy = y, dir = 6;
      const start = i;
      const startDir = dir;
      let steps = 0;
      const MAX = w * h;
      do {
        polyline.push([cx + 0.5, cy + 0.5]);
        seen[cy * w + cx] = 1;
        // Look around starting from (dir + 6) mod 8 (one step CCW of
        // arrival direction)
        let found = false;
        let trydir = (dir + 6) & 7;
        for (let k = 0; k < 8; k++) {
          const nx = cx + DX[trydir];
          const ny = cy + DY[trydir];
          if (nx >= 0 && nx < w && ny >= 0 && ny < h
              && ids[ny * w + nx] === want) {
            cx = nx; cy = ny; dir = trydir; found = true;
            break;
          }
          trydir = (trydir + 1) & 7;
        }
        if (!found) break;
        steps++;
        if (steps > MAX) break;
      } while (!(cy * w + cx === start && dir === startDir));
      if (polyline.length >= 3) {
        polyline.push(polyline[0]);   // close
        // Crude DP-style simplification: drop points whose
        // perpendicular distance from the line through their neighbours
        // is below 1 px.  Cheap; keeps the loop under ~200 points for
        // a typical part footprint.
        polys.push(_dpSimplifyPixel(polyline, 1.0));
      }
    }
  }
  return polys;
}

function _dpSimplifyPixel(pl, tol) {
  if (pl.length < 4) return pl;
  const keep = new Uint8Array(pl.length);
  keep[0] = keep[pl.length - 1] = 1;
  const stack = [[0, pl.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    if (b - a < 2) continue;
    let maxD = 0, maxI = -1;
    const ax = pl[a][0], ay = pl[a][1];
    const bx = pl[b][0], by = pl[b][1];
    const dx = bx - ax, dy = by - ay;
    const denom = Math.hypot(dx, dy) || 1;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs(dy * pl[i][0] - dx * pl[i][1] + bx * ay - by * ax) / denom;
      if (d > maxD) { maxD = d; maxI = i; }
    }
    if (maxD > tol) {
      keep[maxI] = 1;
      stack.push([a, maxI]);
      stack.push([maxI, b]);
    }
  }
  const out = [];
  for (let i = 0; i < pl.length; i++) if (keep[i]) out.push(pl[i]);
  return out;
}
// ===== end GPU footprint rasteriser =================================

// Andrew's monotone-chain convex hull on a list of (x, y) pairs.
function _convexHull(points) {
  if (points.length < 3) return points.slice();
  const pts = points.slice().sort((a, b) =>
    a[0] === b[0] ? a[1] - b[1] : a[0] - b[0]);
  const cross = (o, a, b) =>
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [];
  for (const p of pts) {
    while (lower.length >= 2 &&
           cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) {
      lower.pop();
    }
    lower.push(p);
  }
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i--) {
    const p = pts[i];
    while (upper.length >= 2 &&
           cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) {
      upper.pop();
    }
    upper.push(p);
  }
  upper.pop();
  lower.pop();
  return lower.concat(upper);
}

// Build a per-part convex-hull hit layer so clicks land anywhere inside
// a part, not just near its edges.  Hulls are computed from the visible
// polyline points in the baked SVG -- so each hull only contains THIS
// part's points, never a neighbour's.  Sorted biggest-first so small
// parts paint last and win clicks where their hulls overlap (e.g. a
// pivot pin sitting on top of a plate).
function injectHitHullsLayer() {
  const svg = activeSvg();
  if (!svg) return;
  const scaleG = svg.querySelector('g[transform="scale(1,-1)"]')
              || svg.querySelector('.view-transform > g')
              || svg.querySelector(':scope > g');
  if (!scaleG) return;
  scaleG.querySelector(':scope > g.layer-hit-hull')?.remove();
  // Geometry is being (re)built -> drop the nearest-edge click cache so
  // _resolvePartClick recomputes against the new edges.
  svg._edgeVertsCache = null;

  // Collect points per idx from outline_v + sharp_v + smooth_v
  const partPoints = new Map();
  ['.layer-outline_v', '.layer-sharp_v', '.layer-smooth_v'].forEach(sel => {
    svg.querySelectorAll(sel + ' .part').forEach(partG => {
      const idx = parseInt(partG.dataset.part);
      if (Number.isNaN(idx)) return;
      partG.querySelectorAll('path').forEach(p => {
        const d = p.getAttribute('d') || '';
        const toks = d.match(/-?\d+(?:\.\d+)?/g);
        if (!toks) return;
        if (!partPoints.has(idx)) partPoints.set(idx, []);
        const arr = partPoints.get(idx);
        for (let i = 0; i + 1 < toks.length; i += 2) {
          arr.push([parseFloat(toks[i]), parseFloat(toks[i + 1])]);
        }
      });
    });
  });

  // Per-idx hull + area for sort
  const hulls = [];
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  const labelOf = idx => fe?.parts.find(p => p.idx === idx)?.label || '';
  for (const [idx, pts] of partPoints) {
    if (pts.length < 3) continue;
    const hull = _convexHull(pts);
    if (hull.length < 3) continue;
    let s = 0;
    for (let i = 0; i < hull.length; i++) {
      const j = (i + 1) % hull.length;
      s += hull[i][0] * hull[j][1] - hull[j][0] * hull[i][1];
    }
    hulls.push({ idx, hull, area: Math.abs(s) * 0.5 });
  }
  hulls.sort((a, b) => b.area - a.area);

  const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  layer.setAttribute('class', 'layer-hit-hull');
  layer.setAttribute('fill', 'rgba(0,0,0,0)');
  layer.setAttribute('stroke', 'none');
  layer.setAttribute('pointer-events', 'fill');
  for (const e of hulls) {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', 'part part-' + String(e.idx).padStart(3, '0'));
    g.setAttribute('data-part', String(e.idx));
    g.setAttribute('data-label', labelOf(e.idx));
    const d = 'M ' + e.hull.map(p => p[0].toFixed(1) + ' ' + p[1].toFixed(1))
                            .join(' L ') + ' Z';
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    g.appendChild(path);
    layer.appendChild(g);
  }
  // Append last so the hull layer is on top of every visible-edge layer
  // AND the 3mm stroke hit layer.  Filled hull catches the click
  // anywhere inside the convex hull of the part.
  scaleG.appendChild(layer);
}

// Request true per-part silhouettes from the server for any highlighted
// parts we don't already have cached.  When the response arrives, the
// cache is populated and applyHighlights() is re-run to swap the local
// approximation for the closed-loop server polylines.
let _silFetchToken = 0;
async function fetchTrueSilhouettes() {
  const fid = fileSel.value, vid = viewSel.value;
  const st = getState(fid, vid);
  if (!st.highlights || !st.highlights.size) return;
  if (typeof API_BASE !== 'string') return;             // viewer-only build (no server)
  const apiBase = API_BASE;
  // Only fetch closed-profile silhouettes when the fill (shade) is on --
  // the bold edge uses the local assembly-HLR paths so occluded parts
  // are correctly chopped without needing the server.
  if (!$('sty-fill-on').checked) return;

  // Resolve the camera body for THIS view -- preset views use the
  // catalogue view_dir + focal=(0,0,0); the Live view reuses the eye/
  // target cached when /api/render fired.
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  const body = { file_id: fid };
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  } else if (ve && ve.view_dir) {
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  } else {
    return;
  }

  const groupOn = $('sty-group-mode')?.checked ?? false;
  const idxList = [...st.highlights];

  // GROUP REQUEST: when "outline as group" is on and 2+ parts are
  // selected, ask the server for a single compound silhouette.  Falls
  // back to per-part fetch below if disabled or single-select.
  if (groupOn && idxList.length >= 2) {
    if (_getGroupSil(fid, vid, idxList)) {
      applyHighlights();   // already cached -- just re-render
      return;
    }
    const body2 = Object.assign({}, body);
    body2.part_indices = idxList;
    body2.group = true;
    const token = ++_silFetchToken;
    try {
      const r = await fetch(apiBase + '/api/part_silhouettes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body2),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      if (token !== _silFetchToken) return;
      if (fileSel.value !== fid || viewSel.value !== vid) return;
      _setGroupSil(fid, vid, idxList, data.polylines?.group || []);
      applyHighlights();
    } catch (e) {
      console.warn('[silhouette] group fetch failed:', e.message || e);
    }
    return;
  }

  // PER-PART REQUEST (single-select or group mode disabled).
  const missing = [];
  for (const idx of st.highlights) {
    if (!_getTrueSil(fid, vid, idx)) missing.push(idx);
  }
  if (!missing.length) return;
  body.part_indices = missing;

  const token = ++_silFetchToken;
  try {
    const r = await fetch(apiBase + '/api/part_silhouettes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (token !== _silFetchToken) return;
    if (fileSel.value !== fid || viewSel.value !== vid) return;
    for (const [idxStr, polys] of Object.entries(data.polylines || {})) {
      _setTrueSil(fid, vid, parseInt(idxStr), polys);
    }
    applyHighlights();   // re-render silhouette layer with the new data
  } catch (e) {
    console.warn('[silhouette] fetch failed:', e.message || e);
  }
}

// Invalidate cache + refetch when the view (camera) changes, since the
// (u,v) space differs per projection.
function _invalidateSilCache() {
  _trueSilCache.clear();
  _groupSilCache.clear();
  _footprintCache.clear();
  _footprintViewFetched.clear();
}
viewSel.addEventListener('change', () => {
  _invalidateSilCache();
  // Silhouette fetch only fires if shade is on (guarded inside).
  setTimeout(fetchTrueSilhouettes, 0);
});
fileSel.addEventListener('change', () => {
  _invalidateSilCache();
});
// Group-mode toggle: re-render immediately (uses cached data if any),
// then fetch the missing form (group vs per-part) on the side.
$('sty-group-mode')?.addEventListener('change', () => {
  applyHighlights();
  setTimeout(fetchTrueSilhouettes, 0);
});
// Turning shade ON triggers the closed-silhouette fetch (the fill needs
// a closed profile from server-side per-part HLR).
$('sty-fill-on')?.addEventListener('change', () => {
  applyHighlights();
  if ($('sty-fill-on').checked) setTimeout(fetchTrueSilhouettes, 0);
});
// Render the "Applied styles" list in the sidebar.  Each row shows a
// colour swatch + part label + width, plus inline "edit" (select that
// part + load its style into the controls) and "delete" (remove).
function renderAppliedStylesList() {
  const listEl = document.getElementById('applied-styles-list');
  if (!listEl) return;
  const fid = fileSel.value;
  const m = loadPartStyles(fid);
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const entries = Object.entries(m);
  if (!entries.length) {
    listEl.innerHTML = '<li style="color: var(--muted); padding: 4px 0; font-style: italic;">none yet</li>';
    return;
  }
  entries.sort((a, b) => parseInt(a[0]) - parseInt(b[0]));
  listEl.innerHTML = '';
  for (const [idxStr, style] of entries) {
    const idx = parseInt(idxStr);
    const part = fe?.parts.find(p => p.idx === idx);
    const label = part ? part.label : ('part_' + idxStr);
    const li = document.createElement('li');
    li.style.cssText = 'display:flex; align-items:center; gap:6px; padding:3px 4px; '
      + 'border-radius:3px; cursor:pointer;';
    li.title = `part_${idxStr} - ${label}`;

    const swatch = document.createElement('span');
    swatch.style.cssText = 'display:inline-block; width:14px; height:10px; '
      + `background:${style.fillOn ? style.fillColor : '#fff'}; `
      + `border:2px solid ${style.stroke || '#00836a'};`;
    li.appendChild(swatch);

    const text = document.createElement('span');
    text.style.cssText = 'flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;';
    text.textContent = `[${idxStr}] ${label}`;
    li.appendChild(text);

    const wInfo = document.createElement('span');
    wInfo.style.cssText = 'color: var(--muted); font-size:10px;';
    wInfo.textContent = `${(style.width ?? 3).toFixed(1)}mm`;
    li.appendChild(wInfo);

    const editBtn = document.createElement('button');
    editBtn.textContent = '✎';
    editBtn.title = 'Select this part and load its style into the editor';
    editBtn.style.cssText = 'padding:0 5px; font-size:12px; line-height:1.4;';
    editBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      // Load the style into the controls so the user sees the values
      if (style.stroke) $('sty-stroke').value = style.stroke;
      if (style.width != null) {
        $('sty-width').value = String(style.width);
        $('sty-width-val').textContent = style.width.toFixed(1);
      }
      if (style.opacity != null) {
        $('sty-opacity').value = String(style.opacity);
        $('sty-opacity-val').textContent = style.opacity.toFixed(2);
      }
      if (style.dash != null) $('sty-dash').value = style.dash || '';
      if (style.fillOn != null) $('sty-fill-on').checked = !!style.fillOn;
      if (style.fillColor) $('sty-fill').value = style.fillColor;
      if (style.fillAlpha != null) {
        $('sty-fill-opacity').value = String(style.fillAlpha);
        $('sty-fill-opacity-val').textContent = style.fillAlpha.toFixed(2);
      }
      // Select that part
      togglePartHighlight(idx, {append: false});
    });
    li.appendChild(editBtn);

    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.title = 'Remove this applied style';
    delBtn.style.cssText = 'padding:0 5px; font-size:12px; line-height:1.4; color:#c44;';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const m2 = loadPartStyles(fid);
      delete m2[idxStr];
      persistPartStyles(fid, m2);
      applyStyleSheet();
    });
    li.appendChild(delBtn);

    // Whole-row click also selects the part (without loading style)
    li.addEventListener('click', () => togglePartHighlight(idx, {append: false}));
    li.addEventListener('mouseenter', () => li.style.background = '#eef4f2');
    li.addEventListener('mouseleave', () => li.style.background = '');

    listEl.appendChild(li);
  }
}

// ---- Preset styles (project mode) ----------------------------------
// Five fixed IFU presets so figures across a project look consistent.
// Each preset packages stroke + width + fill so one click applies a
// fully-specified style to the current selection.  No color pickers,
// no sliders -- pick a vocabulary and stick to it.
const _STYLE_PRESETS = [
  { id: 'highlight', label: 'Highlight',
     style: { stroke: '#00836a', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: true, fillColor: '#cce6e0', fillAlpha: 0.35 } },
  { id: 'caution',   label: 'Caution',
     style: { stroke: '#b54708', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: true, fillColor: '#fff3e0', fillAlpha: 0.40 } },
  { id: 'info',      label: 'Info',
     style: { stroke: '#1e6fa1', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: true, fillColor: '#e0f0fa', fillAlpha: 0.40 } },
  { id: 'outline',   label: 'Outline only',
     style: { stroke: '#00836a', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: false } },
  { id: 'subtle',    label: 'Subtle',
     style: { stroke: '#52525b', width: 2.0, opacity: 0.85, dash: null,
                 fillOn: false } },
];

function _stylesMatch(a, b) {
  if (!a || !b) return false;
  // Loose equality on the fields that visually matter
  return a.stroke === b.stroke
      && Math.abs((a.width || 0) - (b.width || 0)) < 0.01
      && !!a.fillOn === !!b.fillOn
      && (!a.fillOn || (a.fillColor === b.fillColor
                         && Math.abs((a.fillAlpha || 0)
                                       - (b.fillAlpha || 0)) < 0.01));
}

function _renderPresetRow() {
  const row = document.getElementById('preset-row');
  const actions = document.getElementById('preset-actions');
  if (!row || row.children.length) return;   // already built
  for (const p of _STYLE_PRESETS) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'preset-btn';
    btn.dataset.presetId = p.id;
    btn.title = p.label;
    const sw = document.createElement('span');
    sw.className = 'preset-swatch';
    sw.style.background = p.style.fillOn
      ? p.style.fillColor : 'transparent';
    sw.style.border = '2.5px solid ' + p.style.stroke;
    btn.appendChild(sw);
    const lab = document.createElement('span');
    lab.textContent = p.label;
    btn.appendChild(lab);
    btn.addEventListener('click', () => _applyPreset(p));
    row.appendChild(btn);
  }
  if (actions) actions.style.display = '';
}

function _applyPreset(preset) {
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) {
    (window.IFU_UI?.toast || function(){})(
      'Select one or more parts first', 'error');
    return;
  }
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) m[idx] = { ...preset.style };
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
  // Mark active preset visually
  document.querySelectorAll('#preset-row .preset-btn').forEach(b => {
    b.classList.toggle('is-active', b.dataset.presetId === preset.id);
  });
}

function _refreshPresetActiveState() {
  // After a selection change, light up the preset that matches the
  // applied style of (any of) the selected parts -- otherwise clear.
  const row = document.getElementById('preset-row');
  if (!row) return;
  const st = getState(fileSel.value, viewSel.value);
  const m = loadPartStyles(fileSel.value);
  let activeId = null;
  if (st.highlights && st.highlights.size) {
    for (const idx of st.highlights) {
      const s = m[idx];
      if (!s) continue;
      const match = _STYLE_PRESETS.find(p => _stylesMatch(p.style, s));
      if (match) { activeId = match.id; break; }
    }
  }
  row.querySelectorAll('.preset-btn').forEach(b => {
    b.classList.toggle('is-active', b.dataset.presetId === activeId);
  });
}

// Build the row at startup
setTimeout(_renderPresetRow, 50);

// Preset-remove / clear-all in project mode
document.getElementById('btn-preset-remove')?.addEventListener('click', () => {
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) return;
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) delete m[idx];
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
  _refreshPresetActiveState();
});
document.getElementById('btn-preset-clear')?.addEventListener('click', () => {
  if (!confirm('Clear ALL styled parts on this figure?')) return;
  persistPartStyles(fileSel.value, {});
  applyStyleSheet();
  _refreshPresetActiveState();
});

$('btn-apply-style').addEventListener('click', () => {
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) {
    alert('Select one or more parts first.');
    return;
  }
  // Capture EVERY silhouette/fill control so the persistent overlay
  // matches the live highlight pixel-for-pixel.
  const style = {
    stroke:    $('sty-stroke').value,
    width:     parseFloat($('sty-width').value),
    opacity:   parseFloat($('sty-opacity').value),
    dash:      $('sty-dash').value || null,
    fillOn:    $('sty-fill-on').checked,
    fillColor: $('sty-fill').value,
    fillAlpha: parseFloat($('sty-fill-opacity').value),
  };
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) m[idx] = style;
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
});
$('btn-reset-style').addEventListener('click', () => {
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) return;
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) delete m[idx];
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
});
$('btn-reset-all-style').addEventListener('click', () => {
  if (!confirm('Clear ALL part style overrides for this source?')) return;
  persistPartStyles(fileSel.value, {});
  applyStyleSheet();
});
fileSel.addEventListener('change', applyStyleSheet);

// Expand the current selection to every leaf-Part under the same
// Onshape Assembly.  For each highlighted body, walk up to its parent
// node, then take every Part descendant of that parent (= the
// "sub-assembly" the body belongs to).  Falls back to a no-op when the
// source has no Onshape tree.
$('btn-expand-parent').addEventListener('click', () => {
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) {
    alert('Highlight at least one body first.');
    return;
  }
  if (!_leafByPartIdx.size) {
    alert("This source has no Onshape tree, so grouping by Onshape Assembly is not available here.");
    return;
  }
  const before = st.highlights.size;
  const newSel = new Set(st.highlights);
  for (const idx of st.highlights) {
    const leaf = _leafByPartIdx.get(idx);
    if (!leaf || !leaf._parent) continue;
    // Gather every leaf-Part descendant of the parent assembly, then
    // every solid index those leaves represent (multi-body friendly).
    const siblings = [];
    _flattenLeaves([leaf._parent], siblings);
    for (const s of siblings) {
      for (const i of (s._solid_indices || [])) newSel.add(i);
    }
  }
  st.highlights = newSel;
  applyHighlights();
  console.log(`[expand] selection ${before} -> ${newSel.size}`);
});

// Reset the depth-click cycle (the 3D handler also bumps it forward).
// Useful when the user wants to "start over" at a given pixel without
// having to move the mouse meaningfully far.
$('btn-cycle-deeper').addEventListener('click', () => {
  // Just nudge the cycle counter exposed by the module.
  if (window.IFU_VIEWER?.advanceClickCycle) {
    window.IFU_VIEWER.advanceClickCycle();
  } else {
    alert('Open the 3D pane first.');
  }
});

// init
setMode('smart');
refreshPane();
refreshTree();
refreshSavedViews();
applyStyleSheet();
loadUpAxisFor(fileSel.value);  // restore per-source up-axis on load
setLayout('2d');
