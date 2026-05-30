"""Legacy shim for the IFU viewer pipeline.

The pipeline functions (sources, mesh, GLB export, STEP / Onshape
trees, SVG bake, catalogue) live in the ``ifu`` package.  This module
keeps the legacy entry points (`build_html`, `slugify`, the SOURCES
constants, etc.) importable from the same name they always had, so
existing callers (serve.py, rebuild_html.py, the ad-hoc tests) keep
working unchanged.

The one function that still lives here is ``build_html`` -- it owns the
multi-thousand-line JS/CSS/HTML template that bundles every SVG, GLB,
and JS bit into a single ``viewer.html``.  That template will be
replaced wholesale by the React frontend in Phase 3 of PLAN.md, so
splitting it now would create churn for negative value.
"""
from __future__ import annotations
import base64
import io
import json
import os
import sys
import time
import re
from pathlib import Path
import cadquery as cq
import numpy as np
import trimesh

from t5_hlr_vector import (run_hlr_per_solid, write_svg_parts,
                            split_solids, STD_VIEWS, rotate_shape)

# Re-export everything that was moved into the ``ifu`` package so
# callers that ``from build_viewer import X`` keep working.
from ifu import (
    HERE, OUT,
    SOURCES, VIEWS, SOURCE_VIEW_SUBSET, SOURCE_SKIP_CATEGORIES,
    solid_mesh_arrays, _solid_mesh_arrays, slugify,
    export_glb_b64,
    fetch_step_tree, count_tree,
    fetch_onshape_tree,
    generate_svgs,
    save_catalogue, load_catalogue,
)
# Legacy alias for the private helper that `build_html` calls inline.
_count_tree = count_tree


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def build_html(catalogue):
    """Assemble the standalone viewer page.

    PERF: as of the May 2026 sweep we DO NOT inline baked SVGs into
    the HTML.  Each pane is emitted empty with a ``data-svg-src``
    pointing to ``/api/baked_svg/<file_id>/<view_id>``; the JS fetches
    it on first activation (refreshPane).  Three large assemblies x
    three baked views each meant the inlined HTML was ~26 MB; lazy
    loading drops it to ~500 KB.  Set IFU_INLINE_SVG=1 to re-inline
    if you need a truly offline page.
    """
    inline_svg = os.environ.get("IFU_INLINE_SVG") in ("1", "true", "yes")

    svg_blocks = []
    for fe in catalogue:
        for ve in fe["views"]:
            svg_id = f"svg_{fe['file_id']}_{ve['view_id']}"
            file_id = fe['file_id']
            view_id = ve['view_id']
            svg_src = f"/api/baked_svg/{file_id}/{view_id}"
            if inline_svg:
                content = (OUT / ve["svg_file"]).read_text(encoding="utf-8")
                # Strip the <?xml?> prolog and inject id on the root <svg>
                content = re.sub(r"<\?xml[^>]*\?>\s*", "", content)
                content = content.replace("<svg", f'<svg id="{svg_id}"', 1)
                svg_blocks.append(
                    f'<div class="svg-pane" data-file="{file_id}" '
                    f'data-view="{view_id}" '
                    f'data-svg-id="{svg_id}" '
                    f'data-svg-src="{svg_src}">{content}</div>')
            else:
                # Empty placeholder.  JS refreshPane() fetches the SVG
                # the first time the pane becomes active.
                svg_blocks.append(
                    f'<div class="svg-pane" data-file="{file_id}" '
                    f'data-view="{view_id}" '
                    f'data-svg-id="{svg_id}" '
                    f'data-svg-src="{svg_src}"></div>')

    # Catalogue: structural metadata as a JSON object (small);
    # GLB blobs and Onshape trees are heavy, so each lives in its own
    # JS table keyed by file_id to keep JSON.parse fast at load time.
    #
    # PERF: as of the May 2026 sweep we DO NOT inline GLB blobs.  Even
    # for static sources the JS pulls /api/glb/<sid> on demand (static
    # sources are in _SHAPES so the endpoint works the same way as for
    # Onshape imports).  This drops the bundled HTML from ~26 MB to
    # ~500-1500 KB and lets the browser cache GLBs per-source.  Set
    # IFU_INLINE_GLB=1 in the environment to re-inline if you need a
    # truly offline page.
    catalogue_min = []
    glbs = {}
    trees = {}
    inline_glb = os.environ.get("IFU_INLINE_GLB") in ("1", "true", "yes")
    for fe in catalogue:
        catalogue_min.append({
            "file_id": fe["file_id"],
            "file_label": fe["file_label"],
            "parts": fe["parts"],
            "views": [{"view_id": ve["view_id"], "label": ve["view_label"],
                       "view_dir": [round(v, 4) for v in ve["view_dir"]]}
                      for ve in fe["views"]],
        })
        if inline_glb and fe.get("glb_b64"):
            glbs[fe["file_id"]] = fe["glb_b64"]
        if fe.get("onshape_tree"):
            trees[fe["file_id"]] = fe["onshape_tree"]

    js_catalogue = (
        "const CATALOGUE = " + json.dumps(catalogue_min) + ";\n"
        "const GLB_B64 = " + json.dumps(glbs) + ";\n"
        "const ONSHAPE_TREES = " + json.dumps(trees) + ";"
    )

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
<script type="importmap">
{{
  "imports": {{
    "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
    "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
  }}
}}
</script>
<link rel="stylesheet" href="/static/css/viewer.css"/>
<!-- Sets window.IFU_API_BASE before the app scripts run.  Empty =
     same-origin (local / single-host).  On Vercel, point it at the
     Render API.  Loads first so API_BASE resolves correctly. -->
