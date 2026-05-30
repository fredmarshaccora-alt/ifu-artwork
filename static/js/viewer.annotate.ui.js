// Annotation panel controller: wires the #annot-panel DOM to the explode /
// arrows / preset API exposed by viewer.module.js on window.IFU_VIEWER.
//
// Loaded as a module AFTER viewer.module.js, so by the time this runs the
// IFU_VIEWER.* annotation methods exist.  All handlers call those methods
// lazily (on user interaction), and the only load-time work is fetching the
// preset list + restoring the panel's collapsed state.

const V = () => window.IFU_VIEWER || {};
const $ = (id) => document.getElementById(id);

const API_BASE = (typeof window !== 'undefined' && window.IFU_API_BASE)
  ? window.IFU_API_BASE
  : ((location.protocol === 'http:' || location.protocol === 'https:')
      ? '' : 'http://localhost:5000');

// ---- collapse ----
const panel = $('annot-panel');
const collapseBtn = $('annot-collapse');
const body = $('annot-body');
let collapsed = localStorage.getItem('ifu:annot_collapsed') === '1';
function applyCollapsed() {
  if (body) body.style.display = collapsed ? 'none' : 'block';
  if (collapseBtn) collapseBtn.textContent = collapsed ? '+' : '−';
  localStorage.setItem('ifu:annot_collapsed', collapsed ? '1' : '0');
}
applyCollapsed();
collapseBtn?.addEventListener('click', () => { collapsed = !collapsed; applyCollapsed(); });

// ---- explode ----
const explodeRange = $('explode-range');
explodeRange?.addEventListener('input', () => {
  // slider 0..100 -> spread factor 0..1.5 (multiplier on each part's distance
  // from the assembly centre)
  const factor = (parseInt(explodeRange.value, 10) / 100) * 1.5;
  V().setExplodeFactor?.(factor);
});
$('explode-clear')?.addEventListener('click', () => {
  if (explodeRange) explodeRange.value = 0;
  V().clearExplode?.();
});

// SolidWorks/Onshape-style multi-select + triad.
$('explode-select')?.addEventListener('click', () => {
  const on = !$('explode-select').classList.contains('active');
  setMode(on ? 'explode' : 'none', on ? 'explode-select' : null);
});
$('explode-deselect')?.addEventListener('click', () => V().clearExplodeSelection?.());
document.querySelectorAll('.explode-axis').forEach((btn) => {
  btn.addEventListener('click', () => {
    const dist = parseFloat($('explode-dist')?.value || '50') || 0;
    const sign = parseFloat(btn.dataset.sign || '1');
    V().explodeNudge?.(btn.dataset.axis, sign * dist);
  });
});

function refreshExplodeList(detail) {
  const info = $('explode-selinfo');
  const list = $('explode-list');
  const sel = (detail && detail.selection) || V().getExplodeSelection?.() || [];
  const offsets = (detail && detail.offsets) || V().getExplodeOffsets?.() || {};
  if (info) {
    info.textContent = sel.length
      ? `${sel.length} part${sel.length > 1 ? 's' : ''} selected — drag the triad or use the axis buttons.`
      : 'Click “Select parts”, then click parts in the 3D view.';
  }
  if (list) {
    const idxs = Object.keys(offsets);
    list.innerHTML = '';
    if (!idxs.length) {
      list.innerHTML = '<div class="annot-hint">No parts exploded yet.</div>';
    } else {
      idxs.forEach((k) => {
        const o = offsets[k];
        const mag = Math.round(Math.hypot(o[0], o[1], o[2]));
        const row = document.createElement('div');
        row.className = 'annot-list-row';
        row.innerHTML = `<span>part ${k} · ${mag} mm</span>`;
        const rm = document.createElement('button');
        rm.className = 'annot-btn';
        rm.textContent = '✕';
        rm.title = 'Return this part to its assembled position';
        rm.style.cssText = 'padding:0 7px;margin-left:auto';
        rm.addEventListener('click', () => V().resetExplodePart?.(parseInt(k, 10)));
        row.appendChild(rm);
        list.appendChild(row);
      });
    }
  }
}
window.addEventListener('ifu:explode-changed', (e) => refreshExplodeList(e.detail));
refreshExplodeList();

// ---- arrows ----
function setMode(mode, activeBtnId) {
  V().setAnnotMode?.(mode);
  for (const id of ['arrow-straight', 'arrow-rotation', 'arrow-select', 'explode-select']) {
    $(id)?.classList.toggle('active', id === activeBtnId);
  }
}
$('arrow-straight')?.addEventListener('click', () => setMode('arrow-straight', 'arrow-straight'));
$('arrow-rotation')?.addEventListener('click', () => setMode('arrow-rotation', 'arrow-rotation'));
$('arrow-select')?.addEventListener('click', () => setMode('none', 'arrow-select'));
$('arrows-clear')?.addEventListener('click', () => V().clearArrows?.());

function refreshArrowList() {
  const list = $('arrow-list');
  if (!list) return;
  const arrows = V().getArrows?.() || [];
  // getArrows strips _id; pull ids from the annotation state instead
  const state = V().getAnnotationState?.();
  const defs = (state && state.arrows) || arrows;
  list.innerHTML = '';
  defs.forEach((a, i) => {
    const row = document.createElement('div');
    row.className = 'annot-list-row';
    const label = a.type === 'rotation' ? '↻ rotation' : '⟶ straight';
    row.innerHTML = `<span>${label} ${i + 1}</span>`;
    list.appendChild(row);
  });
  if (!defs.length) list.innerHTML = '<div class="annot-hint">No arrows yet.</div>';
}
window.addEventListener('ifu:arrows-changed', refreshArrowList);
refreshArrowList();

// When leaving arrow mode (e.g. annotation mode reset elsewhere), un-press btns.
window.addEventListener('ifu:annot-mode', (e) => {
  const mode = e.detail;
  if (mode !== 'explode') $('explode-select')?.classList.remove('active');
  if (mode === 'none') {
    for (const id of ['arrow-straight', 'arrow-rotation'])
      $(id)?.classList.remove('active');
  }
});

// ---- presets ("line style") ----
const presetSel = $('preset-sel');
const presetPreview = $('preset-preview');

function renderPreview(id) {
  if (!presetPreview || !id) return;
  presetPreview.innerHTML =
    `<img alt="preview" src="${API_BASE}/api/presets/${encodeURIComponent(id)}/preview.svg`
    + `?t=${Date.now()}"/>`;
}

async function loadPresets(selectId) {
  if (!presetSel) return;
  try {
    const r = await fetch(API_BASE + '/api/presets');
    if (!r.ok) return;
    const data = await r.json();
    const presets = data.presets || [];
    presetSel.innerHTML = '';
    for (const p of presets) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name + (p.builtin ? '' : ' *');
      presetSel.appendChild(opt);
    }
    const chosen = selectId || data.default_id || (presets[0] && presets[0].id);
    if (chosen) {
      presetSel.value = chosen;
      V().setPresetId?.(chosen);
      renderPreview(chosen);
    }
  } catch (_e) { /* server not up yet -- leave empty */ }
}
presetSel?.addEventListener('change', () => {
  V().setPresetId?.(presetSel.value);
  renderPreview(presetSel.value);
});
loadPresets();

// Let other code (settings screen) refresh the list after editing presets.
window.IFU_ANNOT = {
  reloadPresets: (selectId) => loadPresets(selectId),
  refreshArrowList,
  setExplodeSlider: (factor) => {
    if (explodeRange) explodeRange.value = Math.round((factor / 1.5) * 100);
  },
};
