"""Build the interactive HTML IFU viewer.

For each STEP file, runs per-solid HLR at a handful of standard views,
emits part-tagged SVG, then assembles every SVG into one self-contained
HTML page with:

  - file / view / mode pickers
  - pan + zoom (wheel = zoom, drag = pan)
  - click-to-highlight a part (all its paths go teal + heavy)
  - click-to-add callout arrows with text labels
  - layer toggles (silhouette / sharp / smooth / hidden)
  - export annotated SVG button
"""
from __future__ import annotations
import json
import time
import re
from pathlib import Path
import cadquery as cq

from t5_hlr_vector import (run_hlr_per_solid, write_svg_parts,
                            split_solids, STD_VIEWS, rotate_shape)


HERE = Path(__file__).parent
OUT = HERE / "out"


# Each source: (id, label, STEP path, hlr kwargs, pre-rotation (axis, angle))
# Pre-rotation re-orients the model so its longest axis lies along world X
# (the convention our STD_VIEWS are built around).  Presto's long axis is
# world Z by default - rotate -90deg about Y so Z -> X.
SOURCES = [
    ("siderail",  "Folding siderail",
     Path(r"C:\Users\FredMarshAccora\Downloads\P194-03-00 Folding siderail ASSE.STEP"),
     {"mesh_defl": 0.4, "sample_defl": 0.4},
     None),
    ("presto",    "Presto bed (top assembly)",
     HERE.parent / "step_lineart_test" / "presto_top_level.step",
     {"mesh_defl": 1.5, "sample_defl": 1.0},
     ((0, 1, 0), -90)),
    ("contesa",   "Contesa V2 / FL8 (top assembly)",
     HERE / "contesa_top_level.step",
     # 61MB STEP - coarser tessellation to keep mesh memory reasonable
     {"mesh_defl": 3.0, "sample_defl": 1.5},
     # Pre-rotation TBD from bbox inspection
     None),
]

# Three good IFU view dirs.  Defined for the bed convention (X=length, Z=up).
VIEWS = [
    ("iso",   "Iso 3/4 (front-right-above)", STD_VIEWS["iso"]),
    ("front", "Front elevation",              STD_VIEWS["front"]),
    ("side",  "Side elevation",               STD_VIEWS["side"]),
]


# Per-source view subset (e.g. for huge assemblies, only render iso to keep
# the inline HTML size manageable).  None = all views.
SOURCE_VIEW_SUBSET = {
    "contesa": ["iso"],   # 61MB STEP; one view first
}