<script src="/static/config.js"></script>
<!-- Supabase magic-link auth gate. Loads before the app so it can mirror the
     session token into a cookie + show the login overlay. No-op when
     IFU_SUPABASE_URL is empty (local dev). -->
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
<script src="/static/js/auth.js"></script>
</head>
<body>
<header>
  <a href="#/" style="text-decoration: none; color: inherit;"
     title="Go to the project home page"><h1>ACCORA IFU viewer</h1></a>
  <label data-ed-control="file-sel">File: <select id="file-sel"></select></label>
  <label data-ed-control="view-sel">View: <select id="view-sel"></select></label>
  <span class="mode-pill" id="mode-pill" data-ed-control="mode-pill">smart</span>
  <span data-ed-control="mode-btns" style="display:contents;">
    <button id="btn-smart"    class="active">smart</button>
    <button id="btn-detailed">+ smooth</button>
    <button id="btn-hidden">+ hidden</button>
  </span>
  <span style="flex:1"></span>
  <div class="seg-ctl" role="tablist" aria-label="Layout">
    <button id="lay-2d"    class="seg-btn active" title="2D drawing only">2D</button>
    <button id="lay-split" class="seg-btn"        title="2D + 3D side-by-side">Split</button>
    <button id="lay-3d"    class="seg-btn"        title="3D explore only">3D</button>
  </div>
  <span style="flex:1"></span>
  <button id="btn-annotate">+ callout</button>
  <button id="btn-clear">clear callouts</button>
  <button id="btn-detail" data-ed-control="hi-detail"
          title="Re-render just the visible viewport at finer mesh + sample">↕ hi-detail</button>
  <button id="btn-detail-clear" data-ed-control="hi-detail"
          title="Remove the hi-detail overlay" style="display:none">clear detail</button>
  <button id="btn-export">export SVG</button>
  <button id="btn-screenshot" title="Save the current pane(s) as PNG">📸 PNG</button>
  <button id="btn-iact-track"
          title="Toggle interaction log -- records every click and selection change so you can see what the app is doing">🔎 track</button>
  <button id="btn-show-regions"
          title="Colour each part region uniquely so you can see which pixels belong to which part. Useful for debugging 'this highlighted other parts too'">🎨 regions</button>
  <button id="btn-iact-capture"
          title="Snapshot the current 2D state (live SVG + what you clicked + tracker log) to the server so it can be rendered and screenshotted for debugging">📸 capture state</button>
  <button id="btn-server-log" data-ed-control="server-log"
          title="Toggle server log overlay -- see what the backend is doing in real time">📡 log</button>
</header>
<main>
  <aside class="left">
    <section class="ed-section" data-ed-section="project">
      <h2>Project</h2>
      <p style="font-size:11px; color: var(--muted); margin: 0 0 6px 0;">
        Group figures together for one IFU / doc.  Switch project to
        filter the figures list below.</p>
      <div style="display:flex; gap:4px; margin-bottom:6px;">
        <select id="project-sel" style="flex:1; padding:4px 6px; font-size:12px;">
          <option value="">— All figures —</option>
        </select>
        <button id="btn-project-new" title="Create a new project">+</button>
        <button id="btn-project-del" title="Delete current project (keeps figures)">✕</button>
      </div>
      <div style="display:flex; gap:4px; margin-bottom:6px; font-size:11px;">
        <button id="btn-revs-refresh"
                title="Pull the latest Versions list from Onshape for the active source. Updates the 'behind by N' badge on every figure bound to that source."
                style="font-size:11px;">↻ refresh versions</button>
        <span id="revs-status" style="color:var(--muted); align-self:center;"></span>
      </div>
    </section>

    <section class="ed-section" data-ed-section="figures">
      <h2 data-ed-control="legacy-figures">Figures</h2>
      <p data-ed-control="legacy-figures"
         style="font-size:11px; color: var(--muted); margin: 0 0 6px 0;">
        A figure = camera + selection + per-part styles.</p>
      <div data-ed-control="legacy-figures"
           style="display:flex; gap:4px; margin-bottom:4px;">
        <input type="text" id="fig-name" placeholder="figure name..."
               style="flex:1; padding:4px 6px; font-size:12px;
                      border:1px solid var(--line); border-radius:3px;">
        <button id="btn-fig-save"
                title="Save the current selection / styles / camera (Ctrl+S)">save</button>
        <button id="btn-fig-save-as" style="display:none;"
                title="Create a new figure from current state">save as new...</button>
      </div>
      <div id="fig-save-status"
           style="font-size:11px; color:var(--muted); margin: 0 0 6px 0;
                  min-height: 14px; display: none;"></div>
      <ul id="figures-list" class="saved-views-list"
          data-ed-control="legacy-figures"></ul>
      <!-- Variant sidebar (project-scoped editor / subview mode).
           Replaces the project-wide figures list with a vertical
           strip of thumbnail cards, all of which are highlight
           variants of the active View.  Auto-save means switching
           cards is safe. -->
      <h2 id="variants-header"
          style="display:none; margin: 0 0 6px 0; font-size: 13px;">
        Variants</h2>
      <p id="variants-help" style="display:none;
            font-size: 11px; color: var(--c-text-muted); margin: 0 0 8px 0;">
        Different highlights of this view.  Click + for a new variant;
        edits auto-save.</p>
      <div id="variants-strip"
           style="display:none; flex-direction: column; gap: 6px;"></div>
    </section>

    <section class="ed-section" data-ed-section="saved-views">
      <h2 style="margin-top: 14px;">Saved views (legacy)</h2>
      <p style="font-size:11px; color: var(--muted); margin: 0 0 6px 0;">
        Camera angles only (no selection/style).  Pre-Phase-A.</p>
      <div style="display:flex; gap:4px; margin-bottom:6px;">
        <input type="text" id="view-name" placeholder="name..."
               style="flex:1; padding:4px 6px; font-size:12px;
                      border:1px solid var(--line); border-radius:3px;">
        <button id="btn-save-view" title="Save current camera angle">save</button>
      </div>
      <ul id="saved-views" class="saved-views-list"></ul>
    </section>

    <section class="ed-section" data-ed-section="onshape-tree">
      <h2>Onshape tree</h2>
      <input type="search" id="tree-search" placeholder="filter tree..."
             autocomplete="off" spellcheck="false">
      <p style="font-size:11px; color: var(--muted); margin: 4px 0 8px 0;"
         id="tree-status">No tree for this source.</p>
      <ul class="tree-root" id="tree-root"></ul>
    </section>

    <section class="ed-section" data-ed-section="step-order">
      <h2>Solids (STEP order)</h2>
      <p style="font-size:11px; color: var(--muted); margin: 0 0 8px 0;">
        Click a row to highlight. Click again to clear.</p>
      <ul class="part-list" id="part-list"></ul>
    </section>

    <section class="ed-section" data-ed-section="selection">
      <h2>Selection</h2>
      <div id="selection-info" style="font-size: 12px; color: var(--muted);">
        Nothing selected
      </div>
    </section>
  </aside>
  <div class="canvas-wrap" id="canvas-wrap">
    {svg_blocks}
    <footer>Wheel = zoom &nbsp;&middot;&nbsp; Drag = pan &nbsp;&middot;&nbsp; Click part to highlight &nbsp;&middot;&nbsp; Callout mode: drag to place arrow</footer>
    <div class="tooltip" id="tooltip"></div>
  </div>
  <div id="pane-splitter" role="separator" aria-orientation="vertical"
       title="Drag to resize 2D / 3D panes (double-click to reset)"></div>
  <div class="webgl-wrap" id="webgl-wrap">
    <canvas id="webgl-canvas"></canvas>
    <!-- Configuration panel: shown when the active source has Onshape
         ids and the document defines configurable parameters.  Changing
         a value re-translates the source and reloads the 3D mesh. -->
    <div id="cfg-panel"
         style="position:absolute;top:50px;right:8px;z-index:10;
                width:240px;background:rgba(255,255,255,.96);
                border:1px solid #d4d4d8;border-radius:6px;
                box-shadow:0 4px 14px rgba(0,0,0,.08);
                padding:10px;font-size:12px;display:none;">
      <div id="cfg-header"
           style="display:flex;align-items:center;justify-content:space-between;
                  font-weight:600;color:#18181b;margin-bottom:8px;">
        <span>Onshape configuration</span>
        <button id="cfg-collapse"
                title="Collapse"
                style="background:transparent;border:none;color:#71717a;
                       font-size:14px;cursor:pointer;padding:0 4px;line-height:1;">−</button>
      </div>
      <div id="cfg-body"></div>
      <div id="cfg-status"
           style="font-size:11px;color:#71717a;margin-top:8px;min-height:14px;"></div>
    </div>
    <!-- Annotation panel: exploded view + 3D arrows + line-style preset.
         Drives window.IFU_VIEWER.* (defined in viewer.module.js); the
         resulting state is sent with the next "generate 2D" so the vector
         line-art matches what's set up here. -->
    <div id="annot-panel" class="annot-panel">
      <div class="annot-hd">
        <span>Figure tools</span>
        <button id="annot-collapse" title="Collapse">−</button>
      </div>
      <div id="annot-body">
        <section class="annot-sec">
          <h3>Exploded view</h3>
          <label class="annot-row">Spread
            <input type="range" id="explode-range" min="0" max="100" value="0">
          </label>
          <div class="annot-hint">Click a part in 3D to nudge it along an axis.</div>
          <button id="explode-clear" class="annot-btn">Reset explode</button>
        </section>
        <section class="annot-sec">
          <h3>Arrows</h3>
          <div class="annot-btnrow">
            <button id="arrow-straight" class="annot-btn" title="Click a face to add a straight arrow along its axis">⟶ Straight</button>
            <button id="arrow-rotation" class="annot-btn" title="Click a face to add a rotation arrow around its axis">↻ Rotate</button>
          </div>
          <div class="annot-btnrow">
            <button id="arrow-select" class="annot-btn" title="Back to part selection">Select</button>
            <button id="arrows-clear" class="annot-btn" title="Remove all arrows">Clear arrows</button>
          </div>
          <div id="arrow-list" class="annot-list"></div>
        </section>
        <section class="annot-sec">
          <h3>Line style</h3>
          <select id="preset-sel" class="annot-sel"></select>
          <div id="preset-preview" class="preset-preview"></div>
          <a href="#/settings/styles" class="annot-link">Edit styles…</a>
        </section>
      </div>
    </div>
    <div class="three-toolbar">
      <button id="btn-generate" class="primary"
              title="Render an HLR SVG of the current camera angle and show it on the left. Requires the local server (python serve.py).">
        &#9889; generate 2D
      </button>
      <span class="tb-sep"></span>
      <span id="viewdir-readout" data-ed-control="dev-readout">view_dir = (—, —, —)</span>
      <button id="btn-lock-view" data-ed-control="dev-readout"
              title="Copy view_dir tuple to clipboard">copy view_dir</button>
      <button id="btn-reset-3d" title="Frame the model from the active 2D view direction">reset camera</button>
      <span class="tb-sep" data-ed-control="up-axis"></span>
      <label class="tb-label" data-ed-control="up-axis"
             title="Override what axis is 'up'. 3D-side preview only - paste the resulting tuple into SOURCES and rebuild to bake into 2D HLR.">Up:
        <select id="up-axis-sel">
          <option value="Z" selected>Z</option>
          <option value="Y">Y</option>
          <option value="X">X</option>
          <option value="-Z">-Z</option>
          <option value="-Y">-Y</option>
          <option value="-X">-X</option>
        </select>
      </label>
      <button id="btn-copy-orient" data-ed-control="up-axis"
              title="Copy pre_rotate tuple to clipboard">copy pre_rotate</button>
    </div>
  </div>
  <aside class="right">
    <section class="ed-section" data-ed-section="layers">
    <h2>Layers</h2>
    <label class="layer-toggle"><input type="checkbox" data-layer="outline_v" checked>
      <span class="swatch" style="height:5px"></span> Silhouette (profile)</label>
    <label class="layer-toggle"><input type="checkbox" data-layer="sharp_v" checked>
      <span class="swatch" style="height:2px"></span> Sharp edges</label>
    <label class="layer-toggle"><input type="checkbox" data-layer="smooth_v">
      <span class="swatch thin"></span> Smooth (tangent) edges</label>
    <label class="layer-toggle" data-ed-control="hidden-layers"><input type="checkbox" data-layer="hidden_outline">
      <span class="swatch dashed"></span> Hidden silhouette</label>
    <label class="layer-toggle" data-ed-control="hidden-layers"><input type="checkbox" data-layer="hidden_sharp">
      <span class="swatch dashed"></span> Hidden sharp</label>
    </section>

    <!-- Drawing weights + shading: live CSS overrides on the SVG so
         the user can dial print weights / contrast without a server
         re-render.  Each row stores its value in localStorage keyed
         by source_id so a project keeps the look it shipped with.
         Secondary controls (contrast / colour family / paper) live
         inside a <details> so the panel reads compact by default.  -->
    <section class="ed-section" data-ed-section="drawing">
    <h2>Drawing</h2>
    <label class="draw-row" title="Width of the heavy silhouette lines that trace each part's outer profile (outline_v category)">
      Silhouette
      <input type="range" id="draw-outline-w" min="0.2" max="3.0" step="0.05" value="0.7">
      <span id="draw-outline-w-val">0.70</span>mm
    </label>
    <label class="draw-row" title="Width of visible sharp edges (sharp_v category)">
      Sharp
      <input type="range" id="draw-sharp-w" min="0.05" max="1.5" step="0.05" value="0.30">
      <span id="draw-sharp-w-val">0.30</span>mm
    </label>
    <label class="draw-row" title="Width of smooth / tangent edges (smooth_v category)">
      Smooth
      <input type="range" id="draw-smooth-w" min="0.05" max="1.0" step="0.05" value="0.20">
      <span id="draw-smooth-w-val">0.20</span>mm
    </label>
    <label class="draw-row" title="Width of hidden (dashed) lines">
      Hidden
      <input type="range" id="draw-hidden-w" min="0.05" max="1.5" step="0.05" value="0.30">
      <span id="draw-hidden-w-val">0.30</span>mm
    </label>
    <hr style="border:none; border-top:1px dotted var(--c-line); margin:8px 0;"/>
    <label class="draw-row" title="Width of the bold outline drawn around highlighted parts (the persistent silhouette overlay)">
      Highlight
      <input type="range" id="draw-hl-w" min="0.5" max="15" step="0.5" value="3">
      <span id="draw-hl-w-val">3.0</span>mm
    </label>
    <details class="ed-disclosure">
      <summary>more</summary>
    <label class="draw-row" title="Overall contrast / boldness multiplier applied to all baseline weights">
      Contrast
      <input type="range" id="draw-contrast" min="0.5" max="2.5" step="0.05" value="1">
      <span id="draw-contrast-val">1.00</span>×
    </label>
    <label class="draw-row" title="Opacity of the smooth (tangent) edges -- dim or hide them without changing visibility toggles">
      Smooth α
      <input type="range" id="draw-smooth-alpha" min="0" max="1" step="0.05" value="1">
      <span id="draw-smooth-alpha-val">1.00</span>
    </label>
    <label class="draw-row" title="Line colour family">
      Lines
      <select id="draw-line-color" style="flex:1; padding:2px;">
        <option value="black">Black (print)</option>
        <option value="ink">Ink (#1a1f24)</option>
        <option value="teal">Accora teal</option>
        <option value="grey">Muted grey</option>
      </select>
    </label>
    <label class="draw-row" title="Background colour of the 2D canvas">
      Paper
      <select id="draw-paper" style="flex:1; padding:2px;">
        <option value="white">White</option>
        <option value="cream">Cream</option>
        <option value="cool">Cool grey</option>
        <option value="dark">Dark (for export preview)</option>
      </select>
    </label>
    <div style="display:flex; gap:6px; margin-top:8px;">
      <button id="btn-draw-reset" class="btn"
              title="Restore the IFU print defaults">reset weights</button>
      <button id="btn-draw-save-default" class="btn"
              title="Make these the default weights for new figures">save as default</button>
    </div>
    </details>
    </section>

    <section class="ed-section" data-ed-section="selection-styling">
    <h2>Selection styling</h2>
    <p style="font-size:11px; color: var(--muted); margin: 0 0 6px 0;"
       data-ed-control="dev-prose">
      Properties applied to the currently-highlighted parts.</p>
    <!-- Presets: the one-click-applied style row, visible in project
         mode.  Each button packages stroke + width + fill + alpha so
         every IFU figure across a project uses a small consistent
         vocabulary. -->
    <div id="preset-row" data-ed-control="presets"
         style="display:none;
                flex-wrap: wrap;
                gap: 6px;
                margin: 0 0 8px 0;"></div>
    <div id="preset-actions" data-ed-control="presets"
         style="display:none;
                gap: 6px;
                margin: 0 0 12px 0;">
      <button id="btn-preset-remove" class="btn"
              title="Clear style on the currently-selected parts">remove style</button>
      <button id="btn-preset-clear" class="btn"
              title="Clear styles on every part for this figure">clear all</button>
    </div>
    <!-- Legacy free-form controls.  Wrapped in data-ed-control="advanced-styling"
         so the whole block hides in project mode.  Kept for file://
         fallback + power users. -->
    <div id="style-panel" data-ed-control="advanced-styling" style="font-size: 12px;">
      <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
        Stroke
        <input type="color" id="sty-stroke" value="#00836a" style="width:30px;">
      </label>
      <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
        Width
        <input type="range" id="sty-width" min="0.5" max="15" step="0.5"
               value="3" style="flex:1;">
        <span id="sty-width-val">3.0</span>
      </label>
      <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
        Opacity
        <input type="range" id="sty-opacity" min="0.1" max="1" step="0.05"
               value="1" style="flex:1;">
        <span id="sty-opacity-val">1.0</span>
      </label>
      <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
        Dash
        <select id="sty-dash" style="flex:1; padding:2px;">
          <option value="">solid</option>
          <option value="3 2">dashed</option>
          <option value="1 1.5">dotted</option>
        </select>
      </label>
      <label style="display:flex; align-items:center; gap:6px; margin:8px 0 4px 0;">
        Fill
        <input type="color" id="sty-fill" value="#cce6e0" style="width:30px;">
        <label style="font-size:11px; display:flex; align-items:center; gap:3px;">
          <input type="checkbox" id="sty-fill-on" style="margin:0;"> shade
        </label>
      </label>
      <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
        Fill α
        <input type="range" id="sty-fill-opacity" min="0.05" max="1" step="0.05"
               value="0.3" style="flex:1;">
        <span id="sty-fill-opacity-val">0.30</span>
      </label>
      <label data-ed-control="group-mode"
             style="display:flex; align-items:center; gap:6px; margin:6px 0; font-size:11px;">
        <input type="checkbox" id="sty-group-mode" checked style="margin:0;">
        outline as group (combined profile for multi-select)
      </label>
      <div style="display:flex; gap:4px; margin-top:6px; flex-wrap:wrap;">
        <button id="btn-apply-style" title="Apply to highlighted parts">apply</button>
        <button id="btn-reset-style" title="Clear style for highlighted parts">reset</button>
        <button id="btn-reset-all-style" title="Clear all style overrides for this source">reset all</button>
      </div>
      <div data-ed-control="dev-buttons"
           style="display:flex; gap:4px; margin-top:8px; flex-wrap:wrap;">
        <button id="btn-expand-parent" title="Add all siblings under the same Onshape Assembly to the selection">+ Onshape group</button>
        <button id="btn-cycle-deeper" title="In 3D mode, next click at the same pixel goes one layer deeper">depth-click ↻</button>
      </div>
    </div>
    <!-- Applied list lives OUTSIDE #style-panel so it stays visible
         in project mode (where the rest of the panel is hidden). -->
    <h3 style="margin: 6px 0 4px 0; font-size: 10px; color: var(--c-text-muted);
                text-transform: uppercase; letter-spacing: 0.5px;
                font-weight: 600;">
      Applied to parts
    </h3>
    <ol id="applied-styles-list"
        style="list-style: none; padding: 0; margin: 0; font-size: 11px;
                max-height: 220px; overflow-y: auto;"></ol>
    </section>

    <section class="ed-section" data-ed-section="callouts">
    <h2>Callouts</h2>
    <p style="font-size: 11px; color: var(--muted);"
       data-ed-control="dev-prose">
      Click <b>+ callout</b>, then on the canvas drag from the arrow tip to the
      label position. Enter the label text when prompted.</p>
    <div id="callout-count" style="font-size: 12px; color: var(--muted);">
      0 callouts on this view
    </div>
    </section>

    <section class="ed-section" data-ed-section="pipeline">
    <h2>Pipeline</h2>
    <p style="font-size: 11px; color: var(--muted); line-height: 1.5;">
      Output is true vector SVG generated by analytical hidden-line removal
      (OCCT <code>HLRBRep</code>) per solid. Composer-equivalent pipeline:
      no rasterisation, infinite zoom, edges classified by category.
    </p>
    </section>
  </aside>
</main>

<!--
  F.2 mount point for the new app screens.  Empty + hidden by default
  so the legacy editor (the <header> + <main> above) stays the home
  page; the router only flips this visible when the URL has a hash
  matching one of the new routes (#/, #/project/<id>, etc.).
-->
<div id="app-root" style="display:none; padding: 24px;
                           font-family: Arial, sans-serif;"></div>

<script>
{js_catalogue}
</script>
<script src="/static/js/viewer.classic.js"></script>
<script type="module" src="/static/js/viewer.module.js"></script>
<!-- Annotation UI: runs as a module AFTER viewer.module.js so window.IFU_VIEWER
     is fully augmented with the explode/arrows/preset API before wiring. -->
<script type="module" src="/static/js/viewer.annotate.ui.js"></script>
</script>
</body>
</html>
"""



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