# Per-source categories to OMIT from SVG to keep file size manageable.
# Hidden lines triple the polyline count on assemblies; drop them on
# big sources where the user wouldn't toggle them on anyway.
SOURCE_SKIP_CATEGORIES = {
    "contesa": ("hidden_outline", "hidden_sharp"),
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", s.lower()).strip("_")


def generate_svgs():
    """Run per-solid HLR for every (file, view) pair, write tagged SVG.

    Returns metadata list: [{file_id, file_label, views: [...], parts: [...]}]
    """
    catalogue = []
    for file_id, file_label, sp, hlr_kw, pre_rotate in SOURCES:
        if not sp.exists():
            print(f"  missing: {sp}"); continue
        print(f"\n===== {file_label} =====", flush=True)
        t0 = time.time()
        shape = cq.importers.importStep(str(sp)).val().wrapped
        print(f"  load {time.time()-t0:.1f}s", flush=True)
        if pre_rotate is not None:
            axis, angle = pre_rotate
            shape = rotate_shape(shape, axis, angle)
            print(f"  pre-rotated {angle} deg about {axis}", flush=True)
        # bbox snapshot
        import cadquery as _cq
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        bb = Bnd_Box(); BRepBndLib.Add_s(shape, bb)
        try:
            xmin, ymin, zmin, xmax, ymax, zmax = bb.Get()
            print(f"  bbox  X[{xmin:.0f}..{xmax:.0f}={xmax-xmin:.0f}]  "
                  f"Y[{ymin:.0f}..{ymax:.0f}={ymax-ymin:.0f}]  "
                  f"Z[{zmin:.0f}..{zmax:.0f}={zmax-zmin:.0f}]", flush=True)
        except Exception as e:
            print(f"  bbox failed: {e}", flush=True)

        # Build the part list once (solid count is view-independent)
        solid_meta = [
            {"idx": idx, "label": label}
            for idx, label, _solid in split_solids(shape)
        ]

        file_entry = {
            "file_id": file_id,
            "file_label": file_label,
            "parts": solid_meta,
            "views": [],
        }

        view_filter = SOURCE_VIEW_SUBSET.get(file_id)
        skip_cats = SOURCE_SKIP_CATEGORIES.get(file_id, ())
        for view_id, view_label, vd in VIEWS:
            if view_filter and view_id not in view_filter:
                continue
            print(f"\n  --- view {view_id} {vd} ---", flush=True)
            t1 = time.time()
            parts = run_hlr_per_solid(shape, vd, **hlr_kw)
            svg_name = f"{file_id}__{view_id}.svg"
            svg_path = OUT / svg_name
            bbox = write_svg_parts(parts, svg_path,
                                    precision=1, skip_categories=skip_cats)
            print(f"    svg {svg_path.stat().st_size/1024:.0f}KB  "
                  f"total {time.time()-t1:.1f}s", flush=True)
            file_entry["views"].append({
                "view_id": view_id,
                "view_label": view_label,
                "view_dir": list(vd),
                "svg_file": svg_name,
                "bbox": bbox,
            })
        catalogue.append(file_entry)
    return catalogue


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def build_html(catalogue):
    """Assemble the standalone viewer page."""
    # Inline each SVG verbatim, give each a unique id we can target.
    svg_blocks = []
    for fe in catalogue:
        for ve in fe["views"]:
            svg_id = f"svg_{fe['file_id']}_{ve['view_id']}"
            content = (OUT / ve["svg_file"]).read_text(encoding="utf-8")
            # Strip the <?xml?> prolog and inject id on the root <svg>
            content = re.sub(r"<\?xml[^>]*\?>\s*", "", content)
            content = content.replace("<svg", f'<svg id="{svg_id}"', 1)
            svg_blocks.append(f'<div class="svg-pane" data-file="{fe["file_id"]}" '
                              f'data-view="{ve["view_id"]}" '
                              f'data-svg-id="{svg_id}">{content}</div>')

    # Catalogue lookup as JS literal (avoid full JSON dep)
    js_cat_lines = ["const CATALOGUE = ["]
    for fe in catalogue:
        js_cat_lines.append(f"  {{ file_id: '{fe['file_id']}', "
                             f"file_label: '{html_escape(fe['file_label'])}',")
        js_cat_lines.append("    parts: [")
        for p in fe["parts"]:
            js_cat_lines.append(
                f"      {{ idx: {p['idx']}, label: '{p['label']}' }},")
        js_cat_lines.append("    ],")
        js_cat_lines.append("    views: [")
        for ve in fe["views"]:
            vd = ", ".join(f"{v:.3f}" for v in ve["view_dir"])
            js_cat_lines.append(
                f"      {{ view_id: '{ve['view_id']}', "
                f"label: '{html_escape(ve['view_label'])}', "
                f"view_dir: [{vd}] }},")
        js_cat_lines.append("    ] },")
    js_cat_lines.append("];")
    js_catalogue = "\n".join(js_cat_lines)

    html = HTML_TEMPLATE.format(
        svg_blocks="\n".join(svg_blocks),
        js_catalogue=js_catalogue,
    )
    out_path = OUT / "viewer.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\nwrote {out_path}  {out_path.stat().st_size/1024:.0f}KB")
    return out_path


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Accora IFU viewer  -  HLR vector</title>
<style>
  :root {{
    --accora-teal: #00836a;
    --accora-teal-pale: #cce6e0;
    --accora-lime: #b8d442;
    --bg: #f4f4f5;
    --panel: #ffffff;
    --line: #d8d8da;
    --text: #2a2a2c;
    --muted: #707074;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{ display: grid; grid-template-rows: 50px 1fr;
          font-family: Arial, sans-serif; color: var(--text);
          background: var(--bg); }}
  header {{ display: flex; align-items: center; padding: 0 16px; gap: 24px;
            background: var(--panel); border-bottom: 1px solid var(--line); }}
  header h1 {{ font-size: 16px; font-weight: bold; margin: 0; color: var(--accora-teal); }}
  header label {{ font-size: 13px; color: var(--muted); }}
  header select, header button {{ font-size: 13px; padding: 4px 8px;
                                   border: 1px solid var(--line); background: white;
                                   border-radius: 3px; cursor: pointer; }}
  header button.active {{ background: var(--accora-teal); color: white;
                          border-color: var(--accora-teal); }}
  main {{ display: grid; grid-template-columns: 240px 1fr 260px; overflow: hidden; }}
  aside.left, aside.right {{ background: var(--panel);
                              border-right: 1px solid var(--line);
                              padding: 12px; overflow-y: auto; font-size: 13px; }}
  aside.right {{ border-right: none; border-left: 1px solid var(--line); }}
  aside h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
              color: var(--muted); margin: 16px 0 8px 0; font-weight: bold; }}
  aside h2:first-child {{ margin-top: 0; }}
  .part-list {{ list-style: none; padding: 0; margin: 0; max-height: 280px;
                overflow-y: auto; border: 1px solid var(--line); border-radius: 3px; }}
  .part-list li {{ padding: 4px 8px; cursor: pointer; border-bottom: 1px solid #f0f0f0;
                   font-family: ui-monospace, Consolas, monospace; font-size: 12px; }}
  .part-list li:hover {{ background: var(--accora-teal-pale); }}
  .part-list li.highlighted {{ background: var(--accora-teal); color: white; }}
  .layer-toggle {{ display: flex; align-items: center; gap: 6px;
                   padding: 4px 0; cursor: pointer; }}
  .layer-toggle input {{ margin: 0; }}
  .swatch {{ display: inline-block; width: 16px; height: 3px;
             vertical-align: middle; background: #000; margin-right: 4px; }}
  .swatch.thin {{ height: 1px; }}
  .swatch.dashed {{ border-top: 2px dashed #808080; background: none; height: 0; }}
  .mode-pill {{ display: inline-block; padding: 3px 8px; border-radius: 12px;
                background: var(--accora-teal-pale); color: var(--accora-teal);
                font-size: 11px; font-weight: bold; margin-right: 6px; }}
  .canvas-wrap {{ position: relative; overflow: hidden; background: white; }}
  .svg-pane {{ position: absolute; inset: 0; display: none; }}
  .svg-pane.active {{ display: block; }}
  .svg-pane svg {{ width: 100%; height: 100%; cursor: grab; }}
  .svg-pane svg.panning {{ cursor: grabbing; }}
  .svg-pane svg.annotate-mode {{ cursor: crosshair; }}
  /* layer visibility classes (toggled on <svg>) */
  svg.hide-smooth_v .layer-smooth_v {{ display: none; }}
  svg.hide-sharp_v .layer-sharp_v {{ display: none; }}
  svg.hide-outline_v .layer-outline_v {{ display: none; }}
  svg.hide-hidden_sharp .layer-hidden_sharp {{ display: none; }}
  svg.hide-hidden_outline .layer-hidden_outline {{ display: none; }}
  /* part highlight */
  svg .part.highlight path {{ stroke: var(--accora-teal) !important;
                               stroke-width: 1.4 !important; }}
  svg .part.dim path {{ opacity: 0.18; }}
  /* annotations */
  .annotation-layer {{ pointer-events: all; }}
  .annotation-layer .arrow {{ stroke: var(--accora-teal); stroke-width: 0.7;
                              fill: none; }}
  .annotation-layer .arrowhead {{ fill: var(--accora-teal); stroke: none; }}
  .annotation-layer text {{ fill: var(--accora-teal); font-family: Arial;
                            font-size: 14px; font-weight: bold; }}
  .annotation-layer .anno-group {{ cursor: pointer; }}
  .annotation-layer .anno-group:hover .arrow {{ stroke-width: 1.0; }}
  footer {{ position: absolute; bottom: 8px; left: 50%;
            transform: translateX(-50%); background: rgba(255,255,255,0.95);
            padding: 4px 12px; border-radius: 12px; font-size: 11px;
            color: var(--muted); border: 1px solid var(--line); }}
  .tooltip {{ position: absolute; background: #2a2a2c; color: white;
              padding: 4px 8px; border-radius: 3px; font-size: 11px;
              pointer-events: none; opacity: 0; transition: opacity 0.1s;
              z-index: 10; }}
  .tooltip.show {{ opacity: 1; }}
</style>
</head>
<body>
<header>
  <h1>ACCORA IFU viewer</h1>
  <label>File: <select id="file-sel"></select></label>
  <label>View: <select id="view-sel"></select></label>
  <span class="mode-pill" id="mode-pill">smart</span>
  <button id="btn-smart"    class="active">smart</button>
  <button id="btn-detailed">+ smooth</button>
  <button id="btn-hidden">+ hidden</button>
  <span style="flex:1"></span>
  <button id="btn-annotate">+ callout</button>
  <button id="btn-clear">clear callouts</button>
  <button id="btn-export">export SVG</button>
</header>
<main>
  <aside class="left">
    <h2>Parts</h2>
    <p style="font-size:11px; color: var(--muted); margin: 0 0 8px 0;">
      Click a row to highlight that part. Click again to clear.</p>
    <ul class="part-list" id="part-list"></ul>
    <h2>Selection</h2>
    <div id="selection-info" style="font-size: 12px; color: var(--muted);">
      Nothing selected
    </div>
  </aside>
  <div class="canvas-wrap" id="canvas-wrap">
    {svg_blocks}
    <footer>Wheel = zoom &nbsp;&middot;&nbsp; Drag = pan &nbsp;&middot;&nbsp; Click part to highlight &nbsp;&middot;&nbsp; Callout mode: drag to place arrow</footer>
    <div class="tooltip" id="tooltip"></div>
  </div>
  <aside class="right">
    <h2>Layers</h2>
    <label class="layer-toggle"><input type="checkbox" data-layer="outline_v" checked>
      <span class="swatch" style="height:5px"></span> Silhouette (profile)</label>
    <label class="layer-toggle"><input type="checkbox" data-layer="sharp_v" checked>
      <span class="swatch" style="height:2px"></span> Sharp edges</label>
    <label class="layer-toggle"><input type="checkbox" data-layer="smooth_v">
      <span class="swatch thin"></span> Smooth (tangent) edges</label>
    <label class="layer-toggle"><input type="checkbox" data-layer="hidden_outline">
      <span class="swatch dashed"></span> Hidden silhouette</label>
    <label class="layer-toggle"><input type="checkbox" data-layer="hidden_sharp">
      <span class="swatch dashed"></span> Hidden sharp</label>

    <h2>Callouts</h2>
    <p style="font-size: 11px; color: var(--muted);">
      Click <b>+ callout</b>, then on the canvas drag from the arrow tip to the
      label position. Enter the label text when prompted.</p>
    <div id="callout-count" style="font-size: 12px; color: var(--muted);">
      0 callouts on this view
    </div>

    <h2>Pipeline</h2>
    <p style="font-size: 11px; color: var(--muted); line-height: 1.5;">
      Output is true vector SVG generated by analytical hidden-line removal
      (OCCT <code>HLRBRep</code>) per solid. Composer-equivalent pipeline:
      no rasterisation, infinite zoom, edges classified by category.
    </p>
  </aside>
</main>

<script>
{js_catalogue}

const $ = (id) => document.getElementById(id);
const canvasWrap = $('canvas-wrap');
const fileSel = $('file-sel');
const viewSel = $('view-sel');
const partList = $('part-list');
const selectionInfo = $('selection-info');
const tooltip = $('tooltip');
const calloutCount = $('callout-count');

// state per (file,view): pan/zoom/highlight/annotations
const state = {{}};

function paneKey(f, v) {{ return f + '/' + v; }}
function getState(f, v) {{
  const k = paneKey(f, v);
  if (!state[k]) state[k] = {{
    tx: 0, ty: 0, scale: 1, highlight: null, annotations: []
  }};
  return state[k];
}}

// Populate selectors
CATALOGUE.forEach(fe => {{
  const opt = document.createElement('option');
  opt.value = fe.file_id; opt.textContent = fe.file_label;
  fileSel.appendChild(opt);
}});
function refreshViews() {{
  viewSel.innerHTML = '';
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  fe.views.forEach(ve => {{
    const o = document.createElement('option');
    o.value = ve.view_id; o.textContent = ve.label;
    viewSel.appendChild(o);
  }});
}}
fileSel.addEventListener('change', () => {{
  refreshViews(); refreshPane();
}});
viewSel.addEventListener('change', refreshPane);
refreshViews();

function activePane() {{
  return document.querySelector(
    `.svg-pane[data-file="${{fileSel.value}}"][data-view="${{viewSel.value}}"]`);
}}
function activeSvg() {{ return activePane()?.querySelector('svg'); }}

function refreshPartList() {{
  partList.innerHTML = '';
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  fe.parts.forEach(p => {{
    const li = document.createElement('li');
    li.textContent = `[${{String(p.idx).padStart(3, '0')}}] ${{p.label}}`;
    li.dataset.part = p.idx;
    li.addEventListener('click', () => togglePartHighlight(p.idx));
    partList.appendChild(li);
  }});
}}

function togglePartHighlight(idx) {{
  const svg = activeSvg();
  if (!svg) return;
  const st = getState(fileSel.value, viewSel.value);
  const sel = `.part-${{String(idx).padStart(3, '0')}}`;
  if (st.highlight === idx) {{
    st.highlight = null;
    svg.querySelectorAll('.part').forEach(p => {{
      p.classList.remove('highlight'); p.classList.remove('dim');
    }});
    partList.querySelectorAll('li').forEach(li => li.classList.remove('highlighted'));
    selectionInfo.textContent = 'Nothing selected';
  }} else {{
    st.highlight = idx;
    svg.querySelectorAll('.part').forEach(p => {{
      if (p.classList.contains('part-' + String(idx).padStart(3, '0'))) {{
        p.classList.add('highlight'); p.classList.remove('dim');
      }} else {{
        p.classList.remove('highlight'); p.classList.add('dim');
      }}
    }});
    partList.querySelectorAll('li').forEach(li => {{
      li.classList.toggle('highlighted', parseInt(li.dataset.part) === idx);
    }});
    const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
    const p = fe.parts.find(x => x.idx === idx);
    selectionInfo.innerHTML = `<b>Part ${{idx}}</b><br>${{p ? p.label : ''}}`;
  }}
}}

function applyTransform(pane) {{
  const svg = pane.querySelector('svg');
  const inner = svg.querySelector(':scope > g');
  if (!inner) return;
  const st = getState(pane.dataset.file, pane.dataset.view);
  // Outer transform group: wrap if not present
  let viewG = svg.querySelector(':scope > g.view-transform');
  if (!viewG) {{
    viewG = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    viewG.setAttribute('class', 'view-transform');
    // move all existing children of svg into viewG
    while (svg.firstChild) viewG.appendChild(svg.firstChild);
    svg.appendChild(viewG);
    // annotation layer above transform group
    const al = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    al.setAttribute('class', 'annotation-layer');
    svg.appendChild(al);
  }}
  viewG.setAttribute('transform',
    `translate(${{st.tx}} ${{st.ty}}) scale(${{st.scale}})`);
}}

function attachInteractivity(pane) {{
  const svg = pane.querySelector('svg');
  if (svg.dataset.attached) return;
  svg.dataset.attached = '1';

  // make sure the transform wrapper exists
  applyTransform(pane);

  // Click on a path -> walk up to .part -> highlight
  svg.addEventListener('click', e => {{
    if (svg.classList.contains('annotate-mode')) {{
      handleAnnotateClick(e, svg, pane); return;
    }}
    let p = e.target;
    while (p && p !== svg && !p.classList?.contains('part')) p = p.parentElement;
    if (p && p.classList?.contains('part')) {{
      togglePartHighlight(parseInt(p.dataset.part));
    }}
  }});

  // Hover: show tooltip with part label
  svg.addEventListener('mousemove', e => {{
    if (svg.classList.contains('annotate-mode')) return;
    let p = e.target;
    while (p && p !== svg && !p.classList?.contains('part')) p = p.parentElement;
    if (p && p.classList?.contains('part')) {{
      tooltip.textContent = p.dataset.label || '';
      tooltip.style.left = (e.clientX + 12) + 'px';
      tooltip.style.top = (e.clientY + 12) + 'px';
      tooltip.classList.add('show');
    }} else {{
      tooltip.classList.remove('show');
    }}
  }});
  svg.addEventListener('mouseleave', () => tooltip.classList.remove('show'));

  // Pan: middle-mouse, or left when not on a part
  let panning = false, lastX = 0, lastY = 0;
  svg.addEventListener('mousedown', e => {{
    if (svg.classList.contains('annotate-mode')) {{
      annotateMouseDown(e, svg, pane); return;
    }}
    let onPart = false;
    let p = e.target;
    while (p && p !== svg) {{
      if (p.classList?.contains('part')) {{ onPart = true; break; }}
      p = p.parentElement;
    }}
    if (e.button === 1 || (e.button === 0 && (e.shiftKey || !onPart))) {{
      panning = true; lastX = e.clientX; lastY = e.clientY;
      svg.classList.add('panning');
      e.preventDefault();
    }}
  }});
  window.addEventListener('mousemove', e => {{
    if (!panning) return;
    const st = getState(pane.dataset.file, pane.dataset.view);
    st.tx += (e.clientX - lastX); st.ty += (e.clientY - lastY);
    lastX = e.clientX; lastY = e.clientY;
    applyTransform(pane);
  }});
  window.addEventListener('mouseup', () => {{
    panning = false; svg.classList.remove('panning');
  }});

  // Wheel zoom centred on cursor
  svg.addEventListener('wheel', e => {{
    e.preventDefault();
    const st = getState(pane.dataset.file, pane.dataset.view);
    const factor = Math.exp(-e.deltaY * 0.0015);
    const rect = svg.getBoundingClientRect();
    const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
    // Adjust translation so the point under the cursor stays fixed
    const newScale = Math.max(0.1, Math.min(50, st.scale * factor));
    st.tx = cx - (cx - st.tx) * (newScale / st.scale);
    st.ty = cy - (cy - st.ty) * (newScale / st.scale);
    st.scale = newScale;
    applyTransform(pane);
  }}, {{ passive: false }});
}}

function refreshPane() {{
  document.querySelectorAll('.svg-pane').forEach(p => p.classList.remove('active'));
  const pane = activePane();
  if (!pane) return;
  pane.classList.add('active');
  attachInteractivity(pane);
  refreshPartList();
  applyMode();
  // re-apply highlight if any
  const st = getState(fileSel.value, viewSel.value);
  if (st.highlight !== null && st.highlight !== undefined) {{
    const idx = st.highlight; st.highlight = null;  // toggle will set it back
    togglePartHighlight(idx);
  }}
  refreshAnnotations(pane);
  updateCalloutCount();
}}

// --- Mode + layer toggles ---
const MODE_LAYERS = {{
  smart:    {{ outline_v: 1, sharp_v: 1, smooth_v: 0, hidden_outline: 0, hidden_sharp: 0 }},
  detailed: {{ outline_v: 1, sharp_v: 1, smooth_v: 1, hidden_outline: 0, hidden_sharp: 0 }},
  hidden:   {{ outline_v: 1, sharp_v: 1, smooth_v: 0, hidden_outline: 1, hidden_sharp: 1 }},
}};
let currentMode = 'smart';
function setMode(m) {{
  currentMode = m;
  $('mode-pill').textContent = m;
  document.querySelectorAll('header button[id^="btn-"]').forEach(b => {{
    if (['btn-smart', 'btn-detailed', 'btn-hidden'].includes(b.id)) {{
      b.classList.toggle('active', b.id === 'btn-' + m);
    }}
  }});
  // Sync checkbox panel with mode
  const ms = MODE_LAYERS[m];
  document.querySelectorAll('input[data-layer]').forEach(cb => {{
    cb.checked = !!ms[cb.dataset.layer];
  }});
  applyMode();
}}
function applyMode() {{
  const svg = activeSvg();
  if (!svg) return;
  document.querySelectorAll('input[data-layer]').forEach(cb => {{
    svg.classList.toggle('hide-' + cb.dataset.layer, !cb.checked);
  }});
}}
$('btn-smart').onclick = () => setMode('smart');
$('btn-detailed').onclick = () => setMode('detailed');
$('btn-hidden').onclick = () => setMode('hidden');
document.querySelectorAll('input[data-layer]').forEach(cb => {{
  cb.addEventListener('change', applyMode);
}});

// --- Annotations ---
let annotating = false;
let annoStart = null;
let annoPreview = null;
$('btn-annotate').onclick = () => {{
  annotating = !annotating;
  $('btn-annotate').classList.toggle('active', annotating);
  document.querySelectorAll('.svg-pane svg').forEach(s => {{
    s.classList.toggle('annotate-mode', annotating);
  }});
}};
$('btn-clear').onclick = () => {{
  const st = getState(fileSel.value, viewSel.value);
  st.annotations = [];
  refreshAnnotations(activePane());
  updateCalloutCount();
}};

function svgClientToUser(svg, clientX, clientY) {{
  const pt = svg.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  const inner = svg.querySelector('g.view-transform');
  return pt.matrixTransform(inner.getScreenCTM().inverse());
}}

function annotateMouseDown(e, svg, pane) {{
  if (e.button !== 0) return;
  const p = svgClientToUser(svg, e.clientX, e.clientY);
  annoStart = {{x: p.x, y: p.y, paneFile: pane.dataset.file,
                paneView: pane.dataset.view, screenX: e.clientX,
                screenY: e.clientY}};
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
  const onMove = ev => {{
    if (!annoStart || !annoPreview) return;
    const q = svgClientToUser(svg, ev.clientX, ev.clientY);
    annoPreview.setAttribute('x2', q.x);
    annoPreview.setAttribute('y2', q.y);
  }};
  const onUp = ev => {{
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    if (!annoStart) return;
    const dx = ev.clientX - annoStart.screenX;
    const dy = ev.clientY - annoStart.screenY;
    const dragLen = Math.hypot(dx, dy);
    if (annoPreview) {{ annoPreview.remove(); annoPreview = null; }}
    if (dragLen < 5) {{ annoStart = null; return; }}  // misclick
    const q = svgClientToUser(svg, ev.clientX, ev.clientY);
    const text = prompt('Callout label:', '');
    if (text === null || text === '') {{ annoStart = null; return; }}
    const st = getState(annoStart.paneFile, annoStart.paneView);
    st.annotations.push({{
      x1: annoStart.x, y1: annoStart.y,
      x2: q.x, y2: q.y, text: text
    }});
    annoStart = null;
    refreshAnnotations(pane);
    updateCalloutCount();
  }};
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}}
function handleAnnotateClick(_e, _svg, _pane) {{ /* drag-based; click is a no-op */ }}

function refreshAnnotations(pane) {{
  if (!pane) return;
  const svg = pane.querySelector('svg');
  let layer = svg.querySelector('g.annotation-layer');
  if (!layer) return;
  layer.innerHTML = '';
  const st = getState(pane.dataset.file, pane.dataset.view);
  st.annotations.forEach((a, i) => {{
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
    const p1 = `${{a.x1}},${{a.y1}}`;
    const p2 = `${{a.x1 - ux*ah + px*aw}},${{a.y1 - uy*ah + py*aw}}`;
    const p3 = `${{a.x1 - ux*ah - px*aw}},${{a.y1 - uy*ah - py*aw}}`;
    const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    poly.setAttribute('points', `${{p1}} ${{p2}} ${{p3}}`);
    poly.setAttribute('class', 'arrowhead');
    g.appendChild(poly);
    // label
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', a.x2 + 6);
    t.setAttribute('y', a.y2);
    t.setAttribute('dominant-baseline', 'middle');
    t.textContent = a.text;
    g.appendChild(t);
    g.addEventListener('click', ev => {{
      if (annotating) return;
      ev.stopPropagation();
      if (confirm('Delete callout "' + a.text + '"?')) {{
        st.annotations.splice(i, 1);
        refreshAnnotations(pane); updateCalloutCount();
      }}
    }});
    layer.appendChild(g);
  }});
}}

function updateCalloutCount() {{
  const st = getState(fileSel.value, viewSel.value);
  calloutCount.textContent = `${{st.annotations.length}} callout` +
    (st.annotations.length === 1 ? '' : 's') + ' on this view';
}}

$('btn-export').onclick = () => {{
  const svg = activeSvg();
  if (!svg) return;
  const clone = svg.cloneNode(true);
  // strip annotate-mode + hide-* utility classes
  clone.removeAttribute('class');
  const xml = new XMLSerializer().serializeToString(clone);
  const blob = new Blob([
    '<?xml version="1.0" encoding="UTF-8"?>\n', xml
  ], {{type: 'image/svg+xml'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${{fileSel.value}}_${{viewSel.value}}_annotated.svg`;
  a.click();
}};

// init
setMode('smart');
refreshPane();
</script>
</body>
</html>
"""


def save_catalogue(catalogue):
    """Persist catalogue to disk so we can rebuild HTML without re-running HLR."""
    p = OUT / "_catalogue.json"
    p.write_text(json.dumps(catalogue, indent=2), encoding="utf-8")
    return p


def load_catalogue():
    p = OUT / "_catalogue.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import sys
    OUT.mkdir(exist_ok=True)
    if "--html-only" in sys.argv:
        cat = load_catalogue()
        if cat is None:
            print("no catalogue cached; run without --html-only first"); sys.exit(1)
        build_html(cat)
    else:
        catalogue = generate_svgs()
        save_catalogue(catalogue)
        build_html(catalogue)
