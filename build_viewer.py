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

    # Catalogue: structural metadata as a JSON object (small);
    # GLB blobs and Onshape trees are heavy, so each lives in its own
    # JS table keyed by file_id to keep JSON.parse fast at load time.
    catalogue_min = []
    glbs = {}
    trees = {}
    for fe in catalogue:
        catalogue_min.append({
            "file_id": fe["file_id"],
            "file_label": fe["file_label"],
            "parts": fe["parts"],
            "views": [{"view_id": ve["view_id"], "label": ve["view_label"],
                       "view_dir": [round(v, 4) for v in ve["view_dir"]]}
                      for ve in fe["views"]],
        })
        if fe.get("glb_b64"):
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
  /* Segmented layout control: 2D | Split | 3D */
  .seg-ctl {{ display: inline-flex; border: 1px solid var(--line);
              border-radius: 4px; overflow: hidden; }}
  .seg-btn {{ font-size: 13px; padding: 4px 12px; border: none;
              background: white; cursor: pointer; color: var(--muted);
              border-right: 1px solid var(--line); font-weight: 500; }}
  .seg-btn:last-child {{ border-right: none; }}
  .seg-btn:hover {{ background: var(--accora-teal-pale); color: var(--text); }}
  .seg-btn.active {{ background: var(--accora-teal); color: white; }}
  /* 3-layout grid: areas reflow when body switches layout-* class */
  main {{ display: grid; grid-template-rows: 1fr;
           grid-template-columns: 240px 1fr 260px;
           grid-template-areas: "left center right"; overflow: hidden;
           transition: grid-template-columns 0.18s ease; }}
  aside.left  {{ grid-area: left; }}
  aside.right {{ grid-area: right; }}
  /* 2D-only (default) */
  /* Project-scoped editor: only the figure list and selection summary
     are relevant when the user navigated in from a project workspace.
     Hide the legacy / dev sections so the sidebar stops looking like
     a developer dashboard. */
  body.project-scoped-editor [data-ed-section="project"],
  body.project-scoped-editor [data-ed-section="saved-views"],
  body.project-scoped-editor [data-ed-section="onshape-tree"],
  body.project-scoped-editor [data-ed-section="step-order"],
  body.project-scoped-editor [data-ed-section="pipeline"],
  body.project-scoped-editor [data-ed-control="file-sel"],
  body.project-scoped-editor [data-ed-control="view-sel"],
  body.project-scoped-editor [data-ed-control="mode-pill"],
  body.project-scoped-editor [data-ed-control="mode-btns"],
  body.project-scoped-editor [data-ed-control="hi-detail"],
  body.project-scoped-editor [data-ed-control="dev-readout"],
  body.project-scoped-editor [data-ed-control="up-axis"],
  body.project-scoped-editor [data-ed-control="hidden-layers"],
  body.project-scoped-editor [data-ed-control="group-mode"],
  body.project-scoped-editor [data-ed-control="dev-buttons"],
  body.project-scoped-editor [data-ed-control="dev-prose"],
  body.project-scoped-editor [data-ed-control="advanced-styling"],
  body.project-scoped-editor [data-ed-control="legacy-figures"] {{
    display: none !important;
  }}
  /* Show the variant strip in project mode */
  body.project-scoped-editor #variants-header,
  body.project-scoped-editor #variants-help {{
    display: block !important;
  }}
  body.project-scoped-editor #variants-strip {{
    display: flex !important;
  }}
  /* Variant card: thumbnail + name + selection size */
  .variant-card {{
    display: flex; align-items: center; gap: 8px;
    padding: 6px 8px;
    border: 1px solid var(--c-line);
    border-radius: var(--radius-1);
    background: var(--c-surface);
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }}
  .variant-card:hover {{
    background: var(--c-accora-pale);
    border-color: var(--c-accora);
  }}
  .variant-card.is-active {{
    background: var(--c-accora-pale);
    border-color: var(--c-accora);
    box-shadow: inset 0 0 0 1px var(--c-accora);
  }}
  .variant-card .variant-thumb {{
    flex: 0 0 56px;
    width: 56px; height: 42px;
    background: var(--c-surface-1);
    border: 1px solid var(--c-line);
    border-radius: 3px;
    object-fit: contain;
  }}
  .variant-card .variant-thumb.placeholder {{
    border-style: dashed;
  }}
  .variant-card .variant-meta {{ flex: 1; min-width: 0; }}
  .variant-card .variant-name {{
    font-size: 12px;
    font-weight: 500;
    color: var(--c-text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .variant-card .variant-sub {{
    font-size: 10px; color: var(--c-text-muted);
  }}
  .variant-card.add {{
    justify-content: center;
    border-style: dashed;
    color: var(--c-text-muted);
    font-weight: 500;
  }}
  .variant-card.add:hover {{
    color: var(--c-accora-dark);
  }}
  /* Show the preset row + actions in project mode (flex), keep them
     hidden in the file:// / legacy path so power users see the old
     control set. */
  body.project-scoped-editor [data-ed-control="presets"] {{
    display: flex !important;
  }}
  /* Preset buttons themselves */
  .preset-btn {{
    flex: 1 1 calc(50% - 6px);
    min-width: 86px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    background: var(--c-surface);
    border: 1px solid var(--c-line);
    border-radius: var(--radius-1);
    font-family: var(--font-ui, Inter, sans-serif);
    font-size: 12px;
    font-weight: 500;
    color: var(--c-text);
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s, transform 0.06s;
  }}
  .preset-btn:hover {{
    background: var(--c-accora-pale);
    border-color: var(--c-accora);
  }}
  .preset-btn:active {{ transform: scale(0.98); }}
  .preset-btn.is-active {{
    background: var(--c-accora-pale);
    border-color: var(--c-accora);
    color: var(--c-accora-dark);
    box-shadow: inset 0 0 0 1px var(--c-accora);
  }}
  .preset-swatch {{
    flex: 0 0 16px;
    height: 16px;
    border-radius: 50%;
    box-shadow: 0 0 0 1px rgba(0,0,0,0.06);
  }}
  /* Slim header in project mode -- the dev row + the breadcrumb were
     stacking up to ~80px before; now ~42px total. */
  body.project-scoped-editor header {{
    padding: 2px 16px;
    gap: 12px;
    min-height: 0;
    background: var(--c-surface);
    border-bottom: 1px solid var(--c-line);
    box-shadow: none;
  }}
  body.project-scoped-editor header h1 {{
    font-size: 13px;
    font-weight: 700;
    color: var(--c-accora-dark);
    letter-spacing: 0.2px;
    line-height: 28px;
  }}
  /* Tighten the per-button vertical space */
  body.project-scoped-editor header button,
  body.project-scoped-editor header .seg-btn {{
    padding-top: 3px;
    padding-bottom: 3px;
    line-height: 18px;
  }}
  /* Breadcrumb is now redundant with the back-to-project pill; hide
     it in project mode to claw back another row of vertical space. */
  body.project-scoped-editor #editor-breadcrumb {{
    display: none;
  }}
  /* Re-skin the legacy buttons in project mode to match the G.0
     design system (Onshape-style chrome: subtle borders, teal accents
     on hover, primary-tinted for active, no garish grey). */
  body.project-scoped-editor header button,
  body.project-scoped-editor .three-toolbar button {{
    font-family: var(--font-ui, Inter, sans-serif);
    font-size: 12.5px;
    font-weight: 500;
    padding: 5px 10px;
    border: 1px solid var(--c-line);
    background: var(--c-surface);
    color: var(--c-text);
    border-radius: var(--radius-1);
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }}
  body.project-scoped-editor header button:hover,
  body.project-scoped-editor .three-toolbar button:hover {{
    background: var(--c-accora-pale);
    border-color: var(--c-accora);
    color: var(--c-accora-dark);
  }}
  body.project-scoped-editor header button.active {{
    background: var(--c-accora);
    border-color: var(--c-accora);
    color: #fff;
  }}
  body.project-scoped-editor header button.active:hover {{
    background: var(--c-accora-dark);
    border-color: var(--c-accora-dark);
  }}
  body.project-scoped-editor .three-toolbar button.primary {{
    background: var(--c-accora);
    border-color: var(--c-accora);
    color: #fff;
  }}
  body.project-scoped-editor .three-toolbar button.primary:hover {{
    background: var(--c-accora-dark);
    border-color: var(--c-accora-dark);
    color: #fff;
  }}
  body.project-scoped-editor .seg-ctl {{
    border-color: var(--c-line);
    border-radius: var(--radius-1);
    overflow: hidden;
  }}
  body.project-scoped-editor .seg-btn {{
    font-family: var(--font-ui, Inter, sans-serif);
    font-size: 12.5px;
    font-weight: 500;
    background: var(--c-surface);
    color: var(--c-text-muted);
    border-right: 1px solid var(--c-line);
  }}
  body.project-scoped-editor .seg-btn:hover {{
    background: var(--c-accora-pale);
    color: var(--c-accora-dark);
  }}
  body.project-scoped-editor .seg-btn.active {{
    background: var(--c-accora);
    color: #fff;
  }}
  /* Back-to-project pill: sits right after the logo, primary action
     to leave the editor.  Visually distinct so it's discoverable. */
  body.project-scoped-editor .back-to-project {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12.5px;
    font-weight: 500;
    padding: 5px 12px;
    border-radius: 16px;
    border: 1px solid var(--c-line);
    background: var(--c-surface);
    color: var(--c-text-muted);
    text-decoration: none;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    cursor: pointer;
  }}
  body.project-scoped-editor .back-to-project:hover {{
    background: var(--c-accora-pale);
    border-color: var(--c-accora);
    color: var(--c-accora-dark);
  }}
  /* The whole legacy crumb strip below the header looks dev-y in
     project mode; restyle to match the rest of the chrome. */
  body.project-scoped-editor #editor-breadcrumb {{
    background: var(--c-surface-1);
    border-bottom: 1px solid var(--c-line);
    color: var(--c-text-muted);
    padding: 6px 16px;
  }}
  body.project-scoped-editor #editor-breadcrumb .current {{
    color: var(--c-text);
    font-weight: 600;
  }}
  body.project-scoped-editor #editor-breadcrumb a {{
    color: var(--c-text-muted);
    text-decoration: none;
  }}
  body.project-scoped-editor #editor-breadcrumb a:hover {{
    color: var(--c-accora);
    text-decoration: underline;
  }}
  body.layout-2d .canvas-wrap {{ grid-area: center; display: block; }}
  body.layout-2d .webgl-wrap  {{ display: none; }}
  /* 3D-only */
  body.layout-3d .webgl-wrap  {{ grid-area: center; display: block; }}
  body.layout-3d .canvas-wrap {{ display: none; }}
  /* Split view: a 4-column grid with both panes visible */
  body.layout-split main {{
    grid-template-columns: 240px 1fr 1fr 260px;
    grid-template-areas: "left center2d center3d right";
  }}
  body.layout-split .canvas-wrap {{ grid-area: center2d; display: block;
                                     border-right: 1px solid var(--line); }}
  body.layout-split .webgl-wrap  {{ grid-area: center3d; display: block; }}
  /* In any layout the panes themselves are the same */
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
  .webgl-wrap  {{ position: relative; overflow: hidden;
                   /* Studio backdrop: subtle vertical gradient with a soft
                      horizon at ~62%.  Reads as "looking out across a
                      light grey floor" instead of a flat white box.  The
                      WebGL canvas is alpha:true so this shows through. */
                   background:
                     radial-gradient(ellipse 80% 30% at 50% 78%,
                                     rgba(0,0,0,0.06) 0%,
                                     rgba(0,0,0,0) 70%),
                     linear-gradient(180deg,
                                     #fbfbfc 0%,
                                     #f3f4f6 55%,
                                     #e3e6eb 85%,
                                     #d7dbe1 100%); }}
  .webgl-wrap canvas {{ width: 100%; height: 100%; display: block;
                         cursor: grab; }}
  .webgl-wrap canvas.dragging {{ cursor: grabbing; }}
  .three-toolbar {{ position: absolute; top: 10px; left: 50%;
                     transform: translateX(-50%); background: rgba(255,255,255,0.95);
                     padding: 6px 12px; border-radius: 14px;
                     border: 1px solid var(--line); font-size: 12px;
                     display: flex; gap: 8px; align-items: center;
                     font-family: ui-monospace, Consolas, monospace;
                     max-width: calc(100% - 40px); }}
  .three-toolbar .tb-sep {{ width: 1px; height: 18px; background: var(--line);
                             margin: 0 2px; }}
  .three-toolbar .tb-label {{ font-family: Arial, sans-serif; color: var(--muted);
                               font-size: 12px; }}
  .three-toolbar select {{ font-size: 12px; padding: 2px 4px;
                            border: 1px solid var(--line); border-radius: 3px;
                            background: white; cursor: pointer; }}
  .three-toolbar button {{ font-family: Arial, sans-serif; font-size: 12px;
                            padding: 3px 10px; border: 1px solid var(--line);
                            background: white; border-radius: 3px;
                            cursor: pointer; }}
  .three-toolbar button:hover {{ background: var(--accora-teal-pale); }}
  .three-toolbar button.primary {{ background: var(--accora-teal); color: white;
                                    border-color: var(--accora-teal);
                                    font-weight: 600; padding: 4px 14px; }}
  .three-toolbar button.primary:hover {{ background: #006e58; }}
  .three-toolbar button:disabled {{ opacity: 0.6; cursor: wait; }}
  .three-toolbar button.primary.unavailable {{
    background: #c0c0c4; border-color: #c0c0c4; color: #fff; cursor: help;
  }}
  /* Hide 2D-only header controls when in 3D-only layout (Split keeps them) */
  body.layout-3d #btn-smart,
  body.layout-3d #btn-detailed,
  body.layout-3d #btn-hidden,
  body.layout-3d #mode-pill,
  body.layout-3d #btn-annotate,
  body.layout-3d #btn-clear,
  body.layout-3d #btn-export {{ display: none; }}
  /* In 3D-only mode the right sidebar (layer toggles, callouts) doesn't
     apply; reclaim the space for the 3D canvas. */
  body.layout-3d aside.right {{ display: none; }}
  body.layout-3d main {{
    grid-template-columns: 240px 1fr;
    grid-template-areas: "left center";
  }}
  /* Tree search input */
  #tree-search {{ width: 100%; padding: 4px 6px; font-size: 12px;
                   border: 1px solid var(--line); border-radius: 3px;
                   font-family: ui-monospace, Consolas, monospace; }}
  #tree-search:focus {{ outline: none; border-color: var(--accora-teal); }}
  /* Hide tree nodes that don't match the current search */
  .tree-root li.filtered-out {{ display: none; }}
  /* Saved views panel */
  .saved-views-list {{ list-style: none; padding: 0; margin: 0;
                        font-size: 12px; max-height: 220px; overflow-y: auto;
                        border: 1px solid var(--line); border-radius: 3px; }}
  .saved-views-list li {{ display: flex; align-items: center; gap: 4px;
                           padding: 4px 6px; border-bottom: 1px solid #f0f0f0;
                           font-family: ui-monospace, Consolas, monospace; }}
  .saved-views-list li:hover {{ background: var(--accora-teal-pale); }}
  .saved-views-list .name {{ flex: 1; cursor: pointer; }}
  .saved-views-list button {{ font-size: 11px; padding: 2px 6px;
                               border: 1px solid var(--line); background: white;
                               border-radius: 3px; cursor: pointer; }}
  /* Selection styling panel */
  #style-panel button {{ font-size: 12px; padding: 3px 8px;
                          border: 1px solid var(--line); background: white;
                          border-radius: 3px; cursor: pointer; }}
  #style-panel button:hover {{ background: var(--accora-teal-pale); }}
  #view-name {{ font-family: ui-monospace, Consolas, monospace; }}
  #btn-save-view {{ font-size: 12px; padding: 3px 8px;
                     border: 1px solid var(--line); background: white;
                     border-radius: 3px; cursor: pointer; }}
  #btn-save-view:hover {{ background: var(--accora-teal-pale); }}
  /* Onshape tree */
  .tree-root {{ list-style: none; padding: 0; margin: 0; font-size: 12px;
                font-family: ui-monospace, Consolas, monospace; }}
  .tree-root ul {{ list-style: none; padding-left: 14px; margin: 0;
                    border-left: 1px solid #eee; }}
  .tree-row {{ display: flex; align-items: center; gap: 4px;
                padding: 2px 4px; cursor: pointer; border-radius: 2px; }}
  .tree-row:hover {{ background: var(--accora-teal-pale); }}
  .tree-row.matched {{ color: var(--accora-teal); }}
  .tree-row.highlighted {{ background: var(--accora-teal); color: white; }}
  .tree-row .twisty {{ display: inline-block; width: 10px;
                        font-family: monospace; color: var(--muted); }}
  .tree-row .icon {{ font-size: 10px; color: var(--muted); width: 10px; }}
  .tree-row.is-assembly .icon {{ color: var(--accora-teal); }}
  .svg-pane {{ position: absolute; inset: 0; display: none; }}
  .svg-pane.active {{ display: block; }}
  .svg-pane svg {{ width: 100%; height: 100%; cursor: grab; }}
  .svg-pane svg.panning {{ cursor: grabbing; }}
  .svg-pane svg.annotate-mode {{ cursor: crosshair; }}
  /* Hovering a part: switch to the pointing-hand cursor so it's obvious
     where the click-target is.  The .layer-hit pads each part out to a
     3 mm hit area, so the cursor flips well before you reach the thin
     visible stroke. */
  .svg-pane svg .part {{ cursor: pointer; }}
  .svg-pane svg.panning .part {{ cursor: grabbing; }}
  /* layer visibility classes (toggled on <svg>) */
  svg.hide-smooth_v .layer-smooth_v {{ display: none; }}
  svg.hide-sharp_v .layer-sharp_v {{ display: none; }}
  svg.hide-outline_v .layer-outline_v {{ display: none; }}
  svg.hide-hidden_sharp .layer-hidden_sharp {{ display: none; }}
  svg.hide-hidden_outline .layer-hidden_outline {{ display: none; }}
  /* part highlight: NO automatic stroke change.  The silhouette layer
     (drawn by applySilhouetteFill) is responsible for the bold outer
     edge using the user's chosen colour/width.  Without this rule,
     internal features (slits, screw cuts, mount lines) stay at their
     normal stroke -- otherwise a complex part looks like "loads of
     bits" all bolded.  We still pull non-highlighted parts back to
     0.18 opacity so the selected part stands out. */
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

const $ = (id) => document.getElementById(id);

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

// Tiny hyperscript helper: h('div.card#x', {{onClick}}, [...])
function h(spec, attrs, children) {{
  let tag = 'div', id = '', classes = [];
  if (typeof spec === 'string') {{
    const m = spec.match(/^([a-z0-9]+)?((?:[.#][a-zA-Z0-9_-]+)*)$/);
    if (m) {{
      tag = m[1] || 'div';
      (m[2] || '').split(/(?=[.#])/).forEach(part => {{
        if (part.startsWith('#')) id = part.slice(1);
        else if (part.startsWith('.')) classes.push(part.slice(1));
      }});
    }} else {{
      tag = spec;
    }}
  }}
  const el = document.createElementNS(
    tag === 'svg' || tag === 'path' || tag === 'g'
      ? 'http://www.w3.org/2000/svg'
      : 'http://www.w3.org/1999/xhtml', tag);
  if (id) el.id = id;
  if (classes.length) el.setAttribute('class', classes.join(' '));
  // Handle args overloads: h(spec, children), h(spec, attrs, children)
  if (attrs && (Array.isArray(attrs) || typeof attrs === 'string'
                 || attrs instanceof Node)) {{
    children = attrs;
    attrs = null;
  }}
  if (attrs) {{
    for (const [k, v] of Object.entries(attrs)) {{
      if (k === 'onClick' || k === 'onclick') {{
        el.addEventListener('click', v);
      }} else if (k === 'style' && typeof v === 'object') {{
        for (const [sk, sv] of Object.entries(v)) el.style[sk] = sv;
      }} else if (v === false || v == null) {{
        /* skip */
      }} else if (v === true) {{
        el.setAttribute(k, '');
      }} else {{
        el.setAttribute(k, v);
      }}
    }}
  }}
  const kids = children == null ? []
    : (Array.isArray(children) ? children : [children]);
  for (const c of kids) {{
    if (c == null || c === false) continue;
    el.appendChild(typeof c === 'string' || typeof c === 'number'
      ? document.createTextNode(String(c))
      : c);
  }}
  return el;
}}

// Single source of truth replacing today's scattered globals.  Screens
// read from it and dispatch via setRoute() / selectProject() etc. so
// future undo / persist / sync layers have a stable hook surface.
const AppState = {{
  route: '#/',
  routeParams: {{}},
  currentProjectId: null,
  currentFigureId: null,
  settings: null,        // populated lazily by Settings screen
}};

// Map of route patterns to screen modules.  Each module exports
// mount(container, params) -> teardownFn(optional).
const _routes = [];
let _currentTeardown = null;

function registerRoute(pattern, mountFn) {{
  _routes.push({{ pattern, mountFn }});
}}

function _matchRoute(hash) {{
  for (const {{ pattern, mountFn }} of _routes) {{
    const m = hash.match(pattern);
    if (m) return {{ mountFn, params: m.slice(1) }};
  }}
  return null;
}}

async function renderRoute() {{
  const hash = location.hash || '';
  AppState.route = hash;
  const appRoot = document.getElementById('app-root');
  const header = document.querySelector('header');
  const main = document.querySelector('main');

  // Empty hash falls through to the legacy editor.  The Home screen
  // is opt-in via the logo link in the legacy header (and the explicit
  // '#/' URL).  We don't redirect because too much of the e2e test
  // suite (and existing muscle memory) expects the legacy editor as
  // the default landing page.  When the editor is fully migrated into
  // the route shape (post-F.5+), we can flip this.
  if (!hash || hash === '#') {{
    if (_currentTeardown) {{
      try {{ _currentTeardown(); }} catch (_e) {{}}
      _currentTeardown = null;
    }}
    if (appRoot) appRoot.style.display = 'none';
    if (header) header.style.display = '';
    if (main) main.style.display = '';
    return;
  }}

  const matched = _matchRoute(hash);

  // Tear down the previous screen before mounting the next one.
  // mountFn can be async, so its returned value might be a Promise
  // resolving to either undefined or a teardown function.
  if (_currentTeardown) {{
    try {{
      if (typeof _currentTeardown === 'function') _currentTeardown();
    }} catch (_e) {{}}
    _currentTeardown = null;
  }}

  if (!matched) {{
    if (appRoot) {{
      appRoot.style.display = '';
      appRoot.innerHTML = '';
      appRoot.appendChild(h('div', [
        h('h1', `Unknown route: ${{hash}}`),
        h('p', [
          'Try ',
          h('a', {{ href: '#/' }}, '#/ (Home)'),
          '.',
        ]),
      ]));
    }}
    if (header) header.style.display = 'none';
    if (main) main.style.display = 'none';
    return;
  }}

  // EditorScreen wants the LEGACY editor visible; other screens want
  // the app-root visible.  Let the screen decide.  We pre-set the
  // common case (app-root visible, legacy hidden); EditorScreen
  // overrides on mount.
  if (header) header.style.display = 'none';
  if (main) main.style.display = 'none';
  if (appRoot) {{
    appRoot.style.display = '';
    appRoot.innerHTML = '';
    AppState.routeParams = matched.params;
    // mountFn may be async -- await so the teardown captured is the
    // real function, not a Promise.
    const result = matched.mountFn(appRoot, matched.params);
    _currentTeardown = (result && typeof result.then === 'function')
      ? (await result) || null
      : (result || null);
  }}
}}

window.addEventListener('hashchange', renderRoute);
window.IFU_APP = {{ AppState, h, registerRoute, renderRoute }};
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
:root {{
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
}}
.app-shell {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  color: var(--c-text);
  background: var(--c-bg);
  min-height: 100vh;
  margin: 0; padding: 0;
}}
.app-topbar {{
  background: #ffffff;
  border-bottom: 1px solid var(--c-line);
  height: 48px; padding: 0 var(--space-5);
  display: flex; align-items: center; gap: var(--space-4);
  box-shadow: var(--shadow-1);
  position: sticky; top: 0; z-index: 10;
}}
.app-topbar .logo {{
  font-weight: 600; font-size: 15px; color: var(--c-accora);
  text-decoration: none; letter-spacing: -0.01em;
  display: flex; align-items: center; gap: 8px;
}}
.app-topbar .logo::before {{
  content: ""; width: 18px; height: 18px;
  background: var(--c-accora);
  border-radius: 50%;
  /* concentric arc motif from the Accora brand */
  box-shadow:
    inset 0 0 0 2px #fff,
    inset 0 0 0 4px var(--c-accora);
}}
.app-topbar .crumbs {{
  display: flex; align-items: center; gap: var(--space-2);
  font-size: var(--t-strong); color: var(--c-text-muted);
}}
.app-topbar .crumbs a {{
  color: var(--c-text-muted); text-decoration: none;
}}
.app-topbar .crumbs a:hover {{ color: var(--c-accora); }}
.app-topbar .crumbs .sep {{ color: var(--c-line); }}
.app-topbar .crumbs .current {{ color: var(--c-text); font-weight: 500; }}
.app-topbar .spacer {{ flex: 1; }}
.app-topbar .nav-link {{
  color: var(--c-text-muted); text-decoration: none;
  font-size: var(--t-strong); padding: 4px 8px;
  border-radius: var(--radius-1);
}}
.app-topbar .nav-link:hover {{ background: var(--c-bg); color: var(--c-text); }}

.app-main {{ max-width: 1200px; margin: 0 auto;
  padding: var(--space-6) var(--space-5); }}

.section-title {{
  font-size: var(--t-section); color: var(--c-text-muted);
  text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600;
  margin: var(--space-6) 0 var(--space-3) 0;
}}
.section-title:first-child {{ margin-top: 0; }}

/* Card primitives */
.card-grid {{
  display: grid; gap: var(--space-4);
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
}}
.card {{
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--radius-2); padding: var(--space-4);
  cursor: pointer; transition: border-color 0.12s, box-shadow 0.12s, transform 0.12s;
  display: flex; flex-direction: column; gap: var(--space-2);
  min-height: 96px;
}}
.card:hover {{
  border-color: var(--c-accora);
  box-shadow: var(--shadow-2);
  transform: translateY(-1px);
}}
.card .card-title {{
  font-size: var(--t-card-title); font-weight: 600;
  color: var(--c-text); line-height: 1.25;
}}
.card .card-meta {{
  font-size: var(--t-meta); color: var(--c-text-muted);
  display: flex; gap: var(--space-2); flex-wrap: wrap;
}}
.card .badge {{
  display: inline-block; font-size: var(--t-meta);
  padding: 1px 6px; border-radius: 10px;
  background: var(--c-bg); color: var(--c-text-muted);
}}
.card .badge.ok {{ background: var(--c-accora-pale); color: var(--c-accora); }}
.card .badge.warn {{ background: #fff3e0; color: #c70; }}
.card.placeholder {{
  background: transparent; border-style: dashed;
  align-items: center; justify-content: center;
  color: var(--c-text-muted); cursor: pointer;
  font-size: var(--t-strong);
}}
.card.figure-card {{
  min-height: 200px;
}}
.card.figure-card .card-title {{
  margin-top: auto;
}}
.card.project-card {{
  min-height: 230px;
}}
.card.placeholder.project-new {{
  min-height: 230px;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 8px;
}}
.card.placeholder:hover {{
  background: var(--c-surface); border-style: solid;
  color: var(--c-accora);
}}
.card {{ position: relative; }}
.card .card-menu-btn {{
  position: absolute; top: 6px; right: 6px;
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border: none; background: transparent; cursor: pointer;
  color: var(--c-text-muted); border-radius: var(--radius-1);
  opacity: 0; transition: opacity 0.12s, background 0.12s, color 0.12s;
  font-size: 18px; line-height: 1;
}}
.card:hover .card-menu-btn,
.card .card-menu-btn.open {{ opacity: 1; }}
.card .card-menu-btn:hover {{
  background: var(--c-bg); color: var(--c-text);
}}
.card-menu {{
  position: absolute; min-width: 160px;
  background: var(--c-surface); border: 1px solid var(--c-line);
  border-radius: var(--radius-2); box-shadow: var(--shadow-2);
  padding: 4px; z-index: 100;
  font-size: var(--t-body);
}}
.card-menu .item {{
  padding: 6px 10px; border-radius: var(--radius-1);
  cursor: pointer; color: var(--c-text);
  display: flex; align-items: center; gap: var(--space-2);
}}
.card-menu .item:hover {{ background: var(--c-bg); }}
.card-menu .item.danger {{ color: var(--c-danger); }}
.card-menu .item.danger:hover {{ background: #fdf0f0; }}
.card-menu .sep {{
  height: 1px; background: var(--c-line); margin: 4px 2px;
}}

/* Buttons */
.btn {{
  display: inline-flex; align-items: center; gap: var(--space-2);
  border-radius: var(--radius-1); border: 1px solid var(--c-line);
  background: var(--c-surface); color: var(--c-text);
  font-size: var(--t-strong); font-family: inherit;
  padding: 6px 12px; cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}}
.btn:hover {{ background: var(--c-bg); border-color: var(--c-text-muted); }}
.btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.btn.primary {{
  background: var(--c-accora); color: #fff; border-color: var(--c-accora);
}}
.btn.primary:hover {{
  background: var(--c-accora-dark); border-color: var(--c-accora-dark);
}}
.btn.danger {{
  color: var(--c-danger); border-color: #e5b5b5;
}}
.btn.danger:hover {{ background: #fdf0f0; }}
.btn.ghost {{ border: none; background: transparent; }}
.btn.ghost:hover {{ background: var(--c-bg); }}

/* Inputs */
.input, .select {{
  font-family: inherit; font-size: var(--t-body);
  padding: 6px 10px; border-radius: var(--radius-1);
  border: 1px solid var(--c-line); background: #fff;
  color: var(--c-text);
}}
.input:focus, .select:focus {{
  outline: none; border-color: var(--c-accora);
  box-shadow: 0 0 0 3px var(--c-accora-pale);
}}
.field-row {{
  display: grid; grid-template-columns: 200px 1fr;
  gap: var(--space-4); align-items: center;
  margin-bottom: var(--space-3);
}}
.field-row label {{
  font-size: var(--t-strong); color: var(--c-text-muted);
}}

/* Empty state */
.empty {{
  color: var(--c-text-muted); font-style: italic;
  padding: var(--space-4); text-align: center;
}}

/* Modal */
.modal-backdrop {{
  position: fixed; inset: 0;
  background: rgba(20, 22, 28, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
  animation: fade-in 0.15s ease-out;
}}
@keyframes fade-in {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
.modal {{
  background: #fff; border-radius: var(--radius-3);
  box-shadow: 0 12px 48px rgba(0,0,0,0.25);
  width: 520px; max-width: 90vw; max-height: 86vh;
  display: flex; flex-direction: column;
  animation: pop-in 0.18s ease-out;
}}
@keyframes pop-in {{
  from {{ opacity: 0; transform: scale(0.96); }}
  to {{ opacity: 1; transform: scale(1); }}
}}
.modal-header {{
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--c-line);
  display: flex; align-items: center;
}}
.modal-header h2 {{
  font-size: 17px; margin: 0; font-weight: 600;
  color: var(--c-text); flex: 1;
}}
.modal-close {{
  border: none; background: transparent; font-size: 20px;
  color: var(--c-text-muted); cursor: pointer; padding: 0;
  width: 28px; height: 28px; border-radius: 50%;
}}
.modal-close:hover {{ background: var(--c-bg); color: var(--c-text); }}
.modal-body {{
  padding: var(--space-5);
  overflow-y: auto; flex: 1;
  font-size: var(--t-body); color: var(--c-text);
  line-height: 1.5;
}}
.modal-footer {{
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--c-line);
  display: flex; gap: var(--space-2); justify-content: flex-end;
}}

/* Toast */
.toast-host {{
  position: fixed; bottom: var(--space-5); right: var(--space-5);
  display: flex; flex-direction: column; gap: var(--space-2);
  z-index: 2000; pointer-events: none;
}}
.toast {{
  background: #232325; color: #fff; padding: 10px 14px;
  border-radius: var(--radius-2); font-size: var(--t-body);
  box-shadow: 0 6px 20px rgba(0,0,0,0.18);
  pointer-events: auto; max-width: 360px;
  animation: slide-in 0.18s ease-out;
}}
.toast.success {{ background: var(--c-accora-dark); }}
.toast.error {{ background: #b54040; }}
@keyframes slide-in {{
  from {{ opacity: 0; transform: translateY(8px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}

/* Spinner */
.spinner {{
  display: inline-block;
  width: 16px; height: 16px; vertical-align: middle;
  border: 2px solid var(--c-line); border-top-color: var(--c-accora);
  border-radius: 50%;
  animation: spin 0.9s linear infinite;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
`;

function _ensureDesignStyles() {{
  if (document.getElementById('ifu-design-tokens')) return;
  const s = document.createElement('style');
  s.id = 'ifu-design-tokens';
  s.textContent = _DESIGN_CSS;
  document.head.appendChild(s);
}}

// Top bar: logo + breadcrumb + (optional) right-side nav items.
function _topBar({{ crumbs, rightLinks }}) {{
  const bar = h('div.app-topbar');
  bar.appendChild(h('a.logo', {{ href: '#/' }}, 'Accora IFU'));
  if (crumbs && crumbs.length) {{
    const crumbBox = h('div.crumbs');
    crumbs.forEach((c, i) => {{
      if (i > 0) crumbBox.appendChild(h('span.sep', '/'));
      if (c.href) crumbBox.appendChild(h('a', {{ href: c.href }}, c.label));
      else crumbBox.appendChild(h('span.current', c.label));
    }});
    bar.appendChild(crumbBox);
  }}
  bar.appendChild(h('div.spacer'));
  for (const link of (rightLinks || [])) {{
    bar.appendChild(h('a.nav-link', {{ href: link.href }}, link.label));
  }}
  return bar;
}}

// Modal component: open(title, body, footerButtons) -> close()
// body can be a string or a DOM node.  footerButtons is an array of
// {{label, primary, danger, onClick}} -- onClick gets the close fn.
function openModal({{ title, body, footer, width }}) {{
  _ensureDesignStyles();
  const backdrop = h('div.modal-backdrop');
  const modal = h('div.modal');
  if (width) modal.style.width = (typeof width === 'number' ? width + 'px' : width);

  const closeBtn = h('button.modal-close', {{ title: 'Close' }}, '×');
  const header = h('div.modal-header', [
    h('h2', title || ''),
    closeBtn,
  ]);

  const bodyEl = h('div.modal-body');
  if (typeof body === 'string') bodyEl.appendChild(document.createTextNode(body));
  else if (body instanceof Node) bodyEl.appendChild(body);
  else if (typeof body === 'function') body(bodyEl, _close);   // builder fn

  const footerEl = h('div.modal-footer');
  if (footer && footer.length) {{
    for (const b of footer) {{
      const cls = 'btn ' + (b.primary ? 'primary' : (b.danger ? 'danger' : ''));
      const btn = h('button', {{
        class: cls.trim(),
        onClick: () => b.onClick && b.onClick(_close),
      }}, b.label);
      footerEl.appendChild(btn);
    }}
  }} else {{
    footerEl.appendChild(h('button.btn', {{ onClick: () => _close() }}, 'Close'));
  }}

  function _close() {{ backdrop.remove(); document.removeEventListener('keydown', _esc); }}
  function _esc(e) {{ if (e.key === 'Escape') _close(); }}
  closeBtn.addEventListener('click', _close);
  backdrop.addEventListener('click', (e) => {{
    if (e.target === backdrop) _close();
  }});
  document.addEventListener('keydown', _esc);

  modal.appendChild(header);
  modal.appendChild(bodyEl);
  modal.appendChild(footerEl);
  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);
  return {{ close: _close, body: bodyEl }};
}}

// Toast: short status message in the bottom-right.
function toast(message, kind /* 'success' | 'error' | undefined */) {{
  _ensureDesignStyles();
  let host = document.querySelector('.toast-host');
  if (!host) {{
    host = h('div.toast-host');
    document.body.appendChild(host);
  }}
  const cls = 'toast' + (kind ? ' ' + kind : '');
  const t = h('div', {{ class: cls }}, message);
  host.appendChild(t);
  setTimeout(() => {{
    t.style.transition = 'opacity 0.25s';
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 280);
  }}, 3500);
}}

// Attach a "..." action menu to a .card.  items is a list of:
//   {{ label, onClick: (closeMenu) => void, danger?: true, separator?: true }}
//
// The menu pops below the button, closes on outside click / Escape, and
// stops click-propagation so the menu and its actions don't trigger the
// card's own onClick (which would otherwise navigate away while you
// pick "Delete").
function _attachCardMenu(card, items) {{
  const btn = h('button.card-menu-btn',
                  {{ type: 'button', title: 'More actions',
                     'aria-label': 'More actions' }},
                  '⋯');
  card.appendChild(btn);

  let menu = null;
  function closeMenu() {{
    if (menu) {{ menu.remove(); menu = null; }}
    btn.classList.remove('open');
    document.removeEventListener('click', _docCloser, true);
    document.removeEventListener('keydown', _escCloser);
  }}
  function _docCloser(ev) {{
    if (menu && !menu.contains(ev.target) && ev.target !== btn) closeMenu();
  }}
  function _escCloser(ev) {{
    if (ev.key === 'Escape') closeMenu();
  }}
  btn.addEventListener('click', (ev) => {{
    ev.stopPropagation();
    if (menu) {{ closeMenu(); return; }}
    menu = h('div.card-menu');
    for (const it of items) {{
      if (it.separator) {{
        menu.appendChild(h('div.sep'));
        continue;
      }}
      const item = h('div', {{
        class: 'item' + (it.danger ? ' danger' : ''),
      }}, it.label);
      item.addEventListener('click', (e) => {{
        e.stopPropagation();
        closeMenu();
        try {{ it.onClick(closeMenu); }} catch (err) {{ console.error(err); }}
      }});
      menu.appendChild(item);
    }}
    // Position: anchor below the button.  Since .card has position:relative,
    // we can use right/top in the card's own coordinate space.
    menu.style.top = '32px';
    menu.style.right = '4px';
    card.appendChild(menu);
    btn.classList.add('open');
    // Defer the document listener so the click that opened the menu
    // doesn't immediately close it.
    setTimeout(() => {{
      document.addEventListener('click', _docCloser, true);
      document.addEventListener('keydown', _escCloser);
    }}, 0);
  }});
}}

// Simple confirm modal -- returns a Promise<bool>.  Cancel resolves to false.
// Clicking X or the backdrop / pressing Escape also resolves to false.
function confirmModal({{ title, body, confirmLabel, danger }}) {{
  return new Promise((resolve) => {{
    let resolved = false;
    const finish = (v) => {{ if (!resolved) {{ resolved = true; resolve(v); }} }};
    openModal({{
      title: title || 'Confirm',
      body: typeof body === 'string' ? h('div', body) : body,
      footer: [
        {{ label: 'Cancel', onClick: (close) => {{ close(); finish(false); }} }},
        {{ label: confirmLabel || 'Confirm',
           primary: !danger, danger: !!danger,
           onClick: (close) => {{ close(); finish(true); }} }},
      ],
    }});
    // Backstop: if the modal is closed any other way, the modal-backdrop
    // is removed from the DOM.  MutationObserver detects that and treats
    // it as a cancel.
    const obs = new MutationObserver(() => {{
      if (!document.querySelector('.modal-backdrop')) {{
        obs.disconnect();
        finish(false);
      }}
    }});
    obs.observe(document.body, {{ childList: true }});
  }});
}}

// Expose so screens can use them
window.IFU_UI = {{ openModal, toast, topBar: _topBar,
                    attachCardMenu: _attachCardMenu,
                    confirmModal }};
// ===== end G.0 design system =====


// =====================================================================
// F.3 -- Home screen
// =====================================================================
//
// Lists every project as a card grid; recent figures strip below.
// "Open editor" link still works for the legacy single-source flow.

const _HOME_CSS = `
.home-screen {{
  max-width: 1100px; margin: 0 auto; padding: 32px 24px;
  font-family: Arial, sans-serif; color: #18181b;
}}
.home-screen .topbar {{ display: flex; align-items: baseline;
                         margin-bottom: 24px; gap: 16px; }}
.home-screen .topbar h1 {{ font-size: 24px; margin: 0;
                            color: #00836a; flex: 1; }}
.home-screen .topbar button, .home-screen .topbar a {{
  font-size: 13px; padding: 6px 12px; border-radius: 4px;
  border: 1px solid #d4d4d8; background: #fff; cursor: pointer;
  text-decoration: none; color: inherit;
}}
.home-screen .topbar button:hover, .home-screen .topbar a:hover {{
  background: #cce6e0;
}}
.home-screen h2 {{ font-size: 15px; margin: 24px 0 8px; color: #71717a;
                    text-transform: uppercase; letter-spacing: 0.04em; }}
.home-screen .grid {{ display: grid;
                       grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
                       gap: 16px; }}
.home-screen .card {{
  border: 1px solid #d4d4d8; border-radius: 6px; padding: 16px;
  background: #fff; cursor: pointer; transition: border-color 0.1s;
}}
.home-screen .card:hover {{ border-color: #00836a; }}
.home-screen .card .name {{ font-size: 15px; font-weight: 600;
                              margin-bottom: 4px; }}
.home-screen .card .meta {{ font-size: 12px; color: #71717a; }}
.home-screen .card.placeholder {{
  display: flex; align-items: center; justify-content: center;
  color: #71717a; border-style: dashed; min-height: 80px;
}}
.home-screen .empty {{ color: #71717a; font-style: italic;
                        padding: 16px 0; }}
.home-screen .recents {{ list-style: none; padding: 0; margin: 0; }}
.home-screen .recents li {{ display: flex; gap: 8px; padding: 6px 0;
                              border-bottom: 1px solid #f4f4f5;
                              cursor: pointer; }}
.home-screen .recents li:hover {{ background: #f4f4f5; }}
.home-screen .recents .figname {{ flex: 1; }}
.home-screen .recents .figmeta {{ color: #71717a; font-size: 12px; }}
`;

function _ensureHomeStyles() {{
  if (document.getElementById('home-screen-styles')) return;
  const s = document.createElement('style');
  s.id = 'home-screen-styles';
  s.textContent = _HOME_CSS;
  document.head.appendChild(s);
}}

async function HomeScreen(container) {{
  _ensureDesignStyles();
  container.className = 'app-shell';

  // Top bar
  container.appendChild(_topBar({{
    crumbs: [{{ label: 'Home' }}],
    rightLinks: [
      {{ label: 'Legacy editor', href: '' }},
      {{ label: 'Settings', href: '#/settings' }},
    ],
  }}));

  const main = h('div.app-main');
  container.appendChild(main);

  let projects = [], figures = [];
  try {{
    const [pr, fr] = await Promise.all([
      fetch(API_BASE + '/api/projects'),
      fetch(API_BASE + '/api/figures'),
    ]);
    if (pr.ok) projects = (await pr.json()).projects || [];
    if (fr.ok) figures = (await fr.json()).figures || [];
  }} catch (_e) {{}}

  // Per-project view counts + first-view thumbnail.  Fetched in
  // parallel so the home page doesn't pay an N-projects-deep wait.
  const viewsByProject = {{}};
  await Promise.all(projects.map(async (p) => {{
    try {{
      const r = await fetch(API_BASE + '/api/projects/'
                              + encodeURIComponent(p.id) + '/views');
      if (r.ok) viewsByProject[p.id] = (await r.json()).views || [];
    }} catch (_e) {{ viewsByProject[p.id] = []; }}
  }}));

  // Projects -- big tiles with preview thumbnail (from the project's
  // most-recently-updated view) at the top.
  main.appendChild(h('div.section-title', `Projects (${{projects.length}})`));
  const grid = h('div.card-grid');
  const newCard = h('div.card.placeholder.project-new',
    [h('div', {{ style: {{ fontSize: '32px', color: 'var(--c-accora)' }} }}, '+'),
     h('div', 'New project')]);
  newCard.addEventListener('click', () => _openNewProjectModal());
  grid.appendChild(newCard);

  for (const p of projects) {{
    const card = h('div.card.project-card');
    const projViews = viewsByProject[p.id] || [];
    const figcount = projViews.reduce(
      (acc, v) => acc + (v.figure_count || 0), 0);
    // Preview: latest view's thumbnail.  If no view, fall back to a
    // generated "monogram" tile from the project name initials.
    if (projViews.length) {{
      const v = projViews[0];   // newest-first from /api/projects/.../views
      const thumb = h('img', {{
        src: API_BASE + '/api/views/' + encodeURIComponent(v.id)
             + '/thumbnail?v=' + encodeURIComponent(v.updated_at || ''),
        alt: '',
        style: {{ width: '100%', height: '140px',
                    objectFit: 'contain',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    marginBottom: '8px',
                    border: '1px solid var(--c-line)' }},
      }});
      thumb.onerror = () => {{
        thumb.replaceWith(_monogramTile(p.name));
      }};
      card.appendChild(thumb);
    }} else {{
      card.appendChild(_monogramTile(p.name));
    }}
    card.appendChild(h('div.card-title', p.name));
    if (p.description) {{
      card.appendChild(h('div', {{ style: {{ fontSize: '11px',
                                                color: 'var(--c-text-muted)',
                                                margin: '2px 0 4px 0',
                                                whiteSpace: 'nowrap',
                                                overflow: 'hidden',
                                                textOverflow: 'ellipsis' }} }},
                          p.description));
    }}
    card.appendChild(h('div.card-meta', [
      h('span', `${{projViews.length}} view${{projViews.length === 1 ? '' : 's'}}`),
      h('span', '·'),
      h('span', `${{figcount}} figure${{figcount === 1 ? '' : 's'}}`),
      h('span', '·'),
      h('span', (p.updated_at || '').slice(0, 10)),
    ]));
    card.addEventListener('click', () => {{
      location.hash = '#/project/' + encodeURIComponent(p.id);
    }});
    _attachCardMenu(card, [
      {{ label: 'Rename...', onClick: () => _renameProject(p) }},
      {{ label: 'Edit description...',
         onClick: () => _editProjectDescription(p) }},
      {{ separator: true }},
      {{ label: 'Delete project...', danger: true,
         onClick: () => _deleteProject(p) }},
    ]);
    grid.appendChild(card);
  }}
  main.appendChild(grid);

  // Recents
  if (figures.length) {{
    main.appendChild(h('div.section-title', 'Recent figures'));
    const recentGrid = h('div.card-grid');
    for (const f of figures.slice(0, 6)) {{
      const card = h('div.card', {{ style: {{ minHeight: '72px' }} }});
      card.appendChild(h('div.card-title', f.name || '(untitled)'));
      card.appendChild(h('div.card-meta', [
        h('span', f.source_id || '?'),
        h('span', '·'),
        h('span', (f.updated_at || '').slice(0, 10)),
      ]));
      card.addEventListener('click', () => {{
        if (f.project_id) {{
          location.hash = '#/project/' + encodeURIComponent(f.project_id)
                        + '/figure/' + encodeURIComponent(f.id);
        }} else {{
          location.hash = '';
        }}
      }});
      recentGrid.appendChild(card);
    }}
    main.appendChild(recentGrid);
  }}
}}

// "Monogram" placeholder for projects that have no view (and thus no
// real thumbnail).  Uses the project name's initials, two-tone teal
// background.  Better than a generic "no preview" box for visual
// rhythm on the home page.
function _monogramTile(name) {{
  const initials = (name || '?').split(/\s+/).slice(0, 2)
                                  .map(w => w[0] || '')
                                  .join('').toUpperCase() || '?';
  const tile = h('div', {{
    style: {{ width: '100%', height: '140px',
                background: 'linear-gradient(135deg, var(--c-accora) 0%, '
                            + 'var(--c-accora-dark) 100%)',
                color: '#fff',
                borderRadius: 'var(--radius-1)',
                marginBottom: '8px',
                display: 'flex', alignItems: 'center',
                justifyContent: 'center',
                fontSize: '42px', fontWeight: 700,
                letterSpacing: '2px',
                fontFamily: 'var(--font-ui, Inter, sans-serif)' }} }},
    initials);
  return tile;
}}

registerRoute(/^#\/$/, HomeScreen);

// New-project wizard.  A project is a CAD model + the figures
// authored against it -- the model has to be chosen BEFORE any
// figures exist, so the modal won't create the project until the
// user has either imported an Onshape document OR picked one of
// the existing sources.
function _openNewProjectModal() {{
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
      (urlInput = h('input.input', {{
        placeholder: 'https://cad.onshape.com/documents/...',
        style: {{ width: '100%', fontFamily: 'var(--font-mono)',
                    fontSize: '12px' }},
        autocomplete: 'off',
        spellcheck: false,
      }})),
      (probeHint = h('div', {{ style: {{ fontSize: '11px',
                                              color: 'var(--c-text-muted)',
                                              marginTop: '4px',
                                              minHeight: '14px' }} }}, '')),
    ]),
  ]);

  // ---- Existing pane --------------------------------------------------
  existingPane = h('div', {{ style: {{ display: 'none' }} }}, [
    h('div.field-row', [
      h('label', 'Model'),
      (sourceSelect = h('select.select', {{
        style: {{ width: '100%' }},
      }})),
      h('div', {{ style: {{ fontSize: '11px',
                              color: 'var(--c-text-muted)',
                              marginTop: '4px' }} }},
        'Demo assemblies and previously imported Onshape documents.'),
    ]),
  ]);

  // ---- Modal body -----------------------------------------------------
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Project name'),
      (nameInput = h('input.input', {{
        placeholder: 'e.g. Presto IFU R03',
        style: {{ width: '100%' }},
      }})),
    ]),
    h('div.field-row', [
      h('label', 'Description (optional)'),
      (descInput = h('input.input', {{
        placeholder: 'short description shown on the home card',
        style: {{ width: '100%' }},
      }})),
    ]),
    h('div', {{ style: {{ marginTop: '8px',
                            fontSize: 'var(--t-meta)',
                            fontWeight: 600,
                            color: 'var(--c-text)' }} }},
      'Model'),
    h('div', {{ style: {{ display: 'flex',
                            gap: '4px',
                            borderBottom: '1px solid var(--c-line)',
                            marginBottom: '12px' }} }},
        [modeTabImport, modeTabExisting]),
    importPane,
    existingPane,
    (errorBox = h('div', {{ style: {{ display: 'none',
                                          padding: '8px 12px',
                                          marginTop: '4px',
                                          background: '#fef2f2',
                                          border: '1px solid #fecaca',
                                          color: '#991b1b',
                                          borderRadius: 'var(--radius-1)',
                                          fontSize: '12px' }} }})),

    // Progress block, shown only during import
    (progressWrap = h('div', {{ style: {{ display: 'none',
                                              marginTop: '16px',
                                              padding: '16px',
                                              background: 'var(--c-accora-pale)',
                                              borderRadius: 'var(--radius-1)' }} }}, [
      (progressLabel = h('div', {{ style: {{ fontWeight: 600,
                                                  marginBottom: '6px',
                                                  color: 'var(--c-accora-dark)' }} }},
                          'Connecting to Onshape...')),
      (progressDetail = h('div', {{ style: {{ fontSize: '12px',
                                                   color: 'var(--c-text-muted)',
                                                   marginBottom: '10px' }} }}, '')),
      h('div', {{ style: {{ height: '6px',
                              background: 'var(--c-surface-1)',
                              borderRadius: '3px',
                              overflow: 'hidden' }} }}, [
        (progressBar = h('div', {{ style: {{ height: '100%',
                                                  width: '0%',
                                                  background: 'var(--c-accora)',
                                                  transition: 'width 0.3s ease' }} }})),
      ]),
    ])),
  ]);

  // ---- Tab styling + switching ----------------------------------------
  function _styleTab(btn, active) {{
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
  }}
  function _setMode(next) {{
    mode = next;
    _styleTab(modeTabImport,   mode === 'import');
    _styleTab(modeTabExisting, mode === 'existing');
    importPane.style.display   = mode === 'import'   ? 'block' : 'none';
    existingPane.style.display = mode === 'existing' ? 'block' : 'none';
    // Update auto-name from the freshly active pane
    if (!nameTouched) {{
      if (mode === 'existing' && sourceSelect.value) {{
        const src = availableSources.find(s => s.id === sourceSelect.value);
        if (src) nameInput.value = src.label || src.id;
      }} else if (mode === 'import' && lastProbedUrl) {{
        // Leave whatever the probe set
      }}
    }}
  }}
  modeTabImport.addEventListener('click', (e) => {{
    e.preventDefault(); _setMode('import');
  }});
  modeTabExisting.addEventListener('click', (e) => {{
    e.preventDefault(); _setMode('existing');
  }});
  _setMode('import');

  // ---- Load existing-source list (async) ------------------------------
  fetch(API_BASE + '/api/sources').then(r => r.json()).then(data => {{
    availableSources = data.sources || [];
    sourceSelect.innerHTML = '';
    if (!availableSources.length) {{
      const opt = document.createElement('option');
      opt.disabled = true; opt.textContent = '(no models available)';
      sourceSelect.appendChild(opt);
      return;
    }}
    for (const s of availableSources) {{
      const opt = document.createElement('option');
      opt.value = s.id;
      const tag = s.origin === 'dynamic' ? ' (Onshape)' : ' (demo)';
      opt.textContent = s.label + tag;
      sourceSelect.appendChild(opt);
    }}
    // If user is on the Existing tab and hasn't typed a name yet,
    // seed it from the default-selected source.
    if (mode === 'existing' && !nameTouched && availableSources[0]) {{
      nameInput.value = availableSources[0].label || availableSources[0].id;
    }}
  }}).catch(() => {{}});

  sourceSelect.addEventListener('change', () => {{
    if (mode === 'existing' && !nameTouched) {{
      const src = availableSources.find(s => s.id === sourceSelect.value);
      if (src) nameInput.value = src.label || src.id;
    }}
  }});

  function showError(msg) {{
    errorBox.textContent = msg;
    errorBox.style.display = 'block';
  }}
  function hideError() {{
    errorBox.style.display = 'none';
  }}
  function setProgress(pct, label, detail) {{
    progressWrap.style.display = 'block';
    progressBar.style.width = Math.max(0, Math.min(100, pct)) + '%';
    if (label != null) progressLabel.textContent = label;
    if (detail != null) progressDetail.textContent = detail;
  }}

  async function pollImport(jobId) {{
    while (true) {{
      await new Promise(res => setTimeout(res, 1500));
      const r = await fetch(API_BASE + '/api/onshape/import/'
                              + encodeURIComponent(jobId));
      if (!r.ok) throw new Error('poll failed: HTTP ' + r.status);
      const job = await r.json();
      setProgress(job.progress || 0,
                    _labelForImportStatus(job),
                    job.message || '');
      if (job.status === 'ready') return job;
      if (job.status === 'error') {{
        throw new Error(job.error || job.message || 'import failed');
      }}
    }}
  }}

  // Mark the name as user-touched once they type anything.  Stops the
  // debounced probe from clobbering their text.
  nameInput.addEventListener('input', () => {{
    if (nameInput.value.trim()) nameTouched = true;
  }});

  async function probeUrl(url) {{
    if (url === lastProbedUrl) return;
    lastProbedUrl = url;
    probeHint.textContent = 'checking Onshape...';
    probeHint.style.color = 'var(--c-text-muted)';
    try {{
      const r = await fetch(API_BASE + '/api/onshape/probe', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ url }}),
      }});
      const data = await r.json();
      if (!r.ok) {{
        probeHint.textContent = data.error || 'could not read URL';
        probeHint.style.color = 'var(--c-danger)';
        return;
      }}
      const docName = data.document_name || '';
      const elName = data.element_name || '';
      probeHint.textContent = `${{docName}} · ${{elName}}`;
      probeHint.style.color = 'var(--c-accora)';
      if (!nameTouched && docName) {{
        nameInput.value = docName;
      }}
    }} catch (e) {{
      probeHint.textContent = 'probe failed: ' + (e.message || e);
      probeHint.style.color = 'var(--c-danger)';
    }}
  }}

  urlInput.addEventListener('input', () => {{
    const url = (urlInput.value || '').trim();
    if (probeTimer) clearTimeout(probeTimer);
    if (!url) {{
      probeHint.textContent = '';
      lastProbedUrl = null;
      return;
    }}
    // Don't fire the probe until the URL looks structurally valid --
    // saves a request per keystroke while typing.
    if (!/\/documents\/[0-9a-f]{{16,}}\/[wvm]\//i.test(url)) {{
      probeHint.textContent = '';
      lastProbedUrl = null;
      return;
    }}
    probeTimer = setTimeout(() => probeUrl(url), 600);
  }});

  const modal = openModal({{
    title: 'New project',
    body,
    footer: [
      (cancelBtn = {{ label: 'Cancel', onClick: (close) => close() }}),
      (createBtn = {{ label: 'Create', primary: true,
                       onClick: async (close) => {{
        const name = (nameInput.value || '').trim();
        if (!name) {{ nameInput.focus(); return; }}
        const description = (descInput.value || '').trim();
        hideError();

        // Enforce: the user must have chosen a model
        let primary_source_id = null;
        let onshape_ids = null;
        let importedJob = null;

        if (mode === 'import') {{
          const url = (urlInput.value || '').trim();
          if (!url) {{
            showError('Paste an Onshape document URL, or switch to '
                        + '"Use an existing model".');
            urlInput.focus();
            return;
          }}
          // Disable inputs once we start
          nameInput.disabled = true;
          descInput.disabled = true;
          urlInput.disabled = true;
          modeTabImport.disabled = true;
          modeTabExisting.disabled = true;
          try {{
            setProgress(2, 'Starting import...', url);
            const r0 = await fetch(API_BASE + '/api/onshape/import', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ url }}),
            }});
            if (!r0.ok) {{
              const j = await r0.json().catch(() => ({{}}));
              throw new Error(j.error || ('HTTP ' + r0.status));
            }}
            const job0 = await r0.json();
            importedJob = await pollImport(job0.id);
            primary_source_id = importedJob.source_id || null;
            onshape_ids = importedJob.onshape_ids || null;
            setProgress(100, 'Import complete', primary_source_id);
          }} catch (e) {{
            showError(e.message || String(e));
            nameInput.disabled = false;
            descInput.disabled = false;
            urlInput.disabled = false;
            modeTabImport.disabled = false;
            modeTabExisting.disabled = false;
            return;
          }}
        }} else {{
          // mode === 'existing'
          primary_source_id = sourceSelect.value || null;
          if (!primary_source_id) {{
            showError('Pick a model from the list.');
            return;
          }}
          const src = availableSources.find(s => s.id === primary_source_id);
          if (src && src.onshape_ids) onshape_ids = src.onshape_ids;
        }}

        // Create the project record (model is now committed)
        try {{
          const pr = await fetch(API_BASE + '/api/projects', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ name, description,
                                      primary_source_id, onshape_ids }}),
          }});
          if (!pr.ok) throw new Error('project create failed: HTTP ' + pr.status);
          const p = await pr.json();
          close();
          toast(importedJob
                  ? 'Project created from Onshape document'
                  : 'Project created', 'success');
          location.hash = '#/project/' + encodeURIComponent(p.id);
        }} catch (e) {{
          showError(e.message || String(e));
          nameInput.disabled = false;
          descInput.disabled = false;
          if (urlInput) urlInput.disabled = false;
          modeTabImport.disabled = false;
          modeTabExisting.disabled = false;
        }}
      }} }}),
    ],
  }});
  setTimeout(() => nameInput.focus(), 50);
  return modal;
}}

function _labelForImportStatus(job) {{
  switch (job.status) {{
    case 'queued':      return 'Queued';
    case 'resolving':   return 'Reading document metadata...';
    case 'translating': return 'Onshape is converting your assembly to STEP...';
    case 'downloading': return 'Downloading STEP geometry...';
    case 'ready':       return 'Done';
    case 'error':       return 'Import failed';
    default:            return job.status || '...';
  }}
}}
// ===== end F.3 Home screen =====


// =====================================================================
// F.4 -- Project workspace screen
// =====================================================================
//
// One project at a time.  Breadcrumb back to home.  Figure grid +
// "new figure" card.  Source binding bar (shows the source + revision
// status; refresh button hits the existing /api/sources/.../refresh
// endpoint).

async function ProjectScreen(container, params) {{
  _ensureDesignStyles();
  container.className = 'app-shell';
  const projId = params[0];

  let proj = null, figs = [], sources = [];
  try {{
    const [pr, fr, sr] = await Promise.all([
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId)),
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId) + '/figures'),
      fetch(API_BASE + '/api/sources'),
    ]);
    if (pr.ok) proj = await pr.json();
    if (fr.ok) figs = (await fr.json()).figures || [];
    if (sr.ok) sources = (await sr.json()).sources || [];
  }} catch (_e) {{}}

  if (!proj) {{
    container.appendChild(_topBar({{
      crumbs: [{{ label: 'Home', href: '#/' }}, {{ label: 'Project not found' }}],
    }}));
    const main = h('div.app-main');
    main.appendChild(h('p', {{ style: {{ color: 'var(--c-text-muted)' }} }},
                        'This project could not be loaded.'));
    main.appendChild(h('a.btn', {{ href: '#/' }}, '← Back to home'));
    container.appendChild(main);
    return;
  }}

  AppState.currentProjectId = projId;

  container.appendChild(_topBar({{
    crumbs: [
      {{ label: 'Home', href: '#/' }},
      {{ label: proj.name }},
    ],
    rightLinks: [
      {{ label: 'Settings', href: '#/settings' }},
    ],
  }}));
  const main = h('div.app-main');
  container.appendChild(main);

  if (proj.description) {{
    main.appendChild(h('p', {{ style: {{ color: 'var(--c-text-muted)',
                                            margin: '0 0 24px 0' }} }},
                        proj.description));
  }}

  // Source status bar.  Prefer the project's primary source -- that
  // is the model the project IS.  Fall back to the union of figure
  // sources for legacy projects that don't have a primary set.
  let usedSourceIds;
  if (proj.primary_source_id) {{
    usedSourceIds = [proj.primary_source_id];
    // Include any orphan sources used by older figures so the user
    // still sees them in the bar (rare).
    for (const f of figs) {{
      if (f.source_id && f.source_id !== proj.primary_source_id
          && !usedSourceIds.includes(f.source_id)) {{
        usedSourceIds.push(f.source_id);
      }}
    }}
  }} else {{
    usedSourceIds = [...new Set(figs.map(f => f.source_id).filter(Boolean))];
  }}
  if (usedSourceIds.length) {{
    const bar = h('div', {{ style: {{
        background: 'var(--c-surface)', border: '1px solid var(--c-line)',
        borderRadius: 'var(--radius-2)', padding: '12px 16px',
        marginBottom: '24px',
        display: 'flex', alignItems: 'center', gap: '12px',
        fontSize: 'var(--t-body)',
      }} }});
    bar.appendChild(h('span', {{ style: {{ color: 'var(--c-text-muted)' }} }}, 'Sources:'));
    for (const sid of usedSourceIds) {{
      const src = sources.find(s => s.id === sid);
      bar.appendChild(h('span', {{ style: {{ fontWeight: '500' }} }},
                        src?.label || sid));
      bar.appendChild(h('span.badge.ok', sid));
    }}
    bar.appendChild(h('div', {{ style: {{ flex: '1' }} }}));
    const refreshBtn = h('button.btn', '↻ Refresh Onshape Versions');
    refreshBtn.addEventListener('click', async () => {{
      refreshBtn.disabled = true;
      refreshBtn.textContent = 'Refreshing...';
      let ok = 0, fail = 0;
      for (const sid of usedSourceIds) {{
        try {{
          const r = await fetch(API_BASE + '/api/sources/'
                                   + encodeURIComponent(sid)
                                   + '/versions/refresh', {{ method: 'POST' }});
          if (r.ok) ok++; else fail++;
        }} catch (_e) {{ fail++; }}
      }}
      toast(`Refreshed ${{ok}} source(s)` + (fail ? `, ${{fail}} failed` : ''),
            fail ? 'error' : 'success');
      refreshBtn.disabled = false;
      refreshBtn.textContent = '↻ Refresh Onshape Versions';
    }});
    bar.appendChild(refreshBtn);
    main.appendChild(bar);
  }}

  // Views grid: each view = camera angle, owns 1..N figures (highlight
  // variants).  "New view" sends the user into the editor in a special
  // "create-view" mode that captures whatever camera angle they choose.
  let views = [];
  try {{
    const vr = await fetch(API_BASE + '/api/projects/'
                            + encodeURIComponent(projId) + '/views');
    if (vr.ok) views = (await vr.json()).views || [];
  }} catch (_e) {{}}

  main.appendChild(h('div.section-title', `Views (${{views.length}})`));
  const grid = h('div.card-grid');
  const newCard = h('div.card.placeholder',
    [h('div', {{ style: {{ fontSize: '24px' }} }}, '+'),
     h('div', 'New view')]);
  newCard.addEventListener('click', () => _openNewViewModal(projId, proj));
  grid.appendChild(newCard);

  for (const view of views) {{
    const card = h('div.card.figure-card');
    const thumb = h('img', {{
      src: API_BASE + '/api/views/' + encodeURIComponent(view.id)
           + '/thumbnail?v=' + encodeURIComponent(view.updated_at || ''),
      alt: '',
      style: {{ width: '100%', height: '120px',
                  objectFit: 'contain',
                  background: 'var(--c-surface-1)',
                  borderRadius: 'var(--radius-1)',
                  marginBottom: '6px',
                  border: '1px solid var(--c-line)' }},
    }});
    thumb.onerror = () => {{
      thumb.replaceWith(h('div', {{
        style: {{ width: '100%', height: '120px',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    border: '1px dashed var(--c-line)',
                    marginBottom: '6px',
                    display: 'flex', alignItems: 'center',
                    justifyContent: 'center',
                    color: 'var(--c-text-muted)',
                    fontSize: '11px', fontStyle: 'italic' }} }},
        'no preview yet'));
    }};
    card.appendChild(thumb);
    card.appendChild(h('div.card-title', view.name || '(untitled view)'));
    const n = view.figure_count || (view.figure_ids || []).length;
    card.appendChild(h('div.card-meta', [
      h('span', n + (n === 1 ? ' figure' : ' figures')),
      h('span', '·'),
      h('span', (view.updated_at || '').slice(0, 10)),
    ]));
    card.addEventListener('click', () => {{
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(view.id);
    }});
    _attachCardMenu(card, [
      {{ label: 'Rename...', onClick: () => _renameView(view, projId) }},
      {{ separator: true }},
      {{ label: 'Delete view...', danger: true,
         onClick: () => _deleteView(view, projId) }},
    ]);
    grid.appendChild(card);
  }}
  main.appendChild(grid);

  // Legacy figures section: figures that aren't attached to any view
  // (= pre-Phase-3 data the migration couldn't link, OR figures whose
  // view got deleted).  Surfaced so the user can recover them.
  const orphanFigs = figs.filter(f => !f.view_id
                                       || !views.some(v => v.id === f.view_id));
  if (orphanFigs.length) {{
    main.appendChild(h('div.section-title',
      {{ style: {{ marginTop: '32px', color: 'var(--c-text-muted)' }} }},
      `Unfiled figures (${{orphanFigs.length}})`));
    main.appendChild(h('p', {{ style: {{ fontSize: '12px',
                                              color: 'var(--c-text-muted)',
                                              margin: '0 0 12px 0' }} }},
      'These figures predate the View layer.  Open them to assign a View, '
      + 'or delete them.'));
    const orphanGrid = h('div.card-grid');
    for (const fig of orphanFigs) {{
      const card = h('div.card.figure-card');
      const thumb = h('img', {{
        src: API_BASE + '/api/figures/' + encodeURIComponent(fig.id)
             + '/thumbnail?v=' + encodeURIComponent(fig.updated_at || ''),
        alt: '',
        style: {{ width: '100%', height: '120px',
                    objectFit: 'contain',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    marginBottom: '6px',
                    border: '1px solid var(--c-line)' }},
      }});
      thumb.onerror = () => {{
        thumb.replaceWith(h('div', {{
          style: {{ width: '100%', height: '120px',
                      background: 'var(--c-surface-1)',
                      borderRadius: 'var(--radius-1)',
                      border: '1px dashed var(--c-line)',
                      marginBottom: '6px',
                      display: 'flex', alignItems: 'center',
                      justifyContent: 'center',
                      color: 'var(--c-text-muted)',
                      fontSize: '11px', fontStyle: 'italic' }} }},
          'no preview yet'));
      }};
      card.appendChild(thumb);
      card.appendChild(h('div.card-title', fig.name || '(untitled)'));
      card.appendChild(h('div.card-meta', [
        h('span', fig.source_id || '?'),
        h('span', '·'),
        h('span', (fig.updated_at || '').slice(0, 10)),
      ]));
      card.addEventListener('click', () => {{
        location.hash = '#/project/' + encodeURIComponent(projId)
                      + '/figure/' + encodeURIComponent(fig.id);
      }});
      _attachCardMenu(card, [
        {{ label: 'Rename...', onClick: () => _renameFigure(fig, projId) }},
        {{ separator: true }},
        {{ label: 'Delete figure...', danger: true,
           onClick: () => _deleteFigure(fig, projId) }},
      ]);
      orphanGrid.appendChild(card);
    }}
    main.appendChild(orphanGrid);
  }}
}}

// Open the editor with the project's primary source so the user can
// pose the camera and "Save view".  No view created up-front -- we
// stamp the View on first save so deleted-without-save doesn't leave
// empty Views littering the project.
function _openNewViewModal(projId, proj) {{
  // Simplest first cut: go straight into the editor with the project
  // pre-bound but no figure loaded.  Save flow there creates the View.
  location.hash = '#/project/' + encodeURIComponent(projId)
                + '/view/__new__';
}}

async function _renameView(view, projId) {{
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'View name'),
      (nameInput = h('input.input', {{ value: view.name || '',
                                          style: {{ width: '100%' }} }})),
    ]),
  ]);
  openModal({{
    title: 'Rename view',
    body,
    footer: [
      {{ label: 'Cancel', onClick: (close) => close() }},
      {{ label: 'Save', primary: true, onClick: async (close) => {{
        const name = (nameInput.value || '').trim();
        if (!name) {{ nameInput.focus(); return; }}
        try {{
          const r = await fetch(API_BASE + '/api/views/'
                                  + encodeURIComponent(view.id),
                                  {{ method: 'PUT',
                                     headers: {{ 'Content-Type': 'application/json' }},
                                     body: JSON.stringify({{ ...view, name }}) }});
          if (!r.ok) throw new Error('HTTP ' + r.status);
          close();
          toast('View renamed', 'success');
          if (window.IFU_APP?.renderRoute) window.IFU_APP.renderRoute();
        }} catch (e) {{
          toast('Rename failed: ' + (e.message || e), 'error');
        }}
      }} }},
    ],
  }});
  setTimeout(() => {{ nameInput.focus(); nameInput.select(); }}, 50);
}}

async function _deleteView(view, projId) {{
  const n = view.figure_count || (view.figure_ids || []).length;
  const body = h('div', [
    h('p', {{ style: {{ marginTop: 0 }} }},
      'Delete the view ', h('strong', view.name || '(untitled)'),
      n ? ` and its ${{n}} figure${{n === 1 ? '' : 's'}}?` : '?'),
    h('p', {{ style: {{ color: 'var(--c-text-muted)',
                          fontSize: '12px', marginBottom: 0 }} }},
      'This action cannot be undone.'),
  ]);
  const ok = await new Promise((resolve) => {{
    openModal({{
      title: 'Delete view?',
      body,
      footer: [
        {{ label: 'Cancel', onClick: (close) => {{ close(); resolve(false); }} }},
        {{ label: 'Delete', danger: true, onClick: (close) => {{
          close(); resolve(true);
        }} }},
      ],
    }});
  }});
  if (!ok) return;
  try {{
    const r = await fetch(API_BASE + '/api/views/'
                            + encodeURIComponent(view.id) + '?cascade=1',
                            {{ method: 'DELETE' }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('View deleted', 'success');
    if (window.IFU_APP?.renderRoute) window.IFU_APP.renderRoute();
  }} catch (e) {{
    toast('Delete failed: ' + (e.message || e), 'error');
  }}
}}

function _openNewFigureModal(projId, sources, proj) {{
  let nameInput, sourceSelect, viewSelect, captureCurrent;
  let configWrap, configStatus;
  // configInputs keys off parameter id; values are <select> or <input>
  const configInputs = {{}};

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
  const currentCam = (() => {{
    try {{
      const c = window.IFU_VIEWER && window.IFU_VIEWER.getCameraEyeTarget?.();
      return c || null;
    }} catch (_e) {{ return null; }}
  }})();

  // Source-area renders differently depending on whether the project
  // already has a model bound.
  const sourceArea = legacyMode
    ? h('div.field-row', [
        h('label', 'Source'),
        (sourceSelect = h('select.select', {{ style: {{ width: '100%' }} }})),
      ])
    : h('div', {{ style: {{ padding: '10px 12px',
                              background: 'var(--c-surface-1)',
                              borderRadius: 'var(--radius-1)',
                              fontSize: 'var(--t-body)',
                              display: 'flex', alignItems: 'center',
                              gap: '8px' }} }}, [
        h('span', {{ style: {{ color: 'var(--c-text-muted)' }} }}, 'Model:'),
        h('strong', projSource ? projSource.label : projSourceId),
        h('span.badge.ok',
          (projSource && projSource.origin === 'dynamic')
            ? 'Onshape' : 'demo'),
      ]);

  const body = h('div', [
    h('div.field-row', [
      h('label', 'Figure name'),
      (nameInput = h('input.input', {{ placeholder: 'e.g. Side rail close-up',
                                         style: {{ width: '100%' }} }})),
    ]),
    sourceArea,
    h('div.field-row', [
      h('label', 'Starting view'),
      (viewSelect = h('select.select', {{ style: {{ width: '100%' }} }})),
    ]),
    currentCam
      ? h('div', {{ style: {{ marginTop: '8px', padding: '8px 12px',
                                background: 'var(--c-accora-pale)',
                                borderRadius: 'var(--radius-1)',
                                fontSize: 'var(--t-meta)',
                                color: 'var(--c-accora-dark)' }} }}, [
          (captureCurrent = h('input', {{ type: 'checkbox', checked: true,
                                            style: {{ marginRight: '6px' }} }})),
          h('span', "Use my current 3D camera angle as the figure's view"),
        ])
      : null,
    // Onshape configuration block (populated when source has onshape_ids)
    (configWrap = h('div', {{ style: {{ marginTop: '12px', display: 'none' }} }}, [
      h('div', {{ style: {{ fontSize: 'var(--t-meta)', fontWeight: 600,
                              color: 'var(--c-text)', marginBottom: '6px' }} }},
        'Onshape configuration'),
      (configStatus = h('div', {{ style: {{ fontSize: '12px',
                                                color: 'var(--c-text-muted)',
                                                marginBottom: '8px' }} }},
                          'loading...')),
    ])),
    h('div', {{ style: {{ marginTop: '12px', fontSize: '12px',
                            color: 'var(--c-text-muted)' }} }},
      'You can re-pose the camera in the editor at any time.'),
  ]);
  // Populate source dropdown only in legacy mode
  if (legacyMode && sourceSelect) {{
    for (const s of (sources || [])) {{
      const opt = document.createElement('option');
      opt.value = s.id;
      let suffix = '';
      if (s.origin === 'dynamic') suffix = '  (Onshape import)';
      else if (!s.onshape_ids) suffix = '  (local)';
      opt.textContent = `${{s.label}}${{suffix}}`;
      sourceSelect.appendChild(opt);
    }}
  }}
  // Starting-view presets -- this is just a default if no camera capture
  ['iso', 'front', 'side'].forEach(vid => {{
    const opt = document.createElement('option');
    opt.value = vid; opt.textContent = vid;
    viewSelect.appendChild(opt);
  }});

  // Resolve the active source id at any moment -- driven by the
  // dropdown in legacy mode, or by the project binding otherwise.
  function _activeSourceId() {{
    if (legacyMode && sourceSelect) return sourceSelect.value;
    return projSourceId;
  }}

  // Fetch configuration parameters for the active source.  Only
  // sources with onshape_ids will return any -- everything else gets
  // ``has_config: false`` and we hide the block.
  async function refreshConfigForSource() {{
    // Clear existing inputs
    Object.keys(configInputs).forEach(k => delete configInputs[k]);
    while (configWrap.children.length > 2) {{
      configWrap.removeChild(configWrap.lastChild);
    }}
    const sid = _activeSourceId();
    const src = (sources || []).find(s => s.id === sid);
    if (!src || !src.onshape_ids) {{
      configWrap.style.display = 'none';
      return;
    }}
    configWrap.style.display = 'block';
    configStatus.textContent = 'loading parameters...';
    try {{
      const r = await fetch(API_BASE + '/api/sources/'
                              + encodeURIComponent(sid) + '/configuration');
      if (!r.ok) {{
        configStatus.textContent = 'parameters unavailable';
        return;
      }}
      const cfg = await r.json();
      if (!cfg.has_config || !cfg.parameters?.length) {{
        configStatus.textContent =
          'this assembly has no configurable parameters';
        return;
      }}
      configStatus.textContent =
        cfg.parameters.length + ' parameter'
        + (cfg.parameters.length === 1 ? '' : 's')
        + ' available -- pick variant to render';
      for (const p of cfg.parameters) {{
        const labelEl = h('label', {{
          style: {{ marginBottom: 0,
                      fontSize: 'var(--t-body)',
                      color: 'var(--c-text)',
                      fontWeight: 500 }} }},
          p.name || p.id || '(unnamed parameter)');
        const row = h('div', {{
          style: {{ display: 'grid',
                      gridTemplateColumns: '160px 1fr',
                      gap: '12px',
                      alignItems: 'center',
                      marginTop: '6px' }} }}, [labelEl]);

        if (p.type === 'enum' && p.options?.length) {{
          const sel = h('select.select', {{ style: {{ width: '100%' }} }});
          for (const o of p.options) {{
            const opt = document.createElement('option');
            opt.value = o.value;
            opt.textContent = o.label;
            if (o.value === p.default) opt.selected = true;
            sel.appendChild(opt);
          }}
          row.appendChild(sel);
          configInputs[p.id] = sel;
        }} else if (p.type === 'boolean') {{
          const wrap = h('label', {{
            style: {{ display: 'flex', alignItems: 'center',
                        gap: '8px', cursor: 'pointer',
                        fontSize: 'var(--t-meta)',
                        color: 'var(--c-text-muted)' }} }});
          const cb = h('input', {{ type: 'checkbox' }});
          if (p.default === true || p.default === 'true') cb.checked = true;
          wrap.appendChild(cb);
          wrap.appendChild(h('span', cb.checked ? 'enabled' : 'disabled'));
          cb.addEventListener('change', () => {{
            wrap.lastChild.textContent = cb.checked ? 'enabled' : 'disabled';
          }});
          // Read .value as a string so the create-handler can write it
          // to the configuration map uniformly.
          Object.defineProperty(cb, 'value', {{
            get() {{ return cb.checked ? 'true' : 'false'; }},
          }});
          row.appendChild(wrap);
          configInputs[p.id] = cb;
        }} else if (p.type === 'quantity') {{
          const inner = h('div', {{
            style: {{ display: 'flex', alignItems: 'center',
                        gap: '6px' }} }});
          const inp = h('input.input', {{
            type: 'text',
            placeholder: p.default != null
              ? `default: ${{p.default}}` : '',
            style: {{ flex: '1', minWidth: 0 }},
          }});
          if (p.default != null) inp.value = String(p.default);
          inner.appendChild(inp);
          if (p.unit) {{
            inner.appendChild(h('span', {{
              style: {{ color: 'var(--c-text-muted)',
                          fontSize: 'var(--t-meta)' }} }},
              p.unit));
          }}
          row.appendChild(inner);
          configInputs[p.id] = inp;
        }} else {{
          // string / unknown: plain text input
          const inp = h('input.input', {{
            type: 'text',
            placeholder: p.default != null
              ? `default: ${{p.default}}` : '',
            style: {{ width: '100%' }},
          }});
          if (p.default != null) inp.value = String(p.default);
          row.appendChild(inp);
          configInputs[p.id] = inp;
        }}
        configWrap.appendChild(row);
      }}
    }} catch (e) {{
      configStatus.textContent = 'error: ' + (e.message || e);
    }}
  }}
  if (legacyMode && sourceSelect) {{
    sourceSelect.addEventListener('change', refreshConfigForSource);
  }}
  // Kick off for whichever source is active by default
  setTimeout(refreshConfigForSource, 0);

  openModal({{
    title: 'New figure',
    body,
    footer: [
      {{ label: 'Cancel', onClick: (close) => close() }},
      {{ label: 'Create + open editor', primary: true, onClick: async (close) => {{
        const name = (nameInput.value || '').trim();
        if (!name) {{ nameInput.focus(); return; }}
        const sourceId = _activeSourceId();
        if (!sourceId) return;
        const useCurrent = currentCam && captureCurrent && captureCurrent.checked;
        // Pull configuration values into the figure payload
        const configValues = {{}};
        let configCount = 0;
        for (const [pid, el] of Object.entries(configInputs)) {{
          const v = el.value;
          if (v !== undefined && v !== null && v !== '') {{
            configValues[pid] = v;
            configCount++;
          }}
        }}
        const payload = {{
          name, source_id: sourceId, project_id: projId,
          view_id: useCurrent ? 'custom' : (viewSelect.value || 'iso'),
        }};
        if (useCurrent) {{
          payload.camera = {{
            eye: currentCam.eye, target: currentCam.target,
            up_axis: 'z',
          }};
        }}
        if (configCount > 0) {{
          payload.configuration = configValues;
        }}
        try {{
          const r = await fetch(API_BASE + '/api/figures', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload),
          }});
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const f = await r.json();
          close();
          toast('Figure created' + (useCurrent ? ' with current 3D pose' : ''),
                'success');
          location.hash = '#/project/' + encodeURIComponent(projId)
                        + '/figure/' + encodeURIComponent(f.id);
        }} catch (e) {{
          toast('Create failed: ' + (e.message || 'unknown'), 'error');
        }}
      }} }},
    ],
  }});
  setTimeout(() => nameInput.focus(), 50);
}}

registerRoute(/^#\/project\/([^/]+)$/, ProjectScreen);


// =====================================================================
// Phase 3 -- View workspace (figures within a view)
// =====================================================================

async function ViewScreen(container, params) {{
  _ensureDesignStyles();
  container.className = 'app-shell';
  const projId = params[0];
  const viewId = params[1];

  // Special "new view" route: redirect to the editor with the project's
  // primary source so the user can pose the camera and Save view.
  if (viewId === '__new__') {{
    location.hash = '#/project/' + encodeURIComponent(projId)
                  + '/figure/__new_view__';
    return;
  }}

  let proj = null, view = null, figs = [];
  try {{
    const [pr, vr, fr] = await Promise.all([
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId)),
      fetch(API_BASE + '/api/views/' + encodeURIComponent(viewId)),
      fetch(API_BASE + '/api/views/' + encodeURIComponent(viewId) + '/figures'),
    ]);
    if (pr.ok) proj = await pr.json();
    if (vr.ok) view = await vr.json();
    if (fr.ok) figs = (await fr.json()).figures || [];
  }} catch (_e) {{}}

  // PIVOT: skip this intermediate workspace and drop straight into the
  // editor for the view's first figure -- the variant strip in the
  // editor sidebar already shows all the highlight variants.  If the
  // view has no figures yet, create a "Default" one so the editor has
  // something to load.  Auto-save means switching variants is safe.
  if (proj && view) {{
    let target = figs[0];
    if (!target) {{
      try {{
        const r = await fetch(API_BASE + '/api/figures', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            name: 'Default variant',
            source_id: view.source_id,
            project_id: projId,
            view_id: viewId,
            camera: view.camera,
            configuration: view.configuration,
          }}),
        }});
        if (r.ok) {{
          target = await r.json();
          await fetch(API_BASE + '/api/views/'
                        + encodeURIComponent(viewId)
                        + '/figures/' + encodeURIComponent(target.id),
                        {{ method: 'POST' }});
        }}
      }} catch (_e) {{}}
    }}
    if (target) {{
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(target.id);
      return;
    }}
  }}

  if (!proj || !view) {{
    container.appendChild(_topBar({{
      crumbs: [{{ label: 'Home', href: '#/' }},
                {{ label: 'Not found' }}],
    }}));
    container.appendChild(h('div.app-main',
      h('p', 'Project or view not found.')));
    return;
  }}

  AppState.currentProjectId = projId;
  container.appendChild(_topBar({{
    crumbs: [
      {{ label: 'Home', href: '#/' }},
      {{ label: proj.name, href: '#/project/' + encodeURIComponent(projId) }},
      {{ label: view.name || 'View' }},
    ],
    rightLinks: [
      {{ label: 'Settings', href: '#/settings' }},
    ],
  }}));
  const main = h('div.app-main');
  container.appendChild(main);

  // Big view preview at the top so the user sees the camera angle
  // they're working under.
  main.appendChild(h('div', {{ style: {{ marginBottom: '24px' }} }}, [
    h('img', {{
      src: API_BASE + '/api/views/' + encodeURIComponent(viewId)
           + '/thumbnail?v=' + encodeURIComponent(view.updated_at || ''),
      style: {{ maxWidth: '480px', maxHeight: '280px',
                  objectFit: 'contain',
                  background: 'var(--c-surface-1)',
                  border: '1px solid var(--c-line)',
                  borderRadius: 'var(--radius-2)',
                  padding: '12px' }},
      onerror: 'this.style.display=\"none\"'
    }}),
  ]));

  main.appendChild(h('div.section-title', `Figures in this view (${{figs.length}})`));
  const grid = h('div.card-grid');

  const newCard = h('div.card.placeholder',
    [h('div', {{ style: {{ fontSize: '24px' }} }}, '+'),
     h('div', 'New figure')]);
  newCard.addEventListener('click', () =>
    _createFigureInView(projId, viewId, view));
  grid.appendChild(newCard);

  for (const fig of figs) {{
    const card = h('div.card.figure-card');
    const thumb = h('img', {{
      src: API_BASE + '/api/figures/' + encodeURIComponent(fig.id)
           + '/thumbnail?v=' + encodeURIComponent(fig.updated_at || ''),
      style: {{ width: '100%', height: '120px',
                  objectFit: 'contain',
                  background: 'var(--c-surface-1)',
                  borderRadius: 'var(--radius-1)',
                  marginBottom: '6px',
                  border: '1px solid var(--c-line)' }},
    }});
    thumb.onerror = () => {{
      thumb.replaceWith(h('div', {{
        style: {{ width: '100%', height: '120px',
                    background: 'var(--c-surface-1)',
                    borderRadius: 'var(--radius-1)',
                    border: '1px dashed var(--c-line)',
                    marginBottom: '6px',
                    display: 'flex', alignItems: 'center',
                    justifyContent: 'center',
                    color: 'var(--c-text-muted)',
                    fontSize: '11px', fontStyle: 'italic' }} }},
        'no preview yet'));
    }};
    card.appendChild(thumb);
    card.appendChild(h('div.card-title', fig.name || '(untitled)'));
    const n = (fig.selection || []).length;
    card.appendChild(h('div.card-meta', [
      h('span', n + (n === 1 ? ' part' : ' parts') + ' highlighted'),
    ]));
    card.addEventListener('click', () => {{
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(fig.id);
    }});
    _attachCardMenu(card, [
      {{ label: 'Rename...', onClick: () => _renameFigure(fig, projId) }},
      {{ separator: true }},
      {{ label: 'Delete figure...', danger: true,
         onClick: () => _deleteFigure(fig, projId) }},
    ]);
    grid.appendChild(card);
  }}
  main.appendChild(grid);
}}

async function _createFigureInView(projId, viewId, view) {{
  // Inherit camera + source from the view; user names the highlight
  // variant and lands in the editor immediately.
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Figure name'),
      (nameInput = h('input.input', {{
        placeholder: 'e.g. "Step 1 — locate caster"',
        style: {{ width: '100%' }},
      }})),
    ]),
    h('p', {{ style: {{ fontSize: '12px', color: 'var(--c-text-muted)',
                          margin: '4px 0 0 0' }} }},
      "The figure inherits the view's camera.  Highlight parts and "
      + "pick a style in the editor."),
  ]);
  openModal({{
    title: 'New figure',
    body,
    footer: [
      {{ label: 'Cancel', onClick: (close) => close() }},
      {{ label: 'Create + open editor', primary: true,
         onClick: async (close) => {{
        const name = (nameInput.value || '').trim();
        if (!name) {{ nameInput.focus(); return; }}
        try {{
          const r = await fetch(API_BASE + '/api/figures', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
              name,
              source_id: view.source_id,
              project_id: projId,
              view_id: viewId,
              camera: view.camera,
              configuration: view.configuration,
            }}),
          }});
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const f = await r.json();
          // Attach to view
          await fetch(API_BASE + '/api/views/'
                        + encodeURIComponent(viewId)
                        + '/figures/' + encodeURIComponent(f.id),
                        {{ method: 'POST' }});
          close();
          toast('Figure created', 'success');
          location.hash = '#/project/' + encodeURIComponent(projId)
                        + '/view/' + encodeURIComponent(viewId)
                        + '/figure/' + encodeURIComponent(f.id);
        }} catch (e) {{
          toast('Create failed: ' + (e.message || e), 'error');
        }}
      }} }},
    ],
  }});
  setTimeout(() => nameInput.focus(), 50);
}}

registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)$/, ViewScreen);


// --- Card actions: rename / delete projects + figures ---------------

function _renameProject(p) {{
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Project name'),
      (nameInput = h('input.input', {{ value: p.name || '',
                                          style: {{ width: '100%' }} }})),
    ]),
  ]);
  openModal({{
    title: 'Rename project',
    body,
    footer: [
      {{ label: 'Cancel', onClick: (close) => close() }},
      {{ label: 'Save', primary: true, onClick: async (close) => {{
        const name = (nameInput.value || '').trim();
        if (!name) {{ nameInput.focus(); return; }}
        try {{
          await _saveProjectPatch(p.id, {{ ...p, name }});
          close();
          toast('Project renamed', 'success');
          if (typeof window.IFU_APP?.renderRoute === 'function') {{
            window.IFU_APP.renderRoute();
          }}
        }} catch (e) {{
          toast('Rename failed: ' + (e.message || 'unknown'), 'error');
        }}
      }} }},
    ],
  }});
  setTimeout(() => {{ nameInput.focus(); nameInput.select(); }}, 50);
}}

function _editProjectDescription(p) {{
  let descInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Description'),
      (descInput = h('textarea.input', {{
        rows: 4, style: {{ width: '100%', resize: 'vertical' }},
      }}, p.description || '')),
    ]),
  ]);
  openModal({{
    title: 'Edit description',
    body,
    footer: [
      {{ label: 'Cancel', onClick: (close) => close() }},
      {{ label: 'Save', primary: true, onClick: async (close) => {{
        const description = (descInput.value || '').trim();
        try {{
          await _saveProjectPatch(p.id, {{ ...p, description }});
          close();
          toast('Description updated', 'success');
          if (typeof window.IFU_APP?.renderRoute === 'function') {{
            window.IFU_APP.renderRoute();
          }}
        }} catch (e) {{
          toast('Update failed: ' + (e.message || 'unknown'), 'error');
        }}
      }} }},
    ],
  }});
  setTimeout(() => descInput.focus(), 50);
}}

async function _saveProjectPatch(projId, patch) {{
  const r = await fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId),
                          {{ method: 'PUT',
                             headers: {{ 'Content-Type': 'application/json' }},
                             body: JSON.stringify(patch) }});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}}

async function _deleteProject(p) {{
  const figcount = (p.figure_ids || []).length;
  const body = h('div', [
    h('p', {{ style: {{ marginTop: 0 }} }},
      'This will delete the project ',
      h('strong', p.name || '(untitled)'),
      figcount
        ? `, which contains ${{figcount}} figure${{figcount === 1 ? '' : 's'}}.`
        : '.'),
    figcount
      ? h('label', {{ style: {{ display: 'flex',
                                    alignItems: 'center',
                                    gap: '8px',
                                    marginTop: '12px' }} }}, [
          h('input', {{ type: 'checkbox', id: '_del_cascade',
                          checked: false }}),
          h('span', `Also delete the ${{figcount}} figure${{figcount === 1 ? '' : 's'}}`),
        ])
      : null,
    h('p', {{ style: {{ color: 'var(--c-text-muted)',
                          fontSize: '12px', marginBottom: 0 }} }},
      'This action cannot be undone.'),
  ]);
  const ok = await new Promise((resolve) => {{
    openModal({{
      title: 'Delete project?',
      body,
      footer: [
        {{ label: 'Cancel', onClick: (close) => {{ close(); resolve(null); }} }},
        {{ label: 'Delete', danger: true, onClick: (close) => {{
          const cb = document.getElementById('_del_cascade');
          close();
          resolve({{ cascade: cb ? cb.checked : false }});
        }} }},
      ],
    }});
  }});
  if (!ok) return;
  try {{
    const q = ok.cascade ? '?cascade=1' : '';
    const r = await fetch(API_BASE + '/api/projects/' + encodeURIComponent(p.id) + q,
                            {{ method: 'DELETE' }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('Project deleted', 'success');
    if (typeof window.IFU_APP?.renderRoute === 'function') {{
      window.IFU_APP.renderRoute();
    }}
  }} catch (e) {{
    toast('Delete failed: ' + (e.message || 'unknown'), 'error');
  }}
}}

function _renameFigure(fig, projId) {{
  let nameInput;
  const body = h('div', [
    h('div.field-row', [
      h('label', 'Figure name'),
      (nameInput = h('input.input', {{ value: fig.name || '',
                                          style: {{ width: '100%' }} }})),
    ]),
  ]);
  openModal({{
    title: 'Rename figure',
    body,
    footer: [
      {{ label: 'Cancel', onClick: (close) => close() }},
      {{ label: 'Save', primary: true, onClick: async (close) => {{
        const name = (nameInput.value || '').trim();
        if (!name) {{ nameInput.focus(); return; }}
        try {{
          const r = await fetch(API_BASE + '/api/figures/'
                                  + encodeURIComponent(fig.id),
                                  {{ method: 'PUT',
                                     headers: {{ 'Content-Type': 'application/json' }},
                                     body: JSON.stringify({{ ...fig, name }}) }});
          if (!r.ok) throw new Error('HTTP ' + r.status);
          close();
          toast('Figure renamed', 'success');
          if (typeof window.IFU_APP?.renderRoute === 'function') {{
            window.IFU_APP.renderRoute();
          }}
        }} catch (e) {{
          toast('Rename failed: ' + (e.message || 'unknown'), 'error');
        }}
      }} }},
    ],
  }});
  setTimeout(() => {{ nameInput.focus(); nameInput.select(); }}, 50);
}}

async function _deleteFigure(fig, projId) {{
  const ok = await confirmModal({{
    title: 'Delete figure?',
    body: h('div', [
      h('p', {{ style: {{ marginTop: 0 }} }},
        'Delete ', h('strong', fig.name || '(untitled)'), '?'),
      h('p', {{ style: {{ color: 'var(--c-text-muted)',
                            fontSize: '12px', marginBottom: 0 }} }},
        'This action cannot be undone.'),
    ]),
    confirmLabel: 'Delete',
    danger: true,
  }});
  if (!ok) return;
  try {{
    const r = await fetch(API_BASE + '/api/figures/'
                            + encodeURIComponent(fig.id),
                            {{ method: 'DELETE' }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('Figure deleted', 'success');
    if (typeof window.IFU_APP?.renderRoute === 'function') {{
      window.IFU_APP.renderRoute();
    }}
  }} catch (e) {{
    toast('Delete failed: ' + (e.message || 'unknown'), 'error');
  }}
}}
// ===== end F.4 Project screen =====


// =====================================================================
// F.6 -- Settings screen
// =====================================================================
//
// App-level prefs (the figure-level styling controls live in the
// editor's right panel).  Reads from /api/settings, writes back on
// every change via PATCH.  Single-user, so no debounce needed.

async function SettingsScreen(container) {{
  _ensureDesignStyles();
  container.className = 'app-shell';

  container.appendChild(_topBar({{
    crumbs: [{{ label: 'Home', href: '#/' }}, {{ label: 'Settings' }}],
  }}));
  const mainEl = h('div.app-main');
  container.appendChild(mainEl);
  const container_orig = container;
  // Redirect subsequent appendChild calls in this function to mainEl
  container = mainEl;

  // Load current settings + source list
  let settings = {{}};
  let sources = [];
  try {{
    const [sr, srcs] = await Promise.all([
      fetch(API_BASE + '/api/settings'),
      fetch(API_BASE + '/api/sources'),
    ]);
    if (sr.ok) settings = await sr.json();
    if (srcs.ok) sources = (await srcs.json()).sources || [];
  }} catch (_e) {{}}
  AppState.settings = settings;

  // Generic row helper: a labelled control on one line
  function fieldRow(label, control) {{
    return h('div', {{ style: {{ display: 'flex',
                                    alignItems: 'center',
                                    gap: '12px',
                                    marginBottom: '12px' }} }},
              [
                h('label', {{ style: {{ width: '220px',
                                          fontSize: '13px',
                                          color: '#71717a' }} }}, label),
                control,
              ]);
  }}

  async function patchSettings(patch) {{
    try {{
      const r = await fetch(API_BASE + '/api/settings', {{
        method: 'PATCH',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(patch),
      }});
      if (r.ok) {{
        settings = await r.json();
        toast('Settings saved', 'success');
      }} else {{
        toast('Save failed: HTTP ' + r.status, 'error');
      }}
    }} catch (e) {{
      toast('Save failed: ' + (e.message || e), 'error');
    }}
  }}

  // ---- General ----
  container.appendChild(h('div.section-title', 'General'));

  const detailSelect = h('select');
  for (const opt of ['coarse', 'normal', 'fine']) {{
    const o = document.createElement('option');
    o.value = opt; o.textContent = opt;
    if ((settings.default_detail || 'normal') === opt) o.selected = true;
    detailSelect.appendChild(o);
  }}
  detailSelect.addEventListener('change', () =>
    patchSettings({{ default_detail: detailSelect.value }}));
  container.appendChild(fieldRow('Default render detail', detailSelect));

  const strokeColor = h('input', {{ type: 'color',
    value: settings.default_stroke_color || '#00836a' }});
  strokeColor.addEventListener('change', () =>
    patchSettings({{ default_stroke_color: strokeColor.value }}));
  container.appendChild(fieldRow('Default stroke colour', strokeColor));

  const strokeWidth = h('input', {{ type: 'number', step: '0.5',
    min: '0.5', max: '15',
    value: settings.default_stroke_width_mm ?? 3.0,
    style: {{ width: '70px' }} }});
  strokeWidth.addEventListener('change', () =>
    patchSettings({{ default_stroke_width_mm: parseFloat(strokeWidth.value) }}));
  container.appendChild(fieldRow('Default stroke width (mm)', strokeWidth));

  const fillColor = h('input', {{ type: 'color',
    value: settings.default_fill_color || '#cce6e0' }});
  fillColor.addEventListener('change', () =>
    patchSettings({{ default_fill_color: fillColor.value }}));
  container.appendChild(fieldRow('Default fill colour', fillColor));

  const fillAlpha = h('input', {{ type: 'number', step: '0.05',
    min: '0', max: '1',
    value: settings.default_fill_alpha ?? 0.3,
    style: {{ width: '70px' }} }});
  fillAlpha.addEventListener('change', () =>
    patchSettings({{ default_fill_alpha: parseFloat(fillAlpha.value) }}));
  container.appendChild(fieldRow('Default fill alpha (0–1)', fillAlpha));

  // ---- Sources (read-only for now; editing in F.5+) ----
  container.appendChild(h('div.section-title', 'Sources'));
  const srcList = h('div', {{ style: {{ marginBottom: '24px' }} }});
  if (!sources.length) {{
    srcList.appendChild(h('div.empty', 'No sources configured.'));
  }} else {{
    for (const s of sources) {{
      srcList.appendChild(h('div', {{ style: {{ marginBottom: '8px',
                                                   fontSize: '13px' }} }},
        [
          h('strong', s.label),
          ' (' + s.id + ') ',
          s.onshape_ids
            ? h('span', {{ style: {{ color: '#0a8' }} }}, 'Onshape')
            : h('span', {{ style: {{ color: '#71717a' }} }}, 'local STEP'),
        ]));
    }}
  }}
  container.appendChild(srcList);

  // ---- Storage ----
  container.appendChild(h('div.section-title', 'Storage'));
  container.appendChild(h('div', {{ style: {{ fontSize: '13px',
                                                marginBottom: '24px' }} }},
    [
      h('strong', 'Projects folder: '),
      h('code', settings.projects_dir || '?'),
    ]));

  // ---- Reset ----
  container.appendChild(h('div.section-title', 'Danger zone'));
  const resetBtn = h('button',
    {{ style: {{ padding: '8px 12px', fontSize: '13px',
                  border: '1px solid #c44', color: '#c44',
                  background: '#fff', cursor: 'pointer',
                  borderRadius: '4px' }} }},
    'Reset to defaults');
  resetBtn.addEventListener('click', async () => {{
    if (!confirm('Reset ALL app settings to defaults?  '
                + 'Per-figure and per-project state is untouched.')) return;
    await fetch(API_BASE + '/api/settings/reset', {{ method: 'POST' }});
    renderRoute();   // re-mount this screen with fresh values
  }});
  container.appendChild(resetBtn);
}}

registerRoute(/^#\/settings$/, SettingsScreen);
// ===== end F.6 Settings screen =====


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
#${{_CRUMB_ID}} {{
  display: flex; gap: 8px; align-items: center;
  padding: 8px 16px; background: #f4f4f5; font-size: 13px;
  border-bottom: 1px solid #d4d4d8;
}}
#${{_CRUMB_ID}} a {{ color: #71717a; text-decoration: none; }}
#${{_CRUMB_ID}} a:hover {{ color: #00836a; text-decoration: underline; }}
#${{_CRUMB_ID}} .sep {{ color: #d4d4d8; }}
#${{_CRUMB_ID}} .current {{ color: #18181b; font-weight: 600; }}
`;

function _ensureCrumbStyles() {{
  if (document.getElementById('editor-crumb-styles')) return;
  const s = document.createElement('style');
  s.id = 'editor-crumb-styles';
  s.textContent = _CRUMB_CSS;
  document.head.appendChild(s);
}}

function _removeCrumb() {{
  document.getElementById(_CRUMB_ID)?.remove();
}}

function _installCrumb(parts) {{
  _ensureCrumbStyles();
  _removeCrumb();
  const crumb = h('div', {{ id: _CRUMB_ID }});
  parts.forEach((p, i) => {{
    if (i > 0) crumb.appendChild(h('span.sep', '/'));
    if (p.href) crumb.appendChild(h('a', {{ href: p.href }}, p.label));
    else crumb.appendChild(h('span.current', p.label));
  }});
  document.body.insertBefore(crumb, document.body.firstChild);
}}

async function EditorScreen(container, params) {{
  // We don't render into `container` -- the LEGACY editor is what we
  // want visible.  We unhide it, install a breadcrumb, then load
  // the figure on top of it.
  container.style.display = 'none';
  const header = document.querySelector('header');
  const main = document.querySelector('main');
  if (header) header.style.display = '';
  if (main) main.style.display = '';

  const projId = params[0];
  const figId = params[1];
  const opts = params[2] || {{}};
  const viewIdFromRoute = opts.viewId || null;

  // Fetch both in parallel so we know the project name for the crumb
  let proj = null, fig = null;
  try {{
    const [pr, fr] = await Promise.all([
      fetch(API_BASE + '/api/projects/' + encodeURIComponent(projId)),
      fetch(API_BASE + '/api/figures/' + encodeURIComponent(figId)),
    ]);
    if (pr.ok) proj = await pr.json();
    if (fr.ok) fig = await fr.json();
  }} catch (_e) {{}}

  // If no view id came in the route but the figure has one, use that
  // -- the variant strip needs the view id to know which figures are
  // siblings.
  const viewId = viewIdFromRoute || fig?.view_id || null;
  AppState.currentViewId = viewId;

  _installCrumb([
    {{ label: 'Home', href: '#/' }},
    {{ label: proj?.name || '(unknown project)',
       href: '#/project/' + encodeURIComponent(projId) }},
    {{ label: fig?.name || '(unknown figure)' }},
  ]);

  if (fig) {{
    AppState.currentProjectId = projId;
    AppState.currentFigureId = figId;
    // Yield a tick so the legacy editor's catalogue is fully ready,
    // then drop the figure in.  Skip the "replace current work?"
    // confirm: the user JUST clicked into this figure via the
    // workspace -- their intent isn't ambiguous.  autoGenerate fires
    // /api/render with the figure's camera so the 2D base view
    // appears without the user having to click "generate 2D" -- the
    // intended UX for a "subview with different highlighting".
    setTimeout(() => {{
      try {{ window._loadFigureIntoEditor(fig, {{
        skipConfirm: true,
        autoGenerate: !!(fig.camera && fig.camera.eye && fig.camera.target),
      }}); }}
      catch (_e) {{}}
      // Bind the legacy sidebar's project filter to THIS project so
      // the figures list only shows figures in this project, not
      // every figure ever made.  Defer one more tick so the project
      // selector has finished populating from /api/projects.
      setTimeout(() => {{
        const pSel = document.getElementById('project-sel');
        if (pSel && projId) {{
          // Make sure the option exists (it should, but defensively
          // add it if /api/projects hasn't returned yet).
          if (!Array.from(pSel.options).some(o => o.value === projId)) {{
            const opt = document.createElement('option');
            opt.value = projId;
            opt.textContent = proj?.name || projId;
            pSel.appendChild(opt);
          }}
          pSel.value = projId;
          pSel.dispatchEvent(new Event('change'));
        }}
        // Pre-fill the figure-name input + retitle the save button
        // so the user knows hitting save UPDATES this figure, not
        // creates a duplicate.  Reveal the secondary "save as new"
        // action for explicit forking.
        const fn = document.getElementById('fig-name');
        const sb = document.getElementById('btn-fig-save');
        const sa = document.getElementById('btn-fig-save-as');
        if (fn && fig) {{
          fn.value = fig.name || '';
          fn.placeholder = 'rename to save under different name';
        }}
        if (sb) {{
          sb.textContent = 'save';
          sb.title = 'Update "' + (fig?.name || 'this figure')
                      + '" with the current camera, selection, styles';
        }}
        if (sa) sa.style.display = '';
        // Capture the figure's loaded state as the dirty-tracking
        // baseline.  Indicator polls every 1s.
        if (window._markLoadedFigureBaseline) {{
          window._markLoadedFigureBaseline();
        }}
        // Render the variant strip if this figure is under a view
        if (viewId && typeof _renderVariantStrip === 'function') {{
          _renderVariantStrip(projId, viewId, figId);
        }}
        // Inject a "back to project" pill in the legacy header so
        // there's an obvious exit.  Sits right after the logo.
        const hdr = document.querySelector('header');
        if (hdr && !hdr.querySelector('.back-to-project')) {{
          const pill = document.createElement('a');
          pill.className = 'back-to-project';
          pill.href = '#/project/' + encodeURIComponent(projId);
          pill.title = 'Return to ' + (proj?.name || 'project') + ' workspace';
          pill.innerHTML = '<span style="font-size:13px;">←</span> '
                            + (proj?.name || 'Project');
          const h1 = hdr.querySelector('h1')?.parentElement;
          if (h1) h1.insertAdjacentElement('afterend', pill);
          else hdr.insertBefore(pill, hdr.firstChild);
        }}
      }}, 100);
    }}, 200);
  }}

  // Hide legacy sidebar sections that just add noise inside a
  // project (Saved views legacy / Onshape tree / STEP-order parts
  // list aren't useful for a project-bound figure).  CSS hook lives
  // in the design system so the editor stays uncluttered.
  document.body.classList.add('project-scoped-editor');

  // Teardown: remove the breadcrumb + restore the global view when
  // the user navigates away.
  return () => {{
    _removeCrumb();
    document.body.classList.remove('project-scoped-editor');
    // Restore the original save button + hide save-as-new
    const fn = document.getElementById('fig-name');
    const sb = document.getElementById('btn-fig-save');
    const sa = document.getElementById('btn-fig-save-as');
    if (fn) {{ fn.value = ''; fn.placeholder = 'figure name...'; }}
    if (sb) {{
      sb.textContent = 'save';
      sb.title = 'Capture current state as a new figure';
    }}
    if (sa) sa.style.display = 'none';
    if (typeof AppState !== 'undefined') {{
      AppState.currentFigureId = null;
      AppState.currentViewId = null;
    }}
    // Clear the variant strip
    const stripEl = document.getElementById('variants-strip');
    if (stripEl) stripEl.innerHTML = '';
    // Pull the back-to-project pill so non-project routes don't
    // inherit a stale exit
    document.querySelectorAll('header .back-to-project')
            .forEach(el => el.remove());
  }};
}}

registerRoute(/^#\/project\/([^/]+)\/figure\/([^/]+)$/, EditorScreen);
// View-aware editor route -- accept the same EditorScreen.  The route
// handler reads the view id from params[1] when present so the editor
// can later use the view's camera + configuration.  For now params are
// (projId, viewId, figId) when this matches.
registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)\/figure\/([^/]+)$/,
              (container, params) => EditorScreen(container,
                [params[0], params[2], {{ viewId: params[1] }}]));

// Update the Project screen's figure-card click to route into the
// editor instead of falling back to legacy.  We do this by replacing
// ProjectScreen with a slightly fuller version.
const _OrigProjectScreen = ProjectScreen;
ProjectScreen = async function(container, params) {{
  await _OrigProjectScreen(container, params);
  // After mount, rebind each figure card click to navigate properly.
  const projId = params[0];
  const cards = container.querySelectorAll('.grid .card:not(.placeholder)');
  // We need the actual figure ids -- refetch them in order.
  let figs = [];
  try {{
    const r = await fetch(API_BASE + '/api/projects/'
                            + encodeURIComponent(projId) + '/figures');
    if (r.ok) figs = (await r.json()).figures || [];
  }} catch (_e) {{}}
  cards.forEach((card, i) => {{
    if (i >= figs.length) return;
    const fid = figs[i].id;
    // Replace existing click handler by cloning the node (drops listeners)
    const clone = card.cloneNode(true);
    card.parentNode.replaceChild(clone, card);
    clone.addEventListener('click', () => {{
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/figure/' + encodeURIComponent(fid);
    }});
  }});
}};
// re-register so the wrapped version wins.  IMPORTANT: keep the
// Phase-3 view routes here too -- the original list was clobbered.
_routes.length = 0;
registerRoute(/^#\/$/, HomeScreen);
registerRoute(/^#\/project\/([^/]+)$/, ProjectScreen);
registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)$/, ViewScreen);
registerRoute(/^#\/project\/([^/]+)\/view\/([^/]+)\/figure\/([^/]+)$/,
              (container, params) => EditorScreen(container,
                [params[0], params[2], {{ viewId: params[1] }}]));
registerRoute(/^#\/project\/([^/]+)\/figure\/([^/]+)$/, EditorScreen);
registerRoute(/^#\/settings$/, SettingsScreen);

// Fire the router on first load.  renderRoute() handles empty-hash
// by redirecting to '#/'.
renderRoute();
// ===== end F.5 Editor route =====


const canvasWrap = $('canvas-wrap');
const fileSel = $('file-sel');
const viewSel = $('view-sel');
const partList = $('part-list');
const selectionInfo = $('selection-info');
const tooltip = $('tooltip');
const calloutCount = $('callout-count');

// state per (file,view): pan/zoom/highlights(Set)/annotations
const state = {{}};

function paneKey(f, v) {{ return f + '/' + v; }}
function getState(f, v) {{
  const k = paneKey(f, v);
  if (!state[k]) state[k] = {{
    tx: 0, ty: 0, scale: 1, highlights: new Set(), annotations: []
  }};
  return state[k];
}}

// Up-axis override table: maps a "what axis is up in the model" choice
// to the rotation that brings that axis onto world Z (our pipeline's
// canonical up). The 3D viewer applies this live; the Python side
// reads the same tuple from SOURCES (pre_rotate) and bakes it into HLR.
const UP_AXIS_ROT = {{
  'Z':  {{ axis: [0,0,1], angle:    0 }},   // identity
  'Y':  {{ axis: [1,0,0], angle:   90 }},   // Y -> Z
  'X':  {{ axis: [0,1,0], angle:  -90 }},   // X -> Z
  '-Z': {{ axis: [1,0,0], angle:  180 }},   // -Z -> Z
  '-Y': {{ axis: [1,0,0], angle:  -90 }},   // -Y -> Z
  '-X': {{ axis: [0,1,0], angle:   90 }},   // -X -> Z
}};

const upAxisSel = $('up-axis-sel');
function _upAxisKey(fid) {{ return 'upAxis_' + fid; }}
function loadUpAxisFor(fid) {{
  const v = localStorage.getItem(_upAxisKey(fid)) || 'Z';
  upAxisSel.value = v;
  return v;
}}
upAxisSel.addEventListener('change', () => {{
  localStorage.setItem(_upAxisKey(fileSel.value), upAxisSel.value);
  window.IFU_VIEWER?.applyUpAxisOverride?.(UP_AXIS_ROT[upAxisSel.value]);
  // Drop the existing Live SVG -- it was rendered against the old
  // orientation and would be misleading next to the freshly-rotated 3D.
  invalidateLiveView(fileSel.value);
}});

// Remove any cached "Live (from 3D)" view for a source.  Called whenever
// upstream state changes (Up: override, source switch) that would make
// the previously-generated SVG stale relative to the current 3D pane.
function invalidateLiveView(file_id) {{
  const fe = CATALOGUE.find(x => x.file_id === file_id);
  if (!fe) return;
  const had = fe.views.some(v => v.view_id === '__live__');
  fe.views = fe.views.filter(v => v.view_id !== '__live__');
  document
    .querySelectorAll(`.svg-pane[data-file="${{file_id}}"][data-view="__live__"]`)
    .forEach((p) => p.remove());
  if (!had) return;
  if (fileSel.value === file_id) {{
    const wasLive = viewSel.value === '__live__';
    refreshViews();
    if (wasLive) {{
      viewSel.value = fe.views[0]?.view_id || 'iso';
      refreshPane();
    }}
  }}
}}
$('btn-copy-orient').addEventListener('click', () => {{
  const r = UP_AXIS_ROT[upAxisSel.value];
  const line = (r.angle === 0)
    ? 'None,  # no pre_rotation needed'
    : `((${{r.axis.join(', ')}}), ${{r.angle}}),`;
  navigator.clipboard?.writeText(line);
  const btn = $('btn-copy-orient');
  const orig = btn.textContent;
  btn.textContent = 'copied!';
  setTimeout(() => {{ btn.textContent = orig; }}, 1500);
}});

// Populate selectors
CATALOGUE.forEach(fe => {{
  const opt = document.createElement('option');
  opt.value = fe.file_id; opt.textContent = fe.file_label;
  fileSel.appendChild(opt);
}});
// Fallback view list for sources that aren't in the baked CATALOGUE
// (e.g. Onshape imports landed at runtime).  Same iso / front / side
// presets used by the standard sources, with the same view directions
// the build pipeline uses.  No baked SVG -- live /api/render fills in.
const _FALLBACK_VIEWS = [
  {{ view_id: 'iso',   label: 'Iso 3/4 (front-right-above)',
     view_dir: [-0.5, -1.0, 0.7] }},
  {{ view_id: 'front', label: 'Front elevation',
     view_dir: [ 0.0, -1.0, 0.25] }},
  {{ view_id: 'side',  label: 'Side elevation',
     view_dir: [-1.0,  0.0, 0.25] }},
];

function refreshViews() {{
  viewSel.innerHTML = '';
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  const views = (fe && fe.views && fe.views.length)
                ? fe.views : _FALLBACK_VIEWS;
  views.forEach(ve => {{
    const o = document.createElement('option');
    o.value = ve.view_id; o.textContent = ve.label;
    viewSel.appendChild(o);
  }});
}}
fileSel.addEventListener('change', () => {{
  refreshViews(); refreshPane();
  const upStored = loadUpAxisFor(fileSel.value);
  window.IFU_VIEWER?.applyUpAxisOverride?.(UP_AXIS_ROT[upStored]);
}});
viewSel.addEventListener('change', refreshPane);
// P1.a: when the View dropdown changes (Iso / Front / Side / Live / saved),
// snap the 3D camera to match that view direction.  This is the cheap
// "make 3D match what I'm looking at in 2D" workflow Composer uses --
// no separate "view in 3D" button needed.  Skip when the view has no
// usable view_dir (e.g. a placeholder entry).
function snap3DToCurrentView() {{
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
}}
viewSel.addEventListener('change', snap3DToCurrentView);
refreshViews();

function activePane() {{
  return document.querySelector(
    `.svg-pane[data-file="${{fileSel.value}}"][data-view="${{viewSel.value}}"]`);
}}
function activeSvg() {{ return activePane()?.querySelector('svg'); }}

function refreshPartList() {{
  partList.innerHTML = '';
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  // Dynamic Onshape sources don't have a baked parts list -- show
  // a placeholder rather than crashing.
  if (!fe || !fe.parts || !fe.parts.length) {{
    const li = document.createElement('li');
    li.style.cssText = 'color:var(--muted);font-style:italic;padding:4px 0;';
    li.textContent = '(no parts list for live source)';
    partList.appendChild(li);
    return;
  }}
  fe.parts.forEach(p => {{
    const li = document.createElement('li');
    li.textContent = `[${{String(p.idx).padStart(3, '0')}}] ${{p.label}}`;
    li.dataset.part = p.idx;
    li.addEventListener('click', (ev) =>
      togglePartHighlight(p.idx, {{append: ev.ctrlKey || ev.metaKey}}));
    partList.appendChild(li);
  }});
}}

// Multi-select highlight: state.highlights is a Set of part idx.
//   - plain click   = replace selection with just this part
//                     (or clear if it was already the only one selected)
//   - Ctrl/Cmd-click = toggle this part in/out of the current selection
//   - Esc            = clear all
function togglePartHighlight(idx, opts) {{
  opts = opts || {{}};
  const append = !!opts.append;
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights) st.highlights = new Set();
  if (append) {{
    if (st.highlights.has(idx)) st.highlights.delete(idx);
    else st.highlights.add(idx);
  }} else {{
    if (st.highlights.size === 1 && st.highlights.has(idx)) {{
      st.highlights.clear();
    }} else {{
      st.highlights.clear();
      st.highlights.add(idx);
    }}
  }}
  applyHighlights();
}}

function clearHighlights() {{
  const st = getState(fileSel.value, viewSel.value);
  if (st.highlights) st.highlights.clear();
  applyHighlights();
}}

let _lastSilHighlightSig = '';
// Lightweight perf HUD: floats top-right, shows last-call durations
// for the hot paths.  Add ?dbg=1 to URL to enable, or set window._DBG = true.
const _DBG_ON = (new URLSearchParams(location.search)).get('dbg') === '1';
let _dbgEl = null;
function _dbgLine(label, ms, extra) {{
  if (!_DBG_ON) return;
  if (!_dbgEl) {{
    _dbgEl = document.createElement('div');
    _dbgEl.id = '_dbg_hud';
    _dbgEl.style.cssText = 'position:fixed;top:8px;right:8px;z-index:99999;'
      + 'background:rgba(0,0,0,.82);color:#0f0;font:11px/1.4 ui-monospace,Consolas;'
      + 'padding:6px 9px;border-radius:6px;pointer-events:none;'
      + 'white-space:pre;max-width:340px';
    document.body.appendChild(_dbgEl);
    _dbgEl._lines = {{}};
  }}
  _dbgEl._lines[label] = `${{label.padEnd(22)}} ${{ms.toFixed(1).padStart(7)}}ms${{extra?'  '+extra:''}}`;
  _dbgEl.textContent = Object.values(_dbgEl._lines).join('\n');
}}
function _dbgTime(label, fn, extra) {{
  if (!_DBG_ON) return fn();
  const t0 = performance.now();
  try {{ return fn(); }}
  finally {{ _dbgLine(label, performance.now() - t0, extra); }}
}}

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

function _ensureServerLogEl() {{
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
  clearBtn.addEventListener('click', () => {{
    _serverLogBody.innerHTML = '';
  }});
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
}}

function _serverLogRender(events) {{
  if (!events || !events.length) return;
  const wasAtBottom =
      _serverLogBody.scrollTop + _serverLogBody.clientHeight
      >= _serverLogBody.scrollHeight - 4;
  for (const e of events) {{
    const line = document.createElement('div');
    let color = '#d4d4d8';
    if (e.level === 'err')   color = '#fda4af';
    else if (e.level === 'warn')  color = '#fde047';
    else if (e.level === 'ok')    color = '#86efac';
    else if (e.level === 'req')   color = '#93c5fd';
    line.style.color = color;
    const parts = [`[${{e.t}}]`, (e.level || '').padEnd(4)];
    for (const [k, v] of Object.entries(e)) {{
      if (k === 't' || k === 'level' || k === 'seq') continue;
      if (v === null || v === undefined || v === '') continue;
      parts.push(`${{k}}=${{v}}`);
    }}
    line.textContent = parts.join(' ');
    _serverLogBody.appendChild(line);
  }}
  // Cap to last 200 lines so the DOM doesn't blow up
  while (_serverLogBody.children.length > 200) {{
    _serverLogBody.removeChild(_serverLogBody.firstChild);
  }}
  if (wasAtBottom) _serverLogBody.scrollTop = _serverLogBody.scrollHeight;
}}

async function _serverLogPoll() {{
  if (!_serverLogOpen) return;
  try {{
    const r = await fetch(API_BASE + '/api/debug/log'
                            + (_serverLogSince ? `?since=${{_serverLogSince}}` : ''));
    if (r.ok) {{
      const data = await r.json();
      _serverLogSince = data.latest_seq || _serverLogSince;
      _serverLogRender(data.events || []);
    }}
  }} catch (_e) {{}}
  _serverLogTimer = setTimeout(_serverLogPoll, 1500);
}}

function _toggleServerLog(forceOpen) {{
  _ensureServerLogEl();
  if (forceOpen === undefined) forceOpen = !_serverLogOpen;
  _serverLogOpen = forceOpen;
  _serverLogEl.style.display = _serverLogOpen ? 'flex' : 'none';
  localStorage.setItem('ifu:server_log_open', _serverLogOpen ? '1' : '0');
  if (_serverLogOpen) {{
    // On (re-)open: ask for the whole buffer once so we have context
    _serverLogSince = 0;
    if (_serverLogTimer) clearTimeout(_serverLogTimer);
    _serverLogPoll();
  }} else if (_serverLogTimer) {{
    clearTimeout(_serverLogTimer);
    _serverLogTimer = null;
  }}
}}

// Wire up the header button (will no-op if the element isn't on this
// page, e.g. on a non-editor route)
const _logBtn = document.getElementById('btn-server-log');
if (_logBtn) {{
  _logBtn.addEventListener('click', () => _toggleServerLog());
}}
// Restore previous open/closed state.  Default OFF so it doesn't get
// in the way unless the user asked for it.
if (localStorage.getItem('ifu:server_log_open') === '1') {{
  _toggleServerLog(true);
}}
// Also open it automatically on ?dbg=1 so the perf HUD + server log
// pair up usefully.
if (_DBG_ON && localStorage.getItem('ifu:server_log_open') !== '0') {{
  _toggleServerLog(true);
}}
window._toggleServerLog = _toggleServerLog;

function applyHighlights() {{
  const _t0 = _DBG_ON ? performance.now() : 0;
  const st = getState(fileSel.value, viewSel.value);
  const set = st.highlights || new Set();
  const any = set.size > 0;
  const svg = activeSvg();
  if (_DBG_ON) {{
    const preview = [...set].slice(0, 12).join(',');
    _dbgLine('SELECTED', 0, set.size + ': ' + preview);
  }}
  if (svg) {{
    const partCount = svg.querySelectorAll('.part').length;
    _dbgTime('toggle-classes', () => {{
      svg.querySelectorAll('.part').forEach(p => {{
        const idx = parseInt(p.dataset.part);
        const hit = set.has(idx);
        p.classList.toggle('highlight', hit);
        p.classList.toggle('dim', any && !hit);
      }});
    }}, `${{partCount}} parts`);
    // Closed-silhouette fill / bold-outline overlay.  Uses the local
    // outline_v polylines as an immediate approximation; in parallel,
    // fetchTrueSilhouettes() asks the server for per-part HLR (no
    // occluders) and re-runs applyHighlights when the response arrives.
    _dbgTime('applySilhouetteFill', () => applySilhouetteFill(
      svg, set,
      $('sty-fill-on').checked,
      $('sty-fill').value,
      parseFloat($('sty-fill-opacity').value),
      $('sty-stroke').value,
      parseFloat($('sty-width').value),
    ), `${{set.size}} sel`);
    // Kick the server fetches ONLY when the highlight set has actually
    // changed (style-only refreshes are routed through
    // restyleSilhouetteOnly and don't get here).  Bold edge now uses
    // the rasterized footprint, so we fetch it on demand for the
    // selected parts; old silhouette fetch is gated on the shade
    // checkbox inside fetchTrueSilhouettes.
    const sig = set.size ? [...set].sort((a,b)=>a-b).join(',') : '';
    if (sig !== _lastSilHighlightSig) {{
      _lastSilHighlightSig = sig;
      if (set.size > 0) {{
        setTimeout(fetchSelectedFootprints, 0);
        setTimeout(fetchTrueSilhouettes, 0);
      }}
    }}
  }}
  partList.querySelectorAll('li').forEach(li => {{
    li.classList.toggle('highlighted', set.has(parseInt(li.dataset.part)));
  }});
  if (treeRoot) {{
    treeRoot.querySelectorAll('.tree-row').forEach(r => {{
      const idx = _tree_to_part_idx[r.dataset.treeId];
      r.classList.toggle('highlighted', idx != null && set.has(idx));
    }});
  }}
  if (set.size === 0) {{
    selectionInfo.textContent = 'Nothing selected';
  }} else if (set.size === 1) {{
    const idx = [...set][0];
    const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
    const p = fe.parts.find(x => x.idx === idx);
    selectionInfo.innerHTML = `<b>Part ${{idx}}</b><br>${{p ? p.label : ''}}`;
  }} else {{
    const list = [...set].sort((a,b)=>a-b);
    const preview = list.slice(0, 8).join(', ') + (list.length > 8 ? ', ...' : '');
    selectionInfo.innerHTML = `<b>${{set.size}} parts</b> selected<br>` +
      `<span style="font-family: ui-monospace, Consolas, monospace; font-size: 11px;">${{preview}}</span>`;
  }}
  _dbgTime('applyHighlights3D', () =>
    window.IFU_VIEWER?.applyHighlights3D?.(set), `${{set.size}} sel`);
  // Light up the matching preset (or clear) so the user can see at
  // a glance what's already applied to the selection.
  if (typeof _refreshPresetActiveState === 'function') {{
    try {{ _refreshPresetActiveState(); }} catch (_e) {{}}
  }}
  if (_DBG_ON) {{
    _dbgLine('applyHighlights TOTAL', performance.now() - _t0,
      `${{set.size}} sel`);
  }}
}}

// Keyboard shortcuts (only when no input/select/textarea has focus).
//   Esc      -- clear selection
//   1/2/3    -- 2D / Split / 3D layout
//   R        -- reset 3D camera to current view's direction
//   F        -- fit (reset pan/zoom on active 2D pane)
window.addEventListener('keydown', (e) => {{
  // Ctrl/Cmd+S = save the current figure.  This works even if focus
  // is in an <input> (e.g. the fig-name field) because we want save
  // to be available everywhere in the editor.  Browser default is
  // "save page" -- preventDefault so we capture it.
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {{
    e.preventDefault();
    if (typeof saveCurrentAsFigure === 'function') saveCurrentAsFigure();
    return;
  }}
  const t = e.target;
  if (t && /^(INPUT|TEXTAREA|SELECT)$/i.test(t.tagName)) return;
  if (e.key === 'Escape') return clearHighlights();
  if (e.key === '1') return $('lay-2d').click();
  if (e.key === '2') return $('lay-split').click();
  if (e.key === '3') return $('lay-3d').click();
  if (e.key.toLowerCase() === 'r') {{
    // R = re-snap 3D camera to current 2D view direction
    snap3DToCurrentView?.();
    return;
  }}
  if (e.key.toLowerCase() === 'f') {{
    // F = reset pan/zoom on the active 2D pane
    const pane = activePane();
    if (pane) {{
      const st = getState(pane.dataset.file, pane.dataset.view);
      st.tx = 0; st.ty = 0; st.scale = 1;
      applyTransform(pane);
    }}
    return;
  }}
}});

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

  // Click on a path -> walk up to .part -> highlight.
  // Ctrl/Cmd-click toggles into a multi-selection.
  svg.addEventListener('click', e => {{
    if (svg.classList.contains('annotate-mode')) {{
      handleAnnotateClick(e, svg, pane); return;
    }}
    let p = e.target;
    while (p && p !== svg && !p.classList?.contains('part')) p = p.parentElement;
    if (p && p.classList?.contains('part')) {{
      togglePartHighlight(parseInt(p.dataset.part),
                          {{append: e.ctrlKey || e.metaKey}});
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
  const _t0 = _DBG_ON ? performance.now() : 0;
  document.querySelectorAll('.svg-pane').forEach(p => p.classList.remove('active'));
  const pane = activePane();
  if (!pane) return;
  pane.classList.add('active');
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

// --- Screenshot exporter ------------------------------------------------
// Captures whichever panes are currently visible (2D, 3D, or both for
// Split) into a single PNG so the user can save the rendered comparison
// for iteration / IFU artwork prep.
// - 2D pane: serialise the SVG, rasterise via <canvas>
// - 3D pane: read the WebGL canvas directly
// - Split:   composite the two side-by-side onto a single canvas
async function svgPaneToCanvas(pane, width, height) {{
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
  if (styleEl && styleEl.textContent) {{
    const inline = document.createElementNS('http://www.w3.org/2000/svg', 'style');
    inline.textContent = styleEl.textContent;
    clone.insertBefore(inline, clone.firstChild);
  }}
  const xml = new XMLSerializer().serializeToString(clone);
  const blob = new Blob([xml], {{ type: 'image/svg+xml;charset=utf-8' }});
  const url = URL.createObjectURL(blob);
  const img = new Image();
  await new Promise((res, rej) => {{
    img.onload = res; img.onerror = rej; img.src = url;
  }});
  const cnv = document.createElement('canvas');
  cnv.width = width; cnv.height = height;
  const ctx = cnv.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);
  ctx.drawImage(img, 0, 0, width, height);
  URL.revokeObjectURL(url);
  return cnv;
}}

async function captureScreenshot() {{
  const wantS = document.body.classList.contains('layout-split');
  const want2 = wantS || document.body.classList.contains('layout-2d');
  const want3 = wantS || document.body.classList.contains('layout-3d');
  let canvas2 = null, canvas3 = null;

  if (want2) {{
    const pane = activePane();
    if (pane) {{
      const r = pane.getBoundingClientRect();
      canvas2 = await svgPaneToCanvas(pane, Math.round(r.width), Math.round(r.height));
    }}
  }}
  if (want3) {{
    const webglCanvas = document.getElementById('webgl-canvas');
    if (webglCanvas) {{
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
    }}
  }}

  // Composite into one image
  let final;
  if (canvas2 && canvas3) {{
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
  }} else {{
    final = canvas2 || canvas3;
  }}
  if (!final) return;

  // Trigger download
  final.toBlob((blob) => {{
    const a = document.createElement('a');
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.href = URL.createObjectURL(blob);
    a.download = `${{fileSel.value}}_${{viewSel.value}}_${{ts}}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }}, 'image/png');
}}

// Stub kept here so the synchronous capture call resolves -- the module
// script overrides this with a real `renderer.render(scene, camera)` call.
function renderer3d_request_present() {{}}

$('btn-screenshot').addEventListener('click', () => {{
  captureScreenshot().catch(err => {{
    console.error('screenshot failed:', err);
    alert('Screenshot failed: ' + err.message);
  }});
}});

// P2.b: hi-detail render of the currently-visible viewport.  We
// compute the visible bbox in projector (u,v) space, POST it to
// /api/render_region at a finer mesh/sample, then overlay the
// returned SVG as a new <g class="layer-region-detail"> group inside
// the active pane's scale-flip group.  Clicking again clears it.
async function detailRenderActive() {{
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
  if (viewG) {{
    const t = viewG.getAttribute('transform') || '';
    const tm = t.match(/translate\\(([-\\d.]+)\\s+([-\\d.]+)\\)\\s*scale\\(([-\\d.]+)\\)/);
    if (tm) {{
      const tx = parseFloat(tm[1]), ty = parseFloat(tm[2]), sc = parseFloat(tm[3]);
      // Visible area in viewBox coords = (viewBox + pan) / scale
      const vw = vb[2] / sc, vh = vb[3] / sc;
      const vx = vb[0] - tx / sc, vy = vb[1] - ty / sc;
      bboxUv = [vx, -(vy + vh), vx + vw, -vy];
    }}
  }}
  const fid = fileSel.value, vid = viewSel.value;
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  const body = {{
    file_id: fid,
    bbox_uv: bboxUv,
    mesh_defl: 0.3,
    sample_defl: 0.3,
  }};
  // Camera: same as fetchTrueSilhouettes
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {{
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  }} else if (ve && ve.view_dir) {{
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  }} else {{
    alert('No view direction for the active source/view');
    return;
  }}
  const btn = $('btn-detail');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ rendering...';
  try {{
    const r = await fetch(API_BASE + '/api/render_region', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
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
    if (incomingScaleG) {{
      Array.from(incomingScaleG.children).forEach(ch => layer.appendChild(ch.cloneNode(true)));
    }} else {{
      Array.from(incoming.children).forEach(ch => layer.appendChild(ch.cloneNode(true)));
    }}
    scaleG.appendChild(layer);
    const nParts = r.headers.get('X-Region-Parts') || '?';
    const seconds = r.headers.get('X-Region-Seconds') || '?';
    btn.textContent = `✓ ${{nParts}} parts in ${{seconds}}s`;
    $('btn-detail-clear').style.display = '';
    setTimeout(() => {{ btn.disabled = false; btn.textContent = orig; }}, 2500);
  }} catch (e) {{
    btn.textContent = '✗ ' + (e.message || 'failed');
    setTimeout(() => {{ btn.disabled = false; btn.textContent = orig; }}, 3000);
  }}
}}
$('btn-detail').addEventListener('click', detailRenderActive);
$('btn-detail-clear').addEventListener('click', () => {{
  const svg = activeSvg();
  if (!svg) return;
  svg.querySelector('g.layer-region-detail')?.remove();
  $('btn-detail-clear').style.display = 'none';
}});

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

// --- Onshape feature tree sidebar -----------------------------------------
// Renders the live instance tree pulled from Onshape into the left sidebar.
// Click a leaf-Part to highlight the matching STEP solid (in both the 2D
// SVG view and the 3D view-finder).  v1 mapping: i-th leaf Part in tree
// order <-> i-th solid in STEP order (positional, since cadquery's STEP
// importer drops Onshape part names).

const treeRoot = $('tree-root');
const treeStatus = $('tree-status');
let _tree_id_counter = 0;
let _tree_idmap = {{}};        // tree-node-id -> tree-node-object
let _tree_to_part_idx = {{}};  // tree-node-id -> solid idx (or null)
let _leafByPartIdx = new Map(); // solid idx -> leaf tree node (for grouping)

function _flattenLeaves(nodes, out) {{
  for (const n of nodes || []) {{
    if (n.type === 'Part') out.push(n);
    else if (n.children && n.children.length) _flattenLeaves(n.children, out);
  }}
}}

// Stamp every tree node with a back-pointer to its parent so the
// "expand to parent group" operation can walk upward without recursing
// the whole tree per call.
function _annotateParents(nodes, parent) {{
  for (const n of nodes || []) {{
    n._parent = parent || null;
    if (n.children && n.children.length) _annotateParents(n.children, n);
  }}
}}

function refreshTree() {{
  treeRoot.innerHTML = '';
  _tree_idmap = {{}};
  _tree_to_part_idx = {{}};
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  const tree = ONSHAPE_TREES[fileSel.value];
  if (!tree || !tree.length) {{
    treeStatus.textContent = 'No tree for this source.';
    return;
  }}
  // positional leaf->solid map + parent-back-pointers.  Each leaf may
  // map to MULTIPLE solid indices (multi-body STEP Part); for Onshape
  // trees the API gives us one idx per leaf, but for STEP trees the
  // server pre-computes _solid_indices as a contiguous range.
  _annotateParents(tree, null);
  const leaves = [];
  _flattenLeaves(tree, leaves);
  let cursor = 0;
  leaves.forEach((leaf, i) => {{
    if (Array.isArray(leaf._solid_indices) && leaf._solid_indices.length) {{
      // STEP-tree leaf: server already attached the index range
      leaf._mapped_idx = leaf._solid_indices[0];
    }} else if (i < fe.parts.length) {{
      leaf._mapped_idx = fe.parts[i].idx;
      leaf._solid_indices = [leaf._mapped_idx];
      cursor = i + 1;
    }} else {{
      leaf._mapped_idx = null;
      leaf._solid_indices = [];
    }}
  }});
  // Reverse map: any solid idx -> tree node (so 3D click can find its
  // sub-assembly).  Each idx in _solid_indices points back to the leaf.
  _leafByPartIdx = new Map();
  for (const leaf of leaves) {{
    for (const idx of (leaf._solid_indices || [])) {{
      _leafByPartIdx.set(idx, leaf);
    }}
  }}
  const totalBodies = leaves.reduce(
    (s, l) => s + (l._solid_indices ? l._solid_indices.length : 0), 0);
  treeStatus.textContent =
    `${{leaves.length}} part instances, ${{totalBodies}} bodies. ` +
    `Click an Assembly to select everything under it.`;

  function buildNode(n) {{
    const id = String(++_tree_id_counter);
    _tree_idmap[id] = n;
    if (n.type === 'Part' && n._mapped_idx != null) {{
      _tree_to_part_idx[id] = n._mapped_idx;
    }}
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
    if (hasKids) {{
      const ul = document.createElement('ul');
      n.children.forEach(c => ul.appendChild(buildNode(c)));
      li.appendChild(ul);
      twisty.addEventListener('click', ev => {{
        ev.stopPropagation();
        const collapsed = ul.style.display === 'none';
        ul.style.display = collapsed ? '' : 'none';
        twisty.textContent = collapsed ? '▾' : '▸';
      }});
    }}
    row.addEventListener('click', (ev) => {{
      const node = _tree_idmap[id];
      const append = ev.ctrlKey || ev.metaKey;
      if (!node) return;
      // Gather all the solid indices this row represents.  Part leaves
      // can have multiple solids (multi-body STEP Part); Assemblies pull
      // in every leaf descendant's full index range.
      let indices = [];
      if (node.type === 'Part') {{
        indices = (node._solid_indices && node._solid_indices.length)
          ? node._solid_indices.slice()
          : (_tree_to_part_idx[id] != null ? [_tree_to_part_idx[id]] : []);
      }} else {{
        const leaves = [];
        _flattenLeaves([node], leaves);
        for (const l of leaves) {{
          for (const i of (l._solid_indices || [])) indices.push(i);
        }}
      }}
      if (!indices.length) return;
      const st = getState(fileSel.value, viewSel.value);
      if (!st.highlights) st.highlights = new Set();
      if (!append) st.highlights.clear();
      indices.forEach(i => st.highlights.add(i));
      applyHighlights();
    }});
    return li;
  }}
  tree.forEach(n => treeRoot.appendChild(buildNode(n)));
}}

// Inject a freshly-rendered SVG (from the local server's /api/render) as a
// "live" view for the given source.  Per-source: each source has its own
// __live__ slot that gets overwritten on every generate.
// camera context (eye/target/up_axis) attached when a Live render fires;
// the silhouette endpoint reuses these so the per-part HLR projects into
// the EXACT same (u,v) space as the baked SVG.
const _liveCamCtx = {{}};  // file_id -> {{eye, target, up_axis}}
function _setLiveCamCtx(file_id, ctx) {{ _liveCamCtx[file_id] = ctx; }}
function _getLiveCamCtx(file_id) {{ return _liveCamCtx[file_id] || null; }}

function injectLiveSVG(file_id, view_dir, svgText) {{
  // Strip any XML prolog and stamp an id on the <svg> so existing helpers
  // (applyTransform / attachInteractivity) can find it.
  const cleaned = svgText
    .replace(/<\\?xml[^>]*\\?>\\s*/, '')
    .replace('<svg', `<svg id="svg_${{file_id}}___live__"`);

  // CRITICAL: every cached overlay keyed by (file_id, '__live__') is
  // tied to the camera the previous render used.  We're about to swap
  // in geometry from a DIFFERENT camera, so the stored polylines no
  // longer correspond to the new SVG's pixel space.  If we don't drop
  // them, the next applyHighlights() will paint last-camera footprints
  // onto this-camera SVG -- closed loops in the wrong place.
  const vid = '__live__';
  for (const k of Array.from(_footprintCache.keys())) {{
    if (k.startsWith(file_id + '|' + vid + '|')) _footprintCache.delete(k);
  }}
  for (const k of Array.from(_trueSilCache.keys())) {{
    if (k.startsWith(file_id + '|' + vid + '|')) _trueSilCache.delete(k);
  }}
  for (const k of Array.from(_groupSilCache.keys())) {{
    if (k.startsWith(file_id + '|' + vid + '|')) _groupSilCache.delete(k);
  }}
  _footprintViewFetched.delete(_fpViewKey(file_id, vid));
  // Force the next fetchSelected* to re-fetch even if the selection
  // didn't change between renders.
  _lastSilHighlightSig = '__force_refetch__';

  // Re-use or create the live pane for this source.
  let pane = document.querySelector(
    `.svg-pane[data-file="${{file_id}}"][data-view="__live__"]`
  );
  if (!pane) {{
    pane = document.createElement('div');
    pane.className = 'svg-pane';
    pane.dataset.file = file_id;
    pane.dataset.view = '__live__';
    pane.dataset.svgId = `svg_${{file_id}}___live__`;
    canvasWrap.appendChild(pane);
  }}
  pane.innerHTML = cleaned;
  // attached flag must be cleared so attachInteractivity rewires the new svg
  pane.querySelector('svg')?.removeAttribute('data-attached');

  // Mine the SVG for its part indices.  Dynamic Onshape sources have
  // no baked CATALOGUE parts list, but the SVG itself contains
  //   <g class="part part-NNN" data-part="N">
  // for every part with visible geometry.  Reading those means we
  // can prefetch the assembly footprint raster ahead of the user's
  // first click -- otherwise that first click eats a ~46s wait
  // before the closed-loop outline appears.
  try {{
    const ids = new Set();
    pane.querySelectorAll('[data-part]').forEach(g => {{
      const n = parseInt(g.dataset.part, 10);
      if (Number.isFinite(n)) ids.add(n);
    }});
    if (ids.size) {{
      // Update / create the CATALOGUE entry's parts list so
      // refreshPartList + prefetchFootprintsForCurrentView find them.
      let cf = CATALOGUE.find(x => x.file_id === file_id);
      if (!cf) {{
        cf = {{ file_id, file_label: file_id, parts: [], views: [] }};
        CATALOGUE.push(cf);
      }}
      const sorted = [...ids].sort((a, b) => a - b);
      cf.parts = sorted.map(idx => ({{ idx, label: 'part_'
                                          + String(idx).padStart(3, '0') }}));
    }}
  }} catch (_e) {{}}

  // Add or update the "Live" option in the View dropdown (per-source).
  // Dynamic Onshape imports aren't in the baked CATALOGUE -- create
  // a stub entry on the fly so refreshViews() can populate the View
  // dropdown and refreshPane() can find the newly-injected pane.
  let fe = CATALOGUE.find(x => x.file_id === file_id);
  if (!fe) {{
    fe = {{
      file_id: file_id,
      file_label: file_id,
      parts: [],
      views: [],
    }};
    CATALOGUE.push(fe);
  }}
  let existing = fe.views.find(v => v.view_id === '__live__');
  if (existing) {{
    existing.view_dir = view_dir;
  }} else {{
    fe.views.push({{
      view_id: '__live__',
      label: '⚡ Live (from 3D)',
      view_dir: view_dir,
    }});
  }}
  // Refresh the View dropdown if this is the active source.  If the
  // fileSel value doesn't match (route switched between variants of
  // different views, dropdown stale), force it before refreshing so
  // we don't leave the user with a blank pane.
  if (fileSel.value !== file_id) {{
    const hasOpt = Array.from(fileSel.options)
                          .some(o => o.value === file_id);
    if (!hasOpt) {{
      const opt = document.createElement('option');
      opt.value = file_id; opt.textContent = file_id;
      fileSel.appendChild(opt);
    }}
    fileSel.value = file_id;
  }}
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
  try {{
    const st = getState(file_id, '__live__');
    if (st) {{ st.tx = 0; st.ty = 0; st.scale = 1; }}
    applyTransform(pane);
  }} catch (_e) {{}}
  // Force layout to split / 2D mode so the SVG is visible (some
  // route entries leave the user on layout-3d).
  if (typeof setLayout === 'function') {{
    document.body.classList.contains('layout-3d') && setLayout('split');
  }}
  // Fire the assembly-wide footprint raster in the background so the
  // user's FIRST click on a part gets a closed-loop outline instantly
  // -- not the 46-second wait that made the outline look "stuck on
  // partial open polylines".  The endpoint memoises per-view so this
  // pays the raster cost once; further selections in the same view
  // hit the cache.
  if (typeof prefetchFootprintsForCurrentView === 'function') {{
    setTimeout(() => prefetchFootprintsForCurrentView(), 100);
  }}
}}

// Expose for the module script (cross-script comms)
window.IFU_VIEWER = {{
  togglePartHighlight,
  clearHighlights,
  injectLiveSVG,
  setLayout: (name) => setLayout(name),
  getActiveFileId: () => fileSel.value,
  getActiveViewDir: () => {{
    const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
    const ve = fe?.views.find(v => v.view_id === viewSel.value);
    return ve?.view_dir;
  }},
  getActiveUpAxis: () => UP_AXIS_ROT[upAxisSel.value],
  onFileChange: (cb) => fileSel.addEventListener('change', cb),
  onViewChange: (cb) => viewSel.addEventListener('change', cb),
  _setLiveCamCtx,
  _getLiveCamCtx,
}};

// Tree refresh on source change
fileSel.addEventListener('change', refreshTree);

// --- Tree search ------------------------------------------------------------
// Live-filter the tree as the user types. Matches any name (substring,
// case-insensitive); shows matching leaves and their ancestor path so the
// hierarchy stays readable. Empty query = show all.
const treeSearch = $('tree-search');
function filterTree(q) {{
  q = (q || '').trim().toLowerCase();
  const allLi = treeRoot.querySelectorAll('li');
  if (!q) {{
    allLi.forEach(li => li.classList.remove('filtered-out'));
    return;
  }}
  // First pass: mark every li as filtered-out
  allLi.forEach(li => li.classList.add('filtered-out'));
  // Second pass: for each li whose name matches, un-filter it AND all ancestor li's
  allLi.forEach(li => {{
    const row = li.querySelector(':scope > .tree-row');
    if (!row) return;
    const name = row.textContent.toLowerCase();
    if (name.includes(q)) {{
      let cur = li;
      while (cur && cur.classList.contains('filtered-out')) {{
        cur.classList.remove('filtered-out');
        cur = cur.parentElement?.closest('li');
      }}
      // Also reveal direct descendants of a matched node so the user sees
      // what's inside the matched subtree.
      li.querySelectorAll('li').forEach(d => d.classList.remove('filtered-out'));
    }}
  }});
}}
treeSearch.addEventListener('input', () => filterTree(treeSearch.value));
// Esc inside the search clears it (separate from the global Esc which
// clears selection -- only act on Esc if the search is focused and has content)
treeSearch.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape' && treeSearch.value) {{
    treeSearch.value = '';
    filterTree('');
    e.stopPropagation();   // don't bubble to the global Esc-clears-selection
  }}
}});

// --- Layout (2D / Split / 3D) ----------------------------------------------
// Three-segment control replacing the old hidden "3D view-finder" toggle.
// Body class drives the grid (grid-template-areas reflow between layouts);
// the module script wakes / sleeps three.js based on whether the WebGL
// pane is currently visible.

const LAYOUTS = ['2d', 'split', '3d'];
let currentLayout = '2d';
function setLayout(name) {{
  if (!LAYOUTS.includes(name)) return;
  currentLayout = name;
  document.body.classList.remove('layout-2d', 'layout-split', 'layout-3d');
  document.body.classList.add('layout-' + name);
  ['lay-2d', 'lay-split', 'lay-3d'].forEach(id => {{
    $(id).classList.toggle('active', id === 'lay-' + name);
  }});
  // tell three.js to (de)activate
  const show3d = (name === 'split' || name === '3d');
  window.IFU_VIEWER?.set3DActive?.(show3d);
}}
$('lay-2d').addEventListener('click', () => setLayout('2d'));
$('lay-split').addEventListener('click', () => setLayout('split'));
$('lay-3d').addEventListener('click', () => setLayout('3d'));

// --- Saved views --------------------------------------------------------
// Per-source list of {{name, eye, target, up_axis}} kept in localStorage so
// the camera angles a user has dialled in survive reloads.  No server
// involvement -- recall just snaps the 3D camera + Up: dropdown.

function _savedViewsKey(fid) {{ return 'savedViews_' + fid; }}
function loadSavedViews(fid) {{
  try {{
    return JSON.parse(localStorage.getItem(_savedViewsKey(fid)) || '[]');
  }} catch (_e) {{ return []; }}
}}
function persistSavedViews(fid, list) {{
  localStorage.setItem(_savedViewsKey(fid), JSON.stringify(list));
}}
function refreshSavedViews() {{
  const ul = $('saved-views');
  ul.innerHTML = '';
  const list = loadSavedViews(fileSel.value);
  if (!list.length) {{
    ul.innerHTML = '<li style="color:var(--muted); font-style:italic;">' +
                   'none yet — orbit the 3D, then click save</li>';
    return;
  }}
  list.forEach((v, i) => {{
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = v.name;
    name.title = 'click to recall';
    name.addEventListener('click', () => recallSavedView(v));
    const del = document.createElement('button');
    del.textContent = '×';
    del.title = 'delete';
    del.addEventListener('click', (e) => {{
      e.stopPropagation();
      const cur = loadSavedViews(fileSel.value);
      cur.splice(i, 1);
      persistSavedViews(fileSel.value, cur);
      refreshSavedViews();
    }});
    li.appendChild(name); li.appendChild(del);
    ul.appendChild(li);
  }});
}}
function recallSavedView(v) {{
  // Make sure 3D is visible so OrbitControls can move
  if (!is3DCurrentlyShown()) setLayout('split');
  // Apply Up: rotation if different
  if (v.up_axis && upAxisSel.value !== v.up_axis) {{
    upAxisSel.value = v.up_axis;
    upAxisSel.dispatchEvent(new Event('change'));
  }}
  window.IFU_VIEWER?.snapCameraTo?.(v.eye, v.target);
}}
function is3DCurrentlyShown() {{
  return document.body.classList.contains('layout-split')
      || document.body.classList.contains('layout-3d');
}}
$('btn-save-view').addEventListener('click', () => {{
  const nameInput = $('view-name');
  const name = (nameInput.value || '').trim();
  if (!name) {{ nameInput.focus(); return; }}
  const cam = window.IFU_VIEWER?.getCameraEyeTarget?.();
  if (!cam) {{ alert('Open the 3D pane first.'); return; }}
  const entry = {{
    name,
    eye:    cam.eye,
    target: cam.target,
    up_axis: upAxisSel.value,
  }};
  const cur = loadSavedViews(fileSel.value);
  // Replace any same-named entry
  const existing = cur.findIndex(v => v.name === name);
  if (existing >= 0) cur[existing] = entry;
  else cur.push(entry);
  persistSavedViews(fileSel.value, cur);
  nameInput.value = '';
  refreshSavedViews();
}});
fileSel.addEventListener('change', refreshSavedViews);

// --- Figures (Phase A) -----------------------------------------------
// A figure is the full editor state (camera + selection + per-part
// styles + layer toggles + notes) saved as a JSON file in
// out/figures/.  Lives on the server, not in localStorage.

const figuresList = document.getElementById('figures-list');

async function listFigures() {{
  if (typeof API_BASE !== 'string') return [];
  try {{
    const r = await fetch(API_BASE + '/api/figures');
    if (!r.ok) return [];
    return (await r.json()).figures || [];
  }} catch (_e) {{ return []; }}
}}

function _gatherCurrentState() {{
  // Snapshot everything the editor currently shows so we can rehydrate
  // it later from the figure JSON.
  const fid = fileSel.value;
  const vid = viewSel.value;
  const st = getState(fid, vid);
  const cam = window.IFU_VIEWER?.getCameraEyeTarget?.();
  const layersOn = {{}};
  document.querySelectorAll('input[data-layer]').forEach(cb => {{
    layersOn[cb.dataset.layer] = !!cb.checked;
  }});
  return {{
    source_id: fid,
    view_id: vid,
    camera: cam ? {{
      eye: cam.eye, target: cam.target,
      up_axis: upAxisSel.value,
    }} : null,
    selection: st.highlights ? [...st.highlights] : [],
    styles_per_part: loadPartStyles(fid),
    layers_on: layersOn,
    detail: parseFloat($('sty-width').value) >= 5 ? "fine" : "normal",
    annotations: (st.annotations || []),
  }};
}}

async function saveCurrentAsFigure() {{
  const nameInput = $('fig-name');
  const name = (nameInput.value || '').trim();
  if (!name) {{ nameInput.focus(); return; }}
  const body = {{ name, ..._gatherCurrentState() }};
  const r = await fetch(API_BASE + '/api/figures', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(body),
  }});
  if (!r.ok) {{
    alert('Save figure failed: ' + r.status);
    return;
  }}
  nameInput.value = '';
  refreshFiguresList();
}}

function _loadFigureIntoEditor(fig, opts) {{
  opts = opts || {{}};
  // Restore: source -> view -> camera -> selection -> styles -> layers
  // Confirm before clobbering current state -- it's destructive and
  // there's no undo.  Skip the prompt when ``opts.skipConfirm`` (the
  // user JUST clicked into this figure via the project workspace, no
  // ambiguity about intent), or if the editor is in a "fresh" state.
  if (!opts.skipConfirm) {{
    const curSt = getState(fileSel.value, viewSel.value);
    const curStyles = loadPartStyles(fileSel.value) || {{}};
    const hasWork = (curSt.highlights && curSt.highlights.size > 0)
                 || Object.keys(curStyles).length > 0;
    if (hasWork) {{
      if (!confirm(`Loading "${{fig.name}}" will replace the current `
                  + `selection and applied styles.  Continue?`)) return;
    }}
  }}

  // If the figure's source isn't in the dropdown yet (e.g. a dynamic
  // Onshape import that landed AFTER the page loaded), pull /api/sources
  // and add an option for it.  Otherwise the value assignment below
  // silently no-ops and the user keeps staring at the wrong assembly.
  if (fig.source_id) {{
    const hasOpt = Array.from(fileSel.options)
                         .some(o => o.value === fig.source_id);
    if (!hasOpt) {{
      const opt = document.createElement('option');
      opt.value = fig.source_id;
      opt.textContent = fig.source_id;   // best-effort label until we
                                          // hear back from /api/sources
      fileSel.appendChild(opt);
      // Fire off a label lookup so the option shows the human name
      fetch(API_BASE + '/api/sources').then(r => r.json()).then(data => {{
        const s = (data.sources || []).find(x => x.id === fig.source_id);
        if (s && s.label) opt.textContent = s.label;
      }}).catch(() => {{}});
    }}
  }}

  if (fig.source_id && fig.source_id !== fileSel.value) {{
    fileSel.value = fig.source_id;
    fileSel.dispatchEvent(new Event('change'));
  }}
  // Only switch view if the figure's view actually exists for this
  // source.  An unknown id (e.g. saved __live__ from a previous
  // session that's since been wiped) would blank the canvas.
  if (fig.view_id && fig.view_id !== viewSel.value) {{
    const valid = Array.from(viewSel.options)
                       .some(o => o.value === fig.view_id);
    if (valid) {{
      viewSel.value = fig.view_id;
      viewSel.dispatchEvent(new Event('change'));
    }} else {{
      console.warn(`figure ${{fig.name}}: view_id ${{fig.view_id}} `
                  + `not available on source ${{fig.source_id}}; `
                  + `keeping current view`);
    }}
  }}
  if (fig.camera && fig.camera.eye && fig.camera.target) {{
    window.IFU_VIEWER?.snapCameraTo?.(fig.camera.eye, fig.camera.target);
    if (fig.camera.up_axis && upAxisSel) {{
      upAxisSel.value = fig.camera.up_axis;
      upAxisSel.dispatchEvent(new Event('change'));
    }}
  }}
  const st = getState(fig.source_id, fig.view_id || viewSel.value);
  st.highlights = new Set(fig.selection || []);
  // Only overwrite styles when the figure has some to apply -- never
  // wipe the user's current per-part styling for an empty figure.
  if (fig.styles_per_part && Object.keys(fig.styles_per_part).length > 0) {{
    persistPartStyles(fig.source_id, fig.styles_per_part);
  }}
  if (fig.layers_on) {{
    document.querySelectorAll('input[data-layer]').forEach(cb => {{
      const want = fig.layers_on[cb.dataset.layer];
      if (typeof want === 'boolean' && cb.checked !== want) {{
        cb.checked = want;
        cb.dispatchEvent(new Event('change'));
      }}
    }});
  }}
  // Refresh everything that responds to those changes
  applyStyleSheet();
  applyHighlights();

  // Auto-render the 2D base view for figures that came from a View
  // (i.e. they carry a camera).  Without this the user lands in the
  // editor, sees the 3D pane, and has to click "generate 2D" before
  // they can start highlighting -- not what you want for a new figure
  // that's supposed to inherit a parent View's drawing.
  if (opts.autoGenerate && fig.camera && fig.camera.eye && fig.camera.target
      && typeof generateLiveSVGForCamera === 'function') {{
    // Show the spinner BEFORE the delay so the user sees something
    // is happening while three.js settles and the render is in
    // flight.  generateLiveSVGForCamera hides it on success.
    if (typeof showCanvasLoading === 'function') {{
      showCanvasLoading('rendering view...');
    }}
    // Let three.js apply the snapped camera + the up_axis change
    // before the render fires, so the projector axes match what the
    // 3D pane is actually showing.
    setTimeout(() => {{
      try {{ generateLiveSVGForCamera(fig.camera); }}
      catch (_e) {{
        if (typeof hideCanvasLoading === 'function') hideCanvasLoading();
      }}
    }}, 350);
  }}
}}

// Expose for tests + ad-hoc debugging
window._loadFigureIntoEditor = _loadFigureIntoEditor;

// ---- Loading overlay -----------------------------------------------
// Big spinner that floats over the 2D canvas while a render is in
// flight or a variant switch is loading.  The user reported that
// clicking a variant card felt like nothing was happening -- this is
// the missing feedback.
function _ensureLoadingOverlayStyles() {{
  if (document.getElementById('_loading_overlay_styles')) return;
  const s = document.createElement('style');
  s.id = '_loading_overlay_styles';
  s.textContent = `
    .canvas-loading-overlay {{
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
    }}
    .canvas-loading-overlay.is-hidden {{
      opacity: 0; pointer-events: none;
    }}
    .canvas-loading-spinner {{
      width: 36px; height: 36px;
      border: 3px solid #d4d4d8;
      border-top-color: var(--c-accora, #00836a);
      border-radius: 50%;
      animation: canvas-spinner-rot 0.9s linear infinite;
    }}
    .canvas-loading-label {{
      font-size: 12px; font-weight: 500;
    }}
    @keyframes canvas-spinner-rot {{ to {{ transform: rotate(360deg); }} }}
  `;
  document.head.appendChild(s);
}}

function showCanvasLoading(label) {{
  _ensureLoadingOverlayStyles();
  const wrap = document.getElementById('canvas-wrap');
  if (!wrap) return;
  // Make sure the container is positioned so absolute children land
  // over it -- canvas-wrap is already position:relative.
  let ov = wrap.querySelector(':scope > .canvas-loading-overlay');
  if (!ov) {{
    ov = document.createElement('div');
    ov.className = 'canvas-loading-overlay';
    ov.innerHTML = '<div class="canvas-loading-spinner"></div>'
                 + '<div class="canvas-loading-label"></div>';
    wrap.appendChild(ov);
  }}
  ov.classList.remove('is-hidden');
  ov.querySelector('.canvas-loading-label').textContent = label || 'loading...';
}}

function hideCanvasLoading() {{
  const wrap = document.getElementById('canvas-wrap');
  if (!wrap) return;
  const ov = wrap.querySelector(':scope > .canvas-loading-overlay');
  if (ov) ov.classList.add('is-hidden');
}}
window.showCanvasLoading = showCanvasLoading;
window.hideCanvasLoading = hideCanvasLoading;


// ---- Variant strip (subview mode) ----------------------------------
// Render a vertical strip of small cards, one per figure attached to
// the active View, with thumbnails.  The currently-open figure is
// marked is-active.  A leading "+" card creates a fresh figure under
// the same view (inherits view's camera + source).  Switching cards
// is a route navigation -- auto-save handles persisting the previous
// figure's edits before the swap.
async function _renderVariantStrip(projId, viewId, activeFigId) {{
  const strip = document.getElementById('variants-strip');
  if (!strip) return;
  strip.innerHTML = '';

  // The "+" add-card always sits at the top
  const addCard = document.createElement('div');
  addCard.className = 'variant-card add';
  addCard.textContent = '+ new highlight variant';
  addCard.addEventListener('click', async () => {{
    // Build a fresh figure name from the variant count
    let view, figs;
    try {{
      view = await (await fetch(API_BASE + '/api/views/'
                                  + encodeURIComponent(viewId))).json();
      figs = await (await fetch(API_BASE + '/api/views/'
                                  + encodeURIComponent(viewId)
                                  + '/figures')).json();
    }} catch (_e) {{
      toast('Failed to load view', 'error');
      return;
    }}
    const nextN = ((figs.figures || []).length) + 1;
    const defaultName = 'Variant ' + nextN;
    try {{
      const r = await fetch(API_BASE + '/api/figures', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          name: defaultName,
          source_id: view.source_id,
          project_id: projId,
          view_id: viewId,
          camera: view.camera,
          configuration: view.configuration,
        }}),
      }});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const f = await r.json();
      await fetch(API_BASE + '/api/views/' + encodeURIComponent(viewId)
                    + '/figures/' + encodeURIComponent(f.id),
                    {{ method: 'POST' }});
      // Visual feedback while the new variant loads
      if (typeof showCanvasLoading === 'function') {{
        showCanvasLoading('creating ' + defaultName + '...');
      }}
      // Hop to the new variant -- editor will auto-render the view
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(f.id);
    }} catch (e) {{
      if (typeof hideCanvasLoading === 'function') hideCanvasLoading();
      toast('Create failed: ' + (e.message || e), 'error');
    }}
  }});
  strip.appendChild(addCard);

  // Fetch the figures under this view
  let figs = [];
  try {{
    const r = await fetch(API_BASE + '/api/views/'
                            + encodeURIComponent(viewId) + '/figures');
    if (r.ok) figs = (await r.json()).figures || [];
  }} catch (_e) {{}}

  for (const f of figs) {{
    const card = document.createElement('div');
    card.className = 'variant-card'
                   + (f.id === activeFigId ? ' is-active' : '');
    const img = document.createElement('img');
    img.className = 'variant-thumb';
    img.src = API_BASE + '/api/figures/' + encodeURIComponent(f.id)
              + '/thumbnail?v=' + encodeURIComponent(f.updated_at || '');
    img.alt = '';
    img.onerror = () => {{
      const ph = document.createElement('div');
      ph.className = 'variant-thumb placeholder';
      img.replaceWith(ph);
    }};
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
    card.addEventListener('click', () => {{
      if (f.id === activeFigId) return;   // already on this variant
      // Visual feedback: empty the SVG pane immediately and show the
      // spinner so the user doesn't stare at a stale variant while
      // the new one's render is in flight.
      const livePane = document.querySelector(
        '.svg-pane[data-file="' + (fileSel?.value || '') + '"][data-view="__live__"]');
      if (livePane) livePane.innerHTML = '';
      if (typeof showCanvasLoading === 'function') {{
        showCanvasLoading('loading ' + (f.name || 'variant') + '...');
      }}
      location.hash = '#/project/' + encodeURIComponent(projId)
                    + '/view/' + encodeURIComponent(viewId)
                    + '/figure/' + encodeURIComponent(f.id);
    }});
    strip.appendChild(card);
  }}
}}
window._renderVariantStrip = _renderVariantStrip;

async function refreshFiguresList() {{
  if (!figuresList) return;
  const figs = await listFigures();
  figuresList.innerHTML = '';
  if (!figs.length) {{
    figuresList.innerHTML = '<li style="color:var(--muted); font-style:italic; padding:4px 0;">no figures yet</li>';
    return;
  }}
  for (const fig of figs) {{
    const li = document.createElement('li');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'name';
    nameSpan.dataset.figId = fig.id;
    nameSpan.textContent = fig.name;
    nameSpan.title = `Source: ${{fig.source_id}}  -  ${{fig.view_id}}\\n` +
                      `Updated: ${{fig.updated_at || '?'}}\\n` +
                      `${{(fig.selection || []).length}} parts selected`;
    nameSpan.addEventListener('click', () => _loadFigureIntoEditor(fig));
    li.appendChild(nameSpan);

    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.title = 'Delete this figure (cannot be undone)';
    delBtn.style.color = '#c44';
    delBtn.addEventListener('click', async (e) => {{
      e.stopPropagation();
      if (!confirm(`Delete figure "${{fig.name}}"?`)) return;
      await fetch(API_BASE + '/api/figures/' + encodeURIComponent(fig.id),
                   {{ method: 'DELETE' }});
      refreshFiguresList();
    }});
    li.appendChild(delBtn);
    figuresList.appendChild(li);
  }}
}}

$('btn-fig-save').addEventListener('click', saveCurrentAsFigure);
$('fig-name').addEventListener('keydown', (e) => {{
  if (e.key === 'Enter') saveCurrentAsFigure();
}});
$('btn-fig-save-as').addEventListener('click', () =>
  saveCurrentAsFigure({{ forceNew: true }}));

// ---- Dirty-state indicator -----------------------------------------
// When a figure is loaded (via /#/project/.../figure/<fid>) we track
// the state we loaded and compare to the live state.  Drift = unsaved
// changes.  The status line under the save button surfaces this so
// the user knows when to hit save.
let _loadedFigureBaseline = null;     // JSON snapshot at load-time
let _lastSavedAt = null;              // ISO time string

function _stateSig() {{
  // Cheap hash of the parts of state we persist.  JSON.stringify is
  // fine here -- the keys are small dicts / short arrays.
  try {{
    const s = _gatherCurrentState();
    // Don't compare layers when the cb wiring hasn't booted yet
    return JSON.stringify({{
      source_id: s.source_id,
      view_id:   s.view_id,
      camera:    s.camera,
      selection: (s.selection || []).slice().sort((a, b) => a - b),
      styles:    s.styles_per_part || {{}},
      layers:    s.layers_on || {{}},
      annot:     (s.annotations || []).length,
    }});
  }} catch (_e) {{ return ''; }}
}}

function _markLoadedFigureBaseline() {{
  // Capture the moment after _loadFigureIntoEditor finishes restoring
  // the figure -- the editor's state IS the loaded figure now, so
  // dirty=false until the user touches something.
  setTimeout(() => {{ _loadedFigureBaseline = _stateSig(); }}, 800);
}}

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

async function _autoSaveFire() {{
  _autoSaveTimer = null;
  if (!_autoSaveOn || _autoSaveInFlight) return;
  if (!AppState.currentFigureId) return;
  if (_loadedFigureBaseline == null) return;
  const sig = _stateSig();
  if (sig === _loadedFigureBaseline) return;
  _autoSaveInFlight = true;
  try {{
    await saveCurrentAsFigure({{ silent: true }});
  }} finally {{
    _autoSaveInFlight = false;
  }}
}}

function _updateSaveStatus() {{
  const el = document.getElementById('fig-save-status');
  if (!el) return;
  const inFigure = (typeof AppState !== 'undefined')
                     && !!AppState.currentFigureId;
  if (!inFigure) {{
    el.style.display = 'none';
    return;
  }}
  el.style.display = 'block';
  if (_autoSaveInFlight) {{
    el.textContent = 'saving...';
    el.style.color = 'var(--accora-teal)';
    return;
  }}
  const sig = _stateSig();
  const dirty = _loadedFigureBaseline != null
                && sig !== _loadedFigureBaseline;
  if (dirty) {{
    el.textContent = _autoSaveOn
      ? '● unsaved changes (auto-save in '
        + Math.ceil(_AUTO_SAVE_DELAY_MS / 1000) + 's)'
      : '● unsaved changes';
    el.style.color = '#b54708';     // amber
    // Schedule / refresh the auto-save debounce so we save once after
    // the user stops changing things.  If the dirty signature is the
    // same as last tick, leave the existing timer running.
    if (_autoSaveOn && sig !== _autoSaveLastDirtySig) {{
      _autoSaveLastDirtySig = sig;
      if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
      _autoSaveTimer = setTimeout(_autoSaveFire, _AUTO_SAVE_DELAY_MS);
    }}
  }} else if (_lastSavedAt) {{
    el.textContent = 'saved ' + _humanAgo(_lastSavedAt);
    el.style.color = 'var(--muted)';
    _autoSaveLastDirtySig = null;
  }} else {{
    el.textContent = 'loaded - no changes yet';
    el.style.color = 'var(--muted)';
    _autoSaveLastDirtySig = null;
  }}
}}

function _humanAgo(iso) {{
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 5000) return 'just now';
  if (ms < 60000) return Math.floor(ms / 1000) + 's ago';
  if (ms < 3600000) return Math.floor(ms / 60000) + 'm ago';
  return Math.floor(ms / 3600000) + 'h ago';
}}

// Poll for state drift.  1s is fine -- the indicator doesn't need to
// react instantly, and we want to keep this cheap.
setInterval(_updateSaveStatus, 1000);

// Expose so EditorScreen / saveCurrentAsFigure can poke them
window._markLoadedFigureBaseline = _markLoadedFigureBaseline;
window._setLastSavedAt = (iso) => {{
  _lastSavedAt = iso || new Date().toISOString();
  // Reset baseline to the just-saved state so the indicator flips
  // back to "saved Xs ago" right away.
  _loadedFigureBaseline = _stateSig();
  _updateSaveStatus();
}};

// ---- Thumbnail capture ---------------------------------------------
// Rasterize the currently-active SVG pane into a small PNG and PUT
// it to /api/figures/<fid>/thumbnail.  The Project workspace cards
// use this as their preview image.  Fire-and-forget: the figure's
// save path still completes if thumbnail capture fails.
async function _captureFigureThumbnail() {{
  const pane = typeof activePane === 'function' ? activePane() : null;
  const svg = pane?.querySelector('svg');
  if (!svg) return null;
  // Strip any pan/zoom view-transform group temporarily so the
  // thumbnail captures the WHOLE figure, not whatever the user
  // happens to have panned to.
  const viewG = svg.querySelector(':scope > g.view-transform');
  const prevTransform = viewG?.getAttribute('transform');
  if (viewG) viewG.removeAttribute('transform');
  let outDataUrl = null;
  try {{
    const xml = new XMLSerializer().serializeToString(svg);
    const blob = new Blob([xml], {{ type: 'image/svg+xml;charset=utf-8' }});
    const url = URL.createObjectURL(blob);
    try {{
      const img = await new Promise((res, rej) => {{
        const i = new Image();
        i.onload = () => res(i);
        i.onerror = rej;
        i.src = url;
      }});
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
      if (ar > W / H) {{ dh = W / ar; dy = (H - dh) / 2; }}
      else {{ dw = H * ar; dx = (W - dw) / 2; }}
      ctx.drawImage(img, dx, dy, dw, dh);
      outDataUrl = canvas.toDataURL('image/png');
    }} finally {{
      URL.revokeObjectURL(url);
    }}
  }} catch (e) {{
    console.warn('[thumbnail] capture failed:', e?.message || e);
    outDataUrl = null;
  }}
  if (viewG && prevTransform !== null) {{
    viewG.setAttribute('transform', prevTransform);
  }}
  return outDataUrl;
}}

// Debounce wrapper -- many auto-saves can fire close together.  We
// only want one thumbnail PUT per "burst".
let _thumbTimer = null;
function _scheduleThumbnailUpload(figId) {{
  if (!figId) return;
  if (_thumbTimer) clearTimeout(_thumbTimer);
  _thumbTimer = setTimeout(async () => {{
    _thumbTimer = null;
    const durl = await _captureFigureThumbnail();
    if (!durl) return;
    try {{
      await fetch(API_BASE + '/api/figures/'
                    + encodeURIComponent(figId) + '/thumbnail',
                    {{ method: 'PUT',
                       headers: {{ 'Content-Type': 'application/json' }},
                       body: JSON.stringify({{ data_url: durl }}) }});
    }} catch (e) {{
      console.warn('[thumbnail] upload failed:', e?.message || e);
    }}
  }}, 800);
}}
window._scheduleThumbnailUpload = _scheduleThumbnailUpload;
// Initial load (once the server probe says we're online)
// probeServer fires its .then before this; refresh manually after a beat.
setTimeout(refreshFiguresList, 1500);

// --- Projects (Phase B) ----------------------------------------------
const projectSel = document.getElementById('project-sel');

async function listProjects() {{
  if (typeof API_BASE !== 'string') return [];
  try {{
    const r = await fetch(API_BASE + '/api/projects');
    if (!r.ok) return [];
    return (await r.json()).projects || [];
  }} catch (_e) {{ return []; }}
}}

async function refreshProjectsList() {{
  if (!projectSel) return;
  const projs = await listProjects();
  const current = projectSel.value;
  projectSel.innerHTML = '<option value="">— All figures —</option>'
    + '<option value="__orphans__">  (Unfiled)</option>';
  for (const p of projs) {{
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    if (p.id === current) opt.selected = true;
    projectSel.appendChild(opt);
  }}
  // If the previously-selected project was deleted, reset to All
  if (current && current !== '__orphans__'
      && !projs.some(p => p.id === current)) {{
    projectSel.value = '';
  }}
}}

// Override figures list to filter by currently-selected project
const _origRefreshFiguresList = refreshFiguresList;
refreshFiguresList = async function() {{
  if (!figuresList) return;
  const pid = projectSel?.value || '';
  let figs;
  if (pid === '__orphans__') {{
    try {{
      const r = await fetch(API_BASE + '/api/figures/orphans');
      figs = r.ok ? (await r.json()).figures || [] : [];
    }} catch (_e) {{ figs = []; }}
  }} else if (pid) {{
    try {{
      const r = await fetch(API_BASE + '/api/projects/'
                              + encodeURIComponent(pid) + '/figures');
      figs = r.ok ? (await r.json()).figures || [] : [];
    }} catch (_e) {{ figs = []; }}
  }} else {{
    figs = await listFigures();
  }}
  figuresList.innerHTML = '';
  if (!figs.length) {{
    figuresList.innerHTML = '<li style="color:var(--muted); font-style:italic; padding:4px 0;">no figures here</li>';
    return;
  }}
  for (const fig of figs) {{
    const li = document.createElement('li');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'name';
    nameSpan.dataset.figId = fig.id;
    nameSpan.textContent = fig.name;
    nameSpan.title = `Source: ${{fig.source_id}}  -  ${{fig.view_id}}\\n` +
                      `Updated: ${{fig.updated_at || '?'}}\\n` +
                      `${{(fig.selection || []).length}} parts selected`;
    nameSpan.addEventListener('click', () => _loadFigureIntoEditor(fig));
    li.appendChild(nameSpan);

    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.title = 'Delete this figure (cannot be undone)';
    delBtn.style.color = '#c44';
    delBtn.addEventListener('click', async (e) => {{
      e.stopPropagation();
      if (!confirm(`Delete figure "${{fig.name}}"?`)) return;
      await fetch(API_BASE + '/api/figures/' + encodeURIComponent(fig.id),
                   {{ method: 'DELETE' }});
      refreshFiguresList();
    }});
    li.appendChild(delBtn);
    figuresList.appendChild(li);
  }}
}};

// Override save so it sets project_id from the current selection.
// Also: if the user navigated in via /#/project/<pid>/figure/<fid>,
// "save" should UPDATE that figure (PUT) rather than create a new
// one (POST).  The previous behaviour silently spammed duplicate
// figures whenever you tweaked styles and pressed save.
const _origSaveCurrentAsFigure = saveCurrentAsFigure;
saveCurrentAsFigure = async function(opts) {{
  opts = opts || {{}};
  const nameInput = $('fig-name');
  // Bind: if an existing figure is loaded (via figure route),
  //   default to that name and UPDATE in place.
  // Free-form: caller passed forceNew, OR no figure is loaded ->
  //   require a name from the input and POST a new figure.
  const loadedFigId = (typeof AppState !== 'undefined')
                       ? AppState.currentFigureId : null;
  const updatingExisting = !!loadedFigId && !opts.forceNew;

  let name = (nameInput.value || '').trim();
  if (!name && updatingExisting) {{
    // Look up the figure's stored name so a save with an empty
    // input doesn't blank the name field.
    try {{
      const r0 = await fetch(API_BASE + '/api/figures/'
                              + encodeURIComponent(loadedFigId));
      if (r0.ok) name = (await r0.json()).name || '';
    }} catch (_e) {{}}
  }}
  if (!name) {{ nameInput.focus(); return; }}

  const body = {{ name, ..._gatherCurrentState() }};
  const pid = projectSel?.value || '';
  if (pid && pid !== '__orphans__') body.project_id = pid;

  let url, method;
  if (updatingExisting) {{
    url = API_BASE + '/api/figures/' + encodeURIComponent(loadedFigId);
    method = 'PUT';
    body.id = loadedFigId;
  }} else {{
    url = API_BASE + '/api/figures';
    method = 'POST';
  }}
  let r;
  try {{
    r = await fetch(url, {{
      method,
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
  }} catch (e) {{
    (window.IFU_UI?.toast || function(){{}})(
      'Save failed: ' + (e.message || e), 'error');
    return;
  }}
  if (!r.ok) {{
    (window.IFU_UI?.toast || function(){{}})(
      'Save failed: HTTP ' + r.status, 'error');
    return;
  }}
  if (updatingExisting) {{
    if (!opts.silent) {{
      (window.IFU_UI?.toast || function(){{}})(
        'Saved \"' + name + '\"', 'success');
    }}
    // Update the breadcrumb in case the name changed
    const crumb = document.querySelector('#editor-breadcrumb .current');
    if (crumb) crumb.textContent = name;
    if (window._setLastSavedAt) window._setLastSavedAt();
    // Re-capture the thumbnail so workspace cards stay in sync with
    // whatever the user has been styling.  Fire-and-forget.
    if (window._scheduleThumbnailUpload) {{
      window._scheduleThumbnailUpload(loadedFigId);
    }}
  }} else {{
    (window.IFU_UI?.toast || function(){{}})(
      'Created \"' + name + '\"', 'success');
    nameInput.value = '';
    refreshFiguresList();
    // If we just forked, hop into the new figure so subsequent
    // saves update it.  /api/figures returns the new record.
    if (opts.forceNew) {{
      try {{
        const fig = await r.json();
        if (fig && fig.id && pid) {{
          location.hash = '#/project/' + encodeURIComponent(pid)
                          + '/figure/' + encodeURIComponent(fig.id);
        }}
      }} catch (_e) {{}}
    }}
  }}
}};

$('btn-project-new').addEventListener('click', async () => {{
  const name = prompt('Project name:');
  if (!name) return;
  const r = await fetch(API_BASE + '/api/projects', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ name }}),
  }});
  if (!r.ok) {{ alert('Create project failed: ' + r.status); return; }}
  const proj = await r.json();
  await refreshProjectsList();
  projectSel.value = proj.id;
  refreshFiguresList();
}});

$('btn-project-del').addEventListener('click', async () => {{
  const pid = projectSel.value;
  if (!pid || pid === '__orphans__') {{
    alert('Pick a project first');
    return;
  }}
  if (!confirm('Delete project? Its figures will become Unfiled.')) return;
  await fetch(API_BASE + '/api/projects/' + encodeURIComponent(pid),
               {{ method: 'DELETE' }});
  projectSel.value = '';
  await refreshProjectsList();
  refreshFiguresList();
}});

projectSel.addEventListener('change', refreshFiguresList);

setTimeout(refreshProjectsList, 1200);

// --- Revisions (Phase C) ---------------------------------------------
// Per-figure revision-status badge.  The figure JSON carries an
// optional ``bound_revision`` (set at save time -- Phase D for full
// wiring); the server computes "versions behind" by comparing the
// bound id to the cached Versions list.

const revsStatus = document.getElementById('revs-status');

async function refreshVersionsForActiveSource() {{
  const fid = fileSel.value;
  if (!fid) return;
  if (revsStatus) revsStatus.textContent = '…';
  try {{
    const r = await fetch(API_BASE + '/api/sources/'
                           + encodeURIComponent(fid)
                           + '/versions/refresh', {{ method: 'POST' }});
    if (!r.ok) {{
      const err = await r.json().catch(() => ({{}}));
      if (revsStatus) revsStatus.textContent =
        '✗ ' + (err.error || ('HTTP ' + r.status));
      return;
    }}
    const env = await r.json();
    const n = (env.versions || []).length;
    if (revsStatus) revsStatus.textContent =
      `✓ ${{n}} versions cached`;
    // Re-render figures list to update badges
    refreshFiguresList();
  }} catch (e) {{
    if (revsStatus) revsStatus.textContent = '✗ ' + (e.message || 'failed');
  }}
}}

$('btn-revs-refresh').addEventListener('click', refreshVersionsForActiveSource);

// Browser-prompt-based version picker.  Lists the cached Versions
// newest-first and asks the user which one to bind to.  Single-user
// local tool -- not worth a fancy modal yet.
async function _promptBindRevision(figureId, versions) {{
  if (!versions || !versions.length) {{
    alert('No cached Versions for this source. Refresh first.');
    return;
  }}
  const lines = versions.map((v, i) =>
    `${{i + 1}}. ${{v.name || '?'}}  (${{(v.created_at || '').slice(0, 10)}})`
  ).join('\\n');
  const pick = prompt(
    'Bind to which Version?\\n\\n' + lines + '\\n\\nEnter number (1-'
      + versions.length + ') or blank to cancel:');
  if (!pick) return;
  const idx = parseInt(pick) - 1;
  if (Number.isNaN(idx) || idx < 0 || idx >= versions.length) {{
    alert('Invalid choice.');
    return;
  }}
  const target = versions[idx];
  const r = await fetch(API_BASE + '/api/figures/'
                          + encodeURIComponent(figureId) + '/bind_revision', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ version_id: target.id }}),
  }});
  if (!r.ok) {{
    const err = await r.json().catch(() => ({{}}));
    alert('Bind failed: ' + (err.error || ('HTTP ' + r.status)));
    return;
  }}
  refreshFiguresList();
}}

// Re-render the figures list with revision badges.  Wraps the prior
// refresh so we can stamp a small ⬆/✓ on each <li>.
const _refreshFiguresList_phaseC = refreshFiguresList;
refreshFiguresList = async function() {{
  await _refreshFiguresList_phaseC();
  // For each rendered li, fetch its revision_status and append a badge
  if (!figuresList) return;
  const lis = figuresList.querySelectorAll('li');
  for (const li of lis) {{
    const nameSpan = li.querySelector('.name');
    if (!nameSpan || !nameSpan.dataset.figId) continue;
    try {{
      const r = await fetch(API_BASE + '/api/figures/'
                              + encodeURIComponent(nameSpan.dataset.figId)
                              + '/revision_status');
      if (!r.ok) continue;
      const s = await r.json();
      const badge = document.createElement('span');
      badge.style.fontSize = '10px';
      badge.style.marginRight = '4px';
      if (s.versions_behind === null || s.versions_behind === undefined) {{
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
        badge.title = `Bind this figure to a Version. ${{versions.length}} cached.`;
        badge.addEventListener('click', (e) => {{
          e.stopPropagation();
          _promptBindRevision(s.figure_id, versions);
        }});
      }} else if (s.versions_behind === 0) {{
        badge.textContent = '✓';
        badge.style.color = '#0a8';
        badge.style.cursor = 'pointer';
        badge.title = 'Bound to the latest Version. Click to re-bind.';
        badge.addEventListener('click', async (e) => {{
          e.stopPropagation();
          const vresp = await fetch(API_BASE + '/api/sources/'
                                       + encodeURIComponent(s.source_id)
                                       + '/versions');
          const versions = (await vresp.json()).versions || [];
          _promptBindRevision(s.figure_id, versions);
        }});
      }} else {{
        badge.textContent = '⬆' + s.versions_behind;
        badge.style.color = '#c70';
        badge.style.cursor = 'pointer';
        badge.title = `Bound to ${{s.bound_revision?.name || '?'}}; latest is `
                    + `${{s.latest_revision?.name || '?'}}. Click to re-bind.`;
        badge.addEventListener('click', async (e) => {{
          e.stopPropagation();
          const vresp = await fetch(API_BASE + '/api/sources/'
                                       + encodeURIComponent(s.source_id)
                                       + '/versions');
          const versions = (await vresp.json()).versions || [];
          _promptBindRevision(s.figure_id, versions);
        }});
      }}
      li.insertBefore(badge, li.firstChild);
    }} catch (_e) {{}}
  }}
}};

// nameSpan.dataset.figId is set inside each list-builder so the
// phaseC wrapper above can look up revision status by id.

// --- Per-part styling ---------------------------------------------------
// Per-source dict of part_idx -> {{stroke, width, opacity, dash}}
// Persisted in localStorage, rebuilt into a <style> tag on every refresh
// so the rules apply to live + baked SVGs alike.

function _styleKey(fid) {{ return 'partStyles_' + fid; }}
function loadPartStyles(fid) {{
  try {{
    return JSON.parse(localStorage.getItem(_styleKey(fid)) || '{{}}');
  }} catch (_e) {{ return {{}}; }}
}}
function persistPartStyles(fid, m) {{
  localStorage.setItem(_styleKey(fid), JSON.stringify(m));
}}

// Persistent silhouette overlay: for each applied part, draw the SAME
// closed-loop polygon the live highlight uses (from footprint cache or
// fallback to outline_v / sharp_v paths), at the user's chosen stroke
// & fill.  Stays on screen across selections, layered just under the
// transient silhouette overlay so live selection still wins on top.
function renderPersistentSilhouettes() {{
  document.querySelectorAll('.svg-pane').forEach(pane => {{
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

    const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    layer.setAttribute('class', 'layer-persistent-silhouette');
    layer.setAttribute('pointer-events', 'none');

    for (const [idxStr, style] of Object.entries(m)) {{
      const idx = parseInt(idxStr);
      // Polygon source: prefer cached server footprint, fall back to
      // assembly-HLR outline_v + sharp_v paths from THIS pane's SVG.
      let subpaths = [];
      const fp = _getFootprint(fid, vid, idx);
      if (fp && fp.length) {{
        fp.forEach(pl => {{
          if (!pl || pl.length < 2) return;
          subpaths.push(
            'M ' + pl.map(p => p[0].toFixed(2) + ' ' + p[1].toFixed(2))
                    .join(' L ') + ' Z'
          );
        }});
      }} else {{
        const partCls = '.part-' + String(idx).padStart(3, '0');
        svg.querySelectorAll(
          '.layer-outline_v ' + partCls + ' path, '
          + '.layer-sharp_v ' + partCls + ' path'
        ).forEach(p => {{
          const d = (p.getAttribute('d') || '').trim();
          if (d) subpaths.push(d);
        }});
      }}
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
    }}
    // Place near the front: just before the transient silhouette layer
    // and click-hit layers so it draws on top of the line art.
    scaleG.appendChild(layer);
  }});
}}

function applyStyleSheet() {{
  const fid = fileSel.value;
  const m = loadPartStyles(fid);
  let css = '';
  for (const [idx, st] of Object.entries(m)) {{
    const sel = `.svg-pane[data-file="${{fid}}"] svg .part.part-${{String(idx).padStart(3, '0')}} path`;
    const rules = [];
    // No automatic stroke override -- the persistent silhouette layer
    // handles colour + width.  We still let opacity override here so
    // applied styles can also fade an individual part.
    if (st.opacity != null && st.opacity !== 1) {{
      rules.push(`opacity: ${{st.opacity}}`);
    }}
    if (rules.length) {{
      css += `${{sel}} {{ ${{rules.join('; ')}} !important; }}\n`;
    }}
  }}
  let styleEl = document.getElementById('per-part-styles');
  if (!styleEl) {{
    styleEl = document.createElement('style');
    styleEl.id = 'per-part-styles';
    document.head.appendChild(styleEl);
  }}
  styleEl.textContent = css;
  // Push to 3D pane too
  window.IFU_VIEWER?.applyPartStyles3D?.(m);
  // Re-render the persistent silhouette overlays and refresh the list
  renderPersistentSilhouettes();
  renderAppliedStylesList();
}}

$('sty-width').addEventListener('input', (e) => {{
  $('sty-width-val').textContent = parseFloat(e.target.value).toFixed(1);
}});
$('sty-opacity').addEventListener('input', (e) => {{
  $('sty-opacity-val').textContent = parseFloat(e.target.value).toFixed(2);
}});
$('sty-fill-opacity').addEventListener('input', (e) => {{
  $('sty-fill-opacity-val').textContent = parseFloat(e.target.value).toFixed(2);
  restyleSilhouetteOnly();
}});
// Style-control changes refresh ONLY the silhouette overlay -- we don't
// re-walk all 678 part nodes on every slider input.  rAF-coalesce so
// drag events get a single update per frame.
let _restylePending = false;
function restyleSilhouetteOnly() {{
  if (_restylePending) return;
  _restylePending = true;
  requestAnimationFrame(() => {{
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
  }});
}}
['sty-stroke', 'sty-width', 'sty-fill', 'sty-fill-on'].forEach(id => {{
  $(id).addEventListener('input', restyleSilhouetteOnly);
  $(id).addEventListener('change', restyleSilhouetteOnly);
}});

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
function _silCacheKey(fid, vid, idx) {{ return fid + '|' + vid + '|' + idx; }}
function _setTrueSil(fid, vid, idx, polys) {{
  _trueSilCache.set(_silCacheKey(fid, vid, idx), polys || []);
}}
function _getTrueSil(fid, vid, idx) {{
  return _trueSilCache.get(_silCacheKey(fid, vid, idx)) || null;
}}
// Visible-footprint cache (server-rasterized).  Keyed by (fid, vid, idx).
// Used for BOTH (a) the bold-edge closed loop tracing the part's actually
// visible 2D region, and (b) the click-anywhere hit area.
const _footprintCache = new Map();
function _fpKey(fid, vid, idx) {{ return fid + '|' + vid + '|' + idx; }}
function _setFootprint(fid, vid, idx, polys) {{
  _footprintCache.set(_fpKey(fid, vid, idx), polys || []);
}}
function _getFootprint(fid, vid, idx) {{
  return _footprintCache.get(_fpKey(fid, vid, idx)) || null;
}}
// Track which views we've already fetched the assembly raster for so we
// don't re-request when the user clicks more parts in the same view.
const _footprintViewFetched = new Set();
function _fpViewKey(fid, vid) {{ return fid + '|' + vid; }}

// Group-mode silhouette cache: keyed by (fid, vid, sorted-index-tuple)
const _groupSilCache = new Map();
function _groupKey(fid, vid, idxList) {{
  return fid + '|' + vid + '|' + idxList.slice().sort((a,b)=>a-b).join(',');
}}
function _setGroupSil(fid, vid, idxList, polys) {{
  _groupSilCache.set(_groupKey(fid, vid, idxList), polys || []);
}}
function _getGroupSil(fid, vid, idxList) {{
  return _groupSilCache.get(_groupKey(fid, vid, idxList)) || null;
}}

// Inject (or refresh) a filled silhouette + bold edge for every
// highlighted part.  Prefers the server-fetched TRUE silhouette (closed
// loops, no occlusion holes); falls back to the local outline_v
// polylines from the baked SVG when the server hasn't responded yet
// (or isn't running at all).
function applySilhouetteFill(svg, highlights, fillOn, fillColor, fillAlpha,
                              strokeColor, strokeWidth) {{
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
  if (fillOn) {{
    const fillSubpaths = [];
    const pushPolylines = (polys) => {{
      polys.forEach(pl => {{
        if (!pl || pl.length < 2) return;
        const d = 'M ' + pl.map(p => p[0].toFixed(2) + ' ' + p[1].toFixed(2))
                          .join(' L ') + ' Z';
        fillSubpaths.push(d);
      }});
    }};
    for (const idx of idxList) {{
      const fp = _getFootprint(fid, vid, idx);
      if (fp && fp.length) pushPolylines(fp);
    }}
    if (fillSubpaths.length) {{
      const fillPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      fillPath.setAttribute('d', fillSubpaths.join(' '));
      fillPath.setAttribute('fill', fillColor);
      fillPath.setAttribute('fill-opacity', String(fillAlpha));
      fillPath.setAttribute('fill-rule', 'evenodd');
      fillPath.setAttribute('stroke', 'none');
      layer.appendChild(fillPath);
    }}
  }}

  // ---- 2) BOLD EDGE stroke (always) -------------------------------
  // Prefer the rasterized FOOTPRINT polygon (one closed loop per
  // visible piece -- so a part occluded in 3 places gets 3 separate
  // bold loops, exactly what you'd expect).  PER-PART decision so a
  // single missing footprint doesn't fall every part back to open
  // polylines.
  const strokeSubpaths = [];
  const _fallbackIdx = [];
  let _withFp = 0;
  for (const idx of idxList) {{
    const fp = _getFootprint(fid, vid, idx);
    if (fp && fp.length) {{
      _withFp++;
      fp.forEach(pl => {{
        if (!pl || pl.length < 2) return;
        strokeSubpaths.push(
          'M ' + pl.map(p => p[0].toFixed(2) + ' ' + p[1].toFixed(2))
                  .join(' L ') + ' Z'
        );
      }});
    }} else {{
      _fallbackIdx.push(idx);
    }}
  }}
  // Open-polyline fallback ONLY for the parts that have no footprint yet
  // (still in flight or genuinely empty from server).  This is the
  // "partial outline" the user sees during the ~46s assembly raster on
  // first click of a heavy source -- the prefetch in injectLiveSVG
  // should make this window short.
  if (_fallbackIdx.length) {{
    if (console) {{
      console.log('[silhouette] ' + _withFp + ' parts using footprint, '
        + _fallbackIdx.length + ' falling back: ' + JSON.stringify(_fallbackIdx));
    }}
    for (const idx of _fallbackIdx) {{
      const partCls = '.part-' + String(idx).padStart(3, '0');
      svg.querySelectorAll(
        '.layer-outline_v ' + partCls + ' path, '
        + '.layer-sharp_v ' + partCls + ' path'
      ).forEach(pathEl => {{
        const d = (pathEl.getAttribute('d') || '').trim();
        if (d) strokeSubpaths.push(d);
      }});
    }}
  }} else if (_DBG_ON) {{
    console.log('[silhouette] all ' + _withFp + ' selected parts have '
      + 'closed-loop footprints');
  }}
  if (strokeSubpaths.length) {{
    const strokePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    strokePath.setAttribute('d', strokeSubpaths.join(' '));
    strokePath.setAttribute('fill', 'none');
    strokePath.setAttribute('stroke', strokeColor);
    strokePath.setAttribute('stroke-width', String(strokeWidth));
    strokePath.setAttribute('stroke-linejoin', 'round');
    strokePath.setAttribute('stroke-linecap', 'round');
    layer.appendChild(strokePath);
  }}

  // Sit BEHIND visible edge layers so the rest of the edges still draw
  // on top, but in front of the hidden layers.
  scaleG.insertBefore(layer, scaleG.firstChild);
}}

// Pre-fetch the visible-footprint polygons for EVERY part in the current
// view.  Server rasterizes the assembly once per view (~2-5s), then
// every per-part lookup is cached.  We then inject a transparent
// hit-fill layer so clicks land anywhere inside a part, not just on
// its edges.  Closed-loop bold stroke uses the same data.
// Fetch the visible-footprint polygon for ONLY the currently-selected
// parts.  First request in a view pays the full assembly raster
// cost (~5-30s depending on source) but the server caches every
// part's footprint after that, so further calls are instant.  This
// is what powers the bold-edge "broken into pieces" rendering.
async function fetchSelectedFootprints() {{
  const fid = fileSel.value, vid = viewSel.value;
  const st = getState(fid, vid);
  if (!st.highlights || !st.highlights.size) return;
  if (typeof API_BASE !== 'string') return;
  const apiBase = API_BASE;

  const missing = [];
  for (const idx of st.highlights) {{
    if (!_getFootprint(fid, vid, idx)) missing.push(idx);
  }}
  if (!missing.length) {{
    if (_DBG_ON) console.log('[footprint] no missing parts, skipping fetch');
    return;
  }}
  console.log('[footprint] fetching ' + missing.length + ' parts: '
              + JSON.stringify(missing) + ' for fid=' + fid + ' vid=' + vid);

  // Camera body (same logic as the other fetchers)
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  const body = {{ file_id: fid }};
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {{
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  }} else if (ve && ve.view_dir) {{
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  }} else {{
    console.warn('[footprint] no camera context for fid=' + fid + ' vid=' + vid
                  + ' -- bailing');
    return;
  }}
  body.part_indices = missing;
  console.log('[footprint] camera body:', JSON.stringify(body));
  try {{
    const r = await fetch(apiBase + '/api/part_footprints', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (fileSel.value !== fid || viewSel.value !== vid) return;
    let _empty = 0, _nonempty = 0;
    for (const [idxStr, polys] of Object.entries(data.polylines || {{}})) {{
      if (!polys || !polys.length) _empty++;
      else _nonempty++;
      _setFootprint(fid, vid, parseInt(idxStr), polys);
    }}
    console.log('[footprint] returned ' + _nonempty + ' parts with polys, '
                + _empty + ' empty.  Stats: '
                + JSON.stringify(data.stats || {{}}));
    applyHighlights();   // re-render bold edge with the new footprints
  }} catch (e) {{
    console.warn('[footprint] fetch failed:', e.message || e);
  }}
}}

async function prefetchFootprintsForCurrentView() {{
  const fid = fileSel.value, vid = viewSel.value;
  const vkey = _fpViewKey(fid, vid);
  if (_footprintViewFetched.has(vkey)) return;
  if (typeof API_BASE !== 'string') return;
  const apiBase = API_BASE;
  // Resolve camera body (same logic as fetchTrueSilhouettes)
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const ve = fe?.views.find(v => v.view_id === vid);
  const body = {{ file_id: fid }};
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {{
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  }} else if (ve && ve.view_dir) {{
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  }} else {{
    return;
  }}
  body.part_indices = fe.parts.map(p => p.idx);
  _footprintViewFetched.add(vkey);   // claim BEFORE the await
  try {{
    const r = await fetch(apiBase + '/api/part_footprints', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (fileSel.value !== fid || viewSel.value !== vid) return;   // stale
    for (const [idxStr, polys] of Object.entries(data.polylines || {{}})) {{
      _setFootprint(fid, vid, parseInt(idxStr), polys);
    }}
    injectHitFillLayer(fid, vid);
    applyHighlights();   // re-render bold stroke using footprints
  }} catch (e) {{
    console.warn('[footprint] prefetch failed:', e.message || e);
    _footprintViewFetched.delete(vkey);
  }}
}}

// Hit-fill click-anywhere layer was here -- removed because the
// rasterized footprints sometimes leak pixels into neighbour parts,
// which made clicks land on the wrong part.  Click targeting now goes
// through the existing 3mm-stroke hit layer (always present in the
// baked SVG); user clicks near any visible edge to select.  The
// FOOTPRINT data is still used for the bold-edge closed loop --
// that's read-only display, no click logic depends on it.
function injectHitFillLayer(_fid, _vid) {{ /* no-op (reverted) */ }}

// Andrew's monotone-chain convex hull on a list of (x, y) pairs.
function _convexHull(points) {{
  if (points.length < 3) return points.slice();
  const pts = points.slice().sort((a, b) =>
    a[0] === b[0] ? a[1] - b[1] : a[0] - b[0]);
  const cross = (o, a, b) =>
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [];
  for (const p of pts) {{
    while (lower.length >= 2 &&
           cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) {{
      lower.pop();
    }}
    lower.push(p);
  }}
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i--) {{
    const p = pts[i];
    while (upper.length >= 2 &&
           cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) {{
      upper.pop();
    }}
    upper.push(p);
  }}
  upper.pop();
  lower.pop();
  return lower.concat(upper);
}}

// Build a per-part convex-hull hit layer so clicks land anywhere inside
// a part, not just near its edges.  Hulls are computed from the visible
// polyline points in the baked SVG -- so each hull only contains THIS
// part's points, never a neighbour's.  Sorted biggest-first so small
// parts paint last and win clicks where their hulls overlap (e.g. a
// pivot pin sitting on top of a plate).
function injectHitHullsLayer() {{
  const svg = activeSvg();
  if (!svg) return;
  const scaleG = svg.querySelector('g[transform="scale(1,-1)"]')
              || svg.querySelector('.view-transform > g')
              || svg.querySelector(':scope > g');
  if (!scaleG) return;
  scaleG.querySelector(':scope > g.layer-hit-hull')?.remove();

  // Collect points per idx from outline_v + sharp_v + smooth_v
  const partPoints = new Map();
  ['.layer-outline_v', '.layer-sharp_v', '.layer-smooth_v'].forEach(sel => {{
    svg.querySelectorAll(sel + ' .part').forEach(partG => {{
      const idx = parseInt(partG.dataset.part);
      if (Number.isNaN(idx)) return;
      partG.querySelectorAll('path').forEach(p => {{
        const d = p.getAttribute('d') || '';
        const toks = d.match(/-?\d+(?:\.\d+)?/g);
        if (!toks) return;
        if (!partPoints.has(idx)) partPoints.set(idx, []);
        const arr = partPoints.get(idx);
        for (let i = 0; i + 1 < toks.length; i += 2) {{
          arr.push([parseFloat(toks[i]), parseFloat(toks[i + 1])]);
        }}
      }});
    }});
  }});

  // Per-idx hull + area for sort
  const hulls = [];
  const fe = CATALOGUE.find(x => x.file_id === fileSel.value);
  const labelOf = idx => fe?.parts.find(p => p.idx === idx)?.label || '';
  for (const [idx, pts] of partPoints) {{
    if (pts.length < 3) continue;
    const hull = _convexHull(pts);
    if (hull.length < 3) continue;
    let s = 0;
    for (let i = 0; i < hull.length; i++) {{
      const j = (i + 1) % hull.length;
      s += hull[i][0] * hull[j][1] - hull[j][0] * hull[i][1];
    }}
    hulls.push({{ idx, hull, area: Math.abs(s) * 0.5 }});
  }}
  hulls.sort((a, b) => b.area - a.area);

  const layer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  layer.setAttribute('class', 'layer-hit-hull');
  layer.setAttribute('fill', 'rgba(0,0,0,0)');
  layer.setAttribute('stroke', 'none');
  layer.setAttribute('pointer-events', 'fill');
  for (const e of hulls) {{
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
  }}
  // Append last so the hull layer is on top of every visible-edge layer
  // AND the 3mm stroke hit layer.  Filled hull catches the click
  // anywhere inside the convex hull of the part.
  scaleG.appendChild(layer);
}}

// Request true per-part silhouettes from the server for any highlighted
// parts we don't already have cached.  When the response arrives, the
// cache is populated and applyHighlights() is re-run to swap the local
// approximation for the closed-loop server polylines.
let _silFetchToken = 0;
async function fetchTrueSilhouettes() {{
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
  const body = {{ file_id: fid }};
  const liveCtx = window.IFU_VIEWER._getLiveCamCtx?.(fid);
  if (vid === '__live__' && liveCtx) {{
    body.eye = liveCtx.eye;
    body.target = liveCtx.target;
    if (liveCtx.up_axis) body.up_axis = liveCtx.up_axis;
  }} else if (ve && ve.view_dir) {{
    body.view_dir = ve.view_dir;
    body.focal = [0, 0, 0];
  }} else {{
    return;
  }}

  const groupOn = $('sty-group-mode')?.checked ?? false;
  const idxList = [...st.highlights];

  // GROUP REQUEST: when "outline as group" is on and 2+ parts are
  // selected, ask the server for a single compound silhouette.  Falls
  // back to per-part fetch below if disabled or single-select.
  if (groupOn && idxList.length >= 2) {{
    if (_getGroupSil(fid, vid, idxList)) {{
      applyHighlights();   // already cached -- just re-render
      return;
    }}
    const body2 = Object.assign({{}}, body);
    body2.part_indices = idxList;
    body2.group = true;
    const token = ++_silFetchToken;
    try {{
      const r = await fetch(apiBase + '/api/part_silhouettes', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body2),
      }});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      if (token !== _silFetchToken) return;
      if (fileSel.value !== fid || viewSel.value !== vid) return;
      _setGroupSil(fid, vid, idxList, data.polylines?.group || []);
      applyHighlights();
    }} catch (e) {{
      console.warn('[silhouette] group fetch failed:', e.message || e);
    }}
    return;
  }}

  // PER-PART REQUEST (single-select or group mode disabled).
  const missing = [];
  for (const idx of st.highlights) {{
    if (!_getTrueSil(fid, vid, idx)) missing.push(idx);
  }}
  if (!missing.length) return;
  body.part_indices = missing;

  const token = ++_silFetchToken;
  try {{
    const r = await fetch(apiBase + '/api/part_silhouettes', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (token !== _silFetchToken) return;
    if (fileSel.value !== fid || viewSel.value !== vid) return;
    for (const [idxStr, polys] of Object.entries(data.polylines || {{}})) {{
      _setTrueSil(fid, vid, parseInt(idxStr), polys);
    }}
    applyHighlights();   // re-render silhouette layer with the new data
  }} catch (e) {{
    console.warn('[silhouette] fetch failed:', e.message || e);
  }}
}}

// Invalidate cache + refetch when the view (camera) changes, since the
// (u,v) space differs per projection.
function _invalidateSilCache() {{
  _trueSilCache.clear();
  _groupSilCache.clear();
  _footprintCache.clear();
  _footprintViewFetched.clear();
}}
viewSel.addEventListener('change', () => {{
  _invalidateSilCache();
  // Silhouette fetch only fires if shade is on (guarded inside).
  setTimeout(fetchTrueSilhouettes, 0);
}});
fileSel.addEventListener('change', () => {{
  _invalidateSilCache();
}});
// Group-mode toggle: re-render immediately (uses cached data if any),
// then fetch the missing form (group vs per-part) on the side.
$('sty-group-mode')?.addEventListener('change', () => {{
  applyHighlights();
  setTimeout(fetchTrueSilhouettes, 0);
}});
// Turning shade ON triggers the closed-silhouette fetch (the fill needs
// a closed profile from server-side per-part HLR).
$('sty-fill-on')?.addEventListener('change', () => {{
  applyHighlights();
  if ($('sty-fill-on').checked) setTimeout(fetchTrueSilhouettes, 0);
}});
// Render the "Applied styles" list in the sidebar.  Each row shows a
// colour swatch + part label + width, plus inline "edit" (select that
// part + load its style into the controls) and "delete" (remove).
function renderAppliedStylesList() {{
  const listEl = document.getElementById('applied-styles-list');
  if (!listEl) return;
  const fid = fileSel.value;
  const m = loadPartStyles(fid);
  const fe = CATALOGUE.find(x => x.file_id === fid);
  const entries = Object.entries(m);
  if (!entries.length) {{
    listEl.innerHTML = '<li style="color: var(--muted); padding: 4px 0; font-style: italic;">none yet</li>';
    return;
  }}
  entries.sort((a, b) => parseInt(a[0]) - parseInt(b[0]));
  listEl.innerHTML = '';
  for (const [idxStr, style] of entries) {{
    const idx = parseInt(idxStr);
    const part = fe?.parts.find(p => p.idx === idx);
    const label = part ? part.label : ('part_' + idxStr);
    const li = document.createElement('li');
    li.style.cssText = 'display:flex; align-items:center; gap:6px; padding:3px 4px; '
      + 'border-radius:3px; cursor:pointer;';
    li.title = `part_${{idxStr}} - ${{label}}`;

    const swatch = document.createElement('span');
    swatch.style.cssText = 'display:inline-block; width:14px; height:10px; '
      + `background:${{style.fillOn ? style.fillColor : '#fff'}}; `
      + `border:2px solid ${{style.stroke || '#00836a'}};`;
    li.appendChild(swatch);

    const text = document.createElement('span');
    text.style.cssText = 'flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;';
    text.textContent = `[${{idxStr}}] ${{label}}`;
    li.appendChild(text);

    const wInfo = document.createElement('span');
    wInfo.style.cssText = 'color: var(--muted); font-size:10px;';
    wInfo.textContent = `${{(style.width ?? 3).toFixed(1)}}mm`;
    li.appendChild(wInfo);

    const editBtn = document.createElement('button');
    editBtn.textContent = '✎';
    editBtn.title = 'Select this part and load its style into the editor';
    editBtn.style.cssText = 'padding:0 5px; font-size:12px; line-height:1.4;';
    editBtn.addEventListener('click', (e) => {{
      e.stopPropagation();
      // Load the style into the controls so the user sees the values
      if (style.stroke) $('sty-stroke').value = style.stroke;
      if (style.width != null) {{
        $('sty-width').value = String(style.width);
        $('sty-width-val').textContent = style.width.toFixed(1);
      }}
      if (style.opacity != null) {{
        $('sty-opacity').value = String(style.opacity);
        $('sty-opacity-val').textContent = style.opacity.toFixed(2);
      }}
      if (style.dash != null) $('sty-dash').value = style.dash || '';
      if (style.fillOn != null) $('sty-fill-on').checked = !!style.fillOn;
      if (style.fillColor) $('sty-fill').value = style.fillColor;
      if (style.fillAlpha != null) {{
        $('sty-fill-opacity').value = String(style.fillAlpha);
        $('sty-fill-opacity-val').textContent = style.fillAlpha.toFixed(2);
      }}
      // Select that part
      togglePartHighlight(idx, {{append: false}});
    }});
    li.appendChild(editBtn);

    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.title = 'Remove this applied style';
    delBtn.style.cssText = 'padding:0 5px; font-size:12px; line-height:1.4; color:#c44;';
    delBtn.addEventListener('click', (e) => {{
      e.stopPropagation();
      const m2 = loadPartStyles(fid);
      delete m2[idxStr];
      persistPartStyles(fid, m2);
      applyStyleSheet();
    }});
    li.appendChild(delBtn);

    // Whole-row click also selects the part (without loading style)
    li.addEventListener('click', () => togglePartHighlight(idx, {{append: false}}));
    li.addEventListener('mouseenter', () => li.style.background = '#eef4f2');
    li.addEventListener('mouseleave', () => li.style.background = '');

    listEl.appendChild(li);
  }}
}}

// ---- Preset styles (project mode) ----------------------------------
// Five fixed IFU presets so figures across a project look consistent.
// Each preset packages stroke + width + fill so one click applies a
// fully-specified style to the current selection.  No color pickers,
// no sliders -- pick a vocabulary and stick to it.
const _STYLE_PRESETS = [
  {{ id: 'highlight', label: 'Highlight',
     style: {{ stroke: '#00836a', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: true, fillColor: '#cce6e0', fillAlpha: 0.35 }} }},
  {{ id: 'caution',   label: 'Caution',
     style: {{ stroke: '#b54708', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: true, fillColor: '#fff3e0', fillAlpha: 0.40 }} }},
  {{ id: 'info',      label: 'Info',
     style: {{ stroke: '#1e6fa1', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: true, fillColor: '#e0f0fa', fillAlpha: 0.40 }} }},
  {{ id: 'outline',   label: 'Outline only',
     style: {{ stroke: '#00836a', width: 4.0, opacity: 1.0, dash: null,
                 fillOn: false }} }},
  {{ id: 'subtle',    label: 'Subtle',
     style: {{ stroke: '#52525b', width: 2.0, opacity: 0.85, dash: null,
                 fillOn: false }} }},
];

function _stylesMatch(a, b) {{
  if (!a || !b) return false;
  // Loose equality on the fields that visually matter
  return a.stroke === b.stroke
      && Math.abs((a.width || 0) - (b.width || 0)) < 0.01
      && !!a.fillOn === !!b.fillOn
      && (!a.fillOn || (a.fillColor === b.fillColor
                         && Math.abs((a.fillAlpha || 0)
                                       - (b.fillAlpha || 0)) < 0.01));
}}

function _renderPresetRow() {{
  const row = document.getElementById('preset-row');
  const actions = document.getElementById('preset-actions');
  if (!row || row.children.length) return;   // already built
  for (const p of _STYLE_PRESETS) {{
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
  }}
  if (actions) actions.style.display = '';
}}

function _applyPreset(preset) {{
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) {{
    (window.IFU_UI?.toast || function(){{}})(
      'Select one or more parts first', 'error');
    return;
  }}
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) m[idx] = {{ ...preset.style }};
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
  // Mark active preset visually
  document.querySelectorAll('#preset-row .preset-btn').forEach(b => {{
    b.classList.toggle('is-active', b.dataset.presetId === preset.id);
  }});
}}

function _refreshPresetActiveState() {{
  // After a selection change, light up the preset that matches the
  // applied style of (any of) the selected parts -- otherwise clear.
  const row = document.getElementById('preset-row');
  if (!row) return;
  const st = getState(fileSel.value, viewSel.value);
  const m = loadPartStyles(fileSel.value);
  let activeId = null;
  if (st.highlights && st.highlights.size) {{
    for (const idx of st.highlights) {{
      const s = m[idx];
      if (!s) continue;
      const match = _STYLE_PRESETS.find(p => _stylesMatch(p.style, s));
      if (match) {{ activeId = match.id; break; }}
    }}
  }}
  row.querySelectorAll('.preset-btn').forEach(b => {{
    b.classList.toggle('is-active', b.dataset.presetId === activeId);
  }});
}}

// Build the row at startup
setTimeout(_renderPresetRow, 50);

// Preset-remove / clear-all in project mode
document.getElementById('btn-preset-remove')?.addEventListener('click', () => {{
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) return;
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) delete m[idx];
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
  _refreshPresetActiveState();
}});
document.getElementById('btn-preset-clear')?.addEventListener('click', () => {{
  if (!confirm('Clear ALL styled parts on this figure?')) return;
  persistPartStyles(fileSel.value, {{}});
  applyStyleSheet();
  _refreshPresetActiveState();
}});

$('btn-apply-style').addEventListener('click', () => {{
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) {{
    alert('Select one or more parts first.');
    return;
  }}
  // Capture EVERY silhouette/fill control so the persistent overlay
  // matches the live highlight pixel-for-pixel.
  const style = {{
    stroke:    $('sty-stroke').value,
    width:     parseFloat($('sty-width').value),
    opacity:   parseFloat($('sty-opacity').value),
    dash:      $('sty-dash').value || null,
    fillOn:    $('sty-fill-on').checked,
    fillColor: $('sty-fill').value,
    fillAlpha: parseFloat($('sty-fill-opacity').value),
  }};
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) m[idx] = style;
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
}});
$('btn-reset-style').addEventListener('click', () => {{
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) return;
  const m = loadPartStyles(fileSel.value);
  for (const idx of st.highlights) delete m[idx];
  persistPartStyles(fileSel.value, m);
  applyStyleSheet();
}});
$('btn-reset-all-style').addEventListener('click', () => {{
  if (!confirm('Clear ALL part style overrides for this source?')) return;
  persistPartStyles(fileSel.value, {{}});
  applyStyleSheet();
}});
fileSel.addEventListener('change', applyStyleSheet);

// Expand the current selection to every leaf-Part under the same
// Onshape Assembly.  For each highlighted body, walk up to its parent
// node, then take every Part descendant of that parent (= the
// "sub-assembly" the body belongs to).  Falls back to a no-op when the
// source has no Onshape tree.
$('btn-expand-parent').addEventListener('click', () => {{
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) {{
    alert('Highlight at least one body first.');
    return;
  }}
  if (!_leafByPartIdx.size) {{
    alert("This source has no Onshape tree, so grouping by Onshape Assembly is not available here.");
    return;
  }}
  const before = st.highlights.size;
  const newSel = new Set(st.highlights);
  for (const idx of st.highlights) {{
    const leaf = _leafByPartIdx.get(idx);
    if (!leaf || !leaf._parent) continue;
    // Gather every leaf-Part descendant of the parent assembly, then
    // every solid index those leaves represent (multi-body friendly).
    const siblings = [];
    _flattenLeaves([leaf._parent], siblings);
    for (const s of siblings) {{
      for (const i of (s._solid_indices || [])) newSel.add(i);
    }}
  }}
  st.highlights = newSel;
  applyHighlights();
  console.log(`[expand] selection ${{before}} -> ${{newSel.size}}`);
}});

// Reset the depth-click cycle (the 3D handler also bumps it forward).
// Useful when the user wants to "start over" at a given pixel without
// having to move the mouse meaningfully far.
$('btn-cycle-deeper').addEventListener('click', () => {{
  // Just nudge the cycle counter exposed by the module.
  if (window.IFU_VIEWER?.advanceClickCycle) {{
    window.IFU_VIEWER.advanceClickCycle();
  }} else {{
    alert('Open the 3D pane first.');
  }}
}});

// init
setMode('smart');
refreshPane();
refreshTree();
refreshSavedViews();
applyStyleSheet();
loadUpAxisFor(fileSel.value);  // restore per-source up-axis on load
setLayout('2d');
</script>

<script type="module">
// --- 3D view-finder (three.js) --------------------------------------------
// Z-locked orbit: camera.up = world Z, so vertical edges in the model stay
// vertical on screen no matter where you orbit to.  Loads the inlined GLB
// for the active source, renders meshes with a Composer-ish look (light
// face fill + heavy crease edges), reads out the live view_dir, and offers
// a "copy view_dir" button to capture an angle for pasting into the
// Python-side STD_VIEWS / VIEWS list.

import * as THREE from 'three';
// Expose for debugging + tests (the ES-module scope is otherwise sealed)
window.THREE = THREE;
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
import {{ GLTFLoader }} from 'three/addons/loaders/GLTFLoader.js';
import {{ ViewHelper }} from 'three/addons/helpers/ViewHelper.js';
// Onshape-quality look: room IBL + SSAO + tone mapping.  Without these
// the renderer ships pre-PBR-era graphics (raw colors, no soft shading,
// no environment).
import {{ RoomEnvironment }}
  from 'three/addons/environments/RoomEnvironment.js';
import {{ EffectComposer }}
  from 'three/addons/postprocessing/EffectComposer.js';
import {{ RenderPass }}
  from 'three/addons/postprocessing/RenderPass.js';
import {{ SSAOPass }}
  from 'three/addons/postprocessing/SSAOPass.js';
import {{ OutputPass }}
  from 'three/addons/postprocessing/OutputPass.js';

const canvas = document.getElementById('webgl-canvas');
const wrap3d = document.getElementById('webgl-wrap');
const readout = document.getElementById('viewdir-readout');

// "Is 3D currently visible?" -- driven by the body's layout class so we
// don't have to query the wrap3d element style (CSS rules with !important
// can stomp on classList).
const is3DVisible = () => {{
  const cl = document.body.classList;
  return cl.contains('layout-split') || cl.contains('layout-3d');
}};

let scene, camera, renderer, controls, viewHelper;
let composer = null;       // EffectComposer for SSAO postprocess
let ssaoPass = null;       // tunable
let envTexture = null;     // PMREM-baked room environment map
let _useComposer = true;   // toggleable for perf-debug
// ViewHelper rendering also needs the main renderer's auto-clear off,
// then we manually clear before main + after main draw the gizmo.
let _viewHelperClock = null;
let loaded = new Map();      // file_id -> THREE.Group
let active = null;           // currently visible group
let partByName = new Map();  // "part_NNN" -> THREE.Object3D
let inited = false;

function init() {{
  if (inited) return;
  inited = true;

  scene = new THREE.Scene();
  // Subtle gradient backdrop instead of flat white -- this is what
  // makes Onshape's viewport feel like a "studio" instead of a
  // sterile white box.  We layer a CSS gradient on #webgl-wrap and
  // leave the scene background transparent so the canvas reveals it.
  scene.background = null;

  const r = canvas.getBoundingClientRect();
  // OrthographicCamera, NOT perspective: OCCT HLR uses orthographic
  // projection, so the SVG never has converging lines.  If the 3D pane
  // were perspective, the same iso direction would look different
  // between 2D and 3D (perspective foreshortens far edges).  Bounds are
  // re-fit in frame() per source; here we just set up the camera shell.
  const aspect = (r.width || 1) / (r.height || 1);
  camera = new THREE.OrthographicCamera(
    -1000 * aspect, 1000 * aspect, 1000, -1000, -100000, 100000);
  camera.up.set(0, 0, 1);                // Z-up world: verticals stay vertical
  camera.position.set(-2000, -4000, 3000);
  camera.lookAt(0, 0, 0);

  renderer = new THREE.WebGLRenderer({{
    canvas,
    antialias: true,
    preserveDrawingBuffer: true,  // required for screenshot exporter
    alpha: true,                  // so the CSS gradient shows through
  }});
  renderer.setSize(r.width, r.height, false);
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  // ACES Filmic + sRGB output: the single most-impactful one-liner.
  // Without this, MeshStandardMaterial colors are crushed; with it,
  // they pop the same way Onshape's do.
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  // Tuned to match Onshape's soft, slightly-cool default render: a
  // touch under 1.0 keeps the pastel palette from blowing out under
  // direct light.
  renderer.toneMappingExposure = 0.95;
  // Shadow maps for the contact shadow plane below the model
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  // ---- Image-based lighting (IBL) -----------------------------------
  // RoomEnvironment is a built-in scene of soft-coloured panels that,
  // when PMREM-baked, gives every MeshStandardMaterial in the scene
  // sky-lit ambient + soft reflections.  This is the difference
  // between "lit by three point lights" and "looks like a real CAD
  // workspace".
  try {{
    const pmrem = new THREE.PMREMGenerator(renderer);
    pmrem.compileEquirectangularShader();
    const roomScene = new RoomEnvironment(renderer);
    envTexture = pmrem.fromScene(roomScene, 0.04).texture;
    scene.environment = envTexture;
    pmrem.dispose();
  }} catch (e) {{
    console.warn('[3d] IBL setup failed; falling back to lights only:', e);
  }}

  // ---- Lights: gentle sun+fill on top of IBL -----------------------
  // IBL provides the ambient + reflections that make MeshStandardMaterial
  // look like CAD plastic; the directional sun adds just enough
  // direction-sense that cylinders read as round and plates have a
  // soft side.  Onshape's default render has very soft, almost
  // shadowless lighting; matching that means low sun intensity.
  scene.add(new THREE.AmbientLight(0xffffff, 0.10));
  const sun = new THREE.DirectionalLight(0xffffff, 0.42);
  sun.position.set(1000, -2000, 2500);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  sun.shadow.camera.near = 100;
  sun.shadow.camera.far  = 12000;
  sun.shadow.camera.left = -3000;
  sun.shadow.camera.right = 3000;
  sun.shadow.camera.top = 3000;
  sun.shadow.camera.bottom = -3000;
  sun.shadow.bias = -0.0008;
  sun.shadow.radius = 6;
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xffffff, 0.15);
  fill.position.set(-2000, 1000, 500);
  scene.add(fill);

  // ---- Floor: contact shadow only, no grid lines -------------------
  // A big horizontal plane sits just below the model and ONLY receives
  // a soft shadow from the sun (transparent ShadowMaterial).  No
  // visible grid lines -- the gradient backdrop reads as the
  // "ground" surface, just like Onshape.
  const shadowMat = new THREE.ShadowMaterial({{
    color: 0x000000,
    opacity: 0.12,    // softer to match the gentler sun
    transparent: true,
  }});
  const shadowPlane = new THREE.Mesh(
    new THREE.PlaneGeometry(20000, 20000), shadowMat);
  shadowPlane.position.z = -10;
  shadowPlane.receiveShadow = true;
  shadowPlane.userData._helper = true;
  shadowPlane.userData._shadowPlane = true;
  scene.add(shadowPlane);

  // A faint grid for orientation reference -- shown subtly via a
  // helper that the existing perf code already filters by
  // userData._helper.  Drawn smaller and lighter than before so it
  // doesn't dominate the new studio-style backdrop.
  const grid = new THREE.GridHelper(6000, 60, 0xd4d4d8, 0xeaeaec);
  grid.rotation.x = Math.PI / 2;
  grid.material.transparent = true;
  grid.material.opacity = 0.35;
  grid.userData._helper = true;
  scene.add(grid);
  const axes = new THREE.AxesHelper(300);
  axes.userData._helper = true;
  scene.add(axes);

  controls = new OrbitControls(camera, canvas);
  controls.target.set(0, 0, 0);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.update();

  // ---- Postprocessing: SSAO ----------------------------------------
  // SSAO darkens corners and crevices in proportion to local geometry
  // density.  It's the visual cue that distinguishes "flat-shaded
  // CAD" from "this looks like a real product photo".  Onshape uses
  // it heavily.
  try {{
    composer = new EffectComposer(renderer);
    composer.setPixelRatio(window.devicePixelRatio || 1);
    composer.setSize(r.width || 800, r.height || 600);
    composer.addPass(new RenderPass(scene, camera));
    ssaoPass = new SSAOPass(scene, camera, r.width || 800, r.height || 600);
    // Tuned for orthographic CAD: small kernel radius scales with
    // the bbox in frame(), large minDistance avoids haloes around
    // small parts.
    ssaoPass.kernelRadius = 24;
    ssaoPass.minDistance = 0.0008;
    ssaoPass.maxDistance = 0.06;
    ssaoPass.output = SSAOPass.OUTPUT.Default;
    composer.addPass(ssaoPass);
    composer.addPass(new OutputPass());
  }} catch (e) {{
    console.warn('[3d] postprocess setup failed; running plain renderer:', e);
    composer = null;
    ssaoPass = null;
    _useComposer = false;
  }}

  // ViewHelper (orientation gizmo): the floating axis-cube in the corner.
  // Click a face -> camera animates to that direction.  Renders as its
  // own overlay viewport in the bottom-right of the canvas.
  viewHelper = new ViewHelper(camera, renderer.domElement);
  viewHelper.controls = controls;
  viewHelper.controls.center = controls.target;
  _viewHelperClock = new THREE.Clock();
  // Click handling: forward canvas clicks to the helper when they land
  // in its viewport region.
  canvas.addEventListener('pointerdown', (e) => {{
    if (!viewHelper) return;
    const rect = canvas.getBoundingClientRect();
    if (viewHelper.handleClick(e)) {{
      // The helper consumed this click for navigation; cancel further
      // processing (so we don't accidentally raycast for selection).
      e.stopPropagation();
    }}
  }}, true);

  window.addEventListener('resize', resize);

  // Distinguish clicks from drag-orbits: only fire raycast on small motion
  let downPos = null;
  canvas.addEventListener('pointerdown', (e) => {{
    canvas.classList.add('dragging');
    downPos = [e.clientX, e.clientY];
  }});
  window.addEventListener('pointerup', (e) => {{
    canvas.classList.remove('dragging');
    if (!downPos) return;
    const dx = e.clientX - downPos[0];
    const dy = e.clientY - downPos[1];
    downPos = null;
    if (Math.hypot(dx, dy) > 4) return;       // it was a drag, not a click
    if (e.target !== canvas) return;          // click landed off-canvas
    handleCanvasClick(e);
  }});

  animate();
}}

// Click-through state: repeat-clicking the same pixel cycles through ray
// intersections so parts hidden behind other parts become selectable.
let _lastClickPx = null;
let _lastClickRayCycle = 0;

function handleCanvasClick(e) {{
  if (!active || !camera) return;
  scene.updateMatrixWorld(true);
  const rect = canvas.getBoundingClientRect();
  const ndc = new THREE.Vector2(
    ((e.clientX - rect.left) / rect.width) * 2 - 1,
    -((e.clientY - rect.top) / rect.height) * 2 + 1,
  );
  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(ndc, camera);
  // Get ALL mesh hits, sorted by depth (closest first by default).
  // Then drop adjacent duplicates from the same part so cycling steps
  // through DIFFERENT parts, not different faces of the same part.
  const rawHits = raycaster.intersectObjects([active], true)
    .filter(h => h.object && h.object.isMesh);
  const hits = [];
  let lastIdx = null;
  for (const h of rawHits) {{
    const i = _partIdxOf(h.object);
    if (i !== lastIdx) {{ hits.push({{ ...h, _partIdx: i }}); lastIdx = i; }}
  }}

  // If this click is at (essentially) the same pixel as the last,
  // advance to the next-deepest hit.  Otherwise reset the cycle.
  const pxNow = [e.clientX, e.clientY];
  const samePx = _lastClickPx &&
    Math.abs(pxNow[0] - _lastClickPx[0]) < 4 &&
    Math.abs(pxNow[1] - _lastClickPx[1]) < 4;
  if (!samePx) _lastClickRayCycle = 0;
  _lastClickPx = pxNow;

  if (hits.length === 0) {{
    if (!e.ctrlKey && !e.metaKey) window.IFU_VIEWER?.clearHighlights?.();
    return;
  }}

  // Pick the hit at the current cycle position (modulo for wrap-around)
  const hit = hits[_lastClickRayCycle % hits.length];
  if (samePx) _lastClickRayCycle++;     // next click goes deeper
  const idx = hit._partIdx;
  if (idx != null) {{
    window.IFU_VIEWER.togglePartHighlight(idx, {{
      append: e.ctrlKey || e.metaKey,
    }});
  }}
}}

function resize() {{
  if (!renderer) return;
  const r = canvas.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return;
  renderer.setSize(r.width, r.height, false);
  // Ortho: maintain the on-screen scale by keeping (right - left) / width
  // and (top - bottom) / height equal across resizes.  Use the existing
  // half-height; recompute half-width from the new aspect.
  if (camera.isOrthographicCamera) {{
    const halfHeight = (camera.top - camera.bottom) / 2;
    const aspect = r.width / r.height;
    const halfWidth = halfHeight * aspect;
    camera.left = -halfWidth;
    camera.right = halfWidth;
  }} else if (camera.isPerspectiveCamera) {{
    camera.aspect = r.width / r.height;
  }}
  camera.updateProjectionMatrix();
  if (composer) {{
    composer.setSize(r.width, r.height);
    if (ssaoPass && ssaoPass.setSize) ssaoPass.setSize(r.width, r.height);
  }}
}}

function animate() {{
  requestAnimationFrame(animate);
  if (!controls || !is3DVisible()) return;
  // Pump the ViewHelper's animation (face-snap interpolation) every
  // frame even if the user hasn't touched OrbitControls.
  if (viewHelper && viewHelper.animating) {{
    const dt = _viewHelperClock ? _viewHelperClock.getDelta() : 0.016;
    viewHelper.update(dt);
  }}
  controls.update();
  const r = canvas.getBoundingClientRect();
  if (renderer.domElement.width !== Math.round(r.width * (window.devicePixelRatio || 1)) ||
      renderer.domElement.height !== Math.round(r.height * (window.devicePixelRatio || 1))) {{
    resize();
  }}
  // Main scene through the EffectComposer (SSAO + output) when
  // available; fall back to the raw renderer.render path when the
  // composer failed to set up (no SSAO support / old WebGL).
  if (_useComposer && composer) {{
    composer.render();
  }} else {{
    renderer.autoClear = true;
    renderer.render(scene, camera);
  }}
  // Overlay the orientation gizmo on top.  ViewHelper.render() leaves
  // the WebGL viewport pointing at its tiny corner region; restore
  // the full viewport explicitly so the next frame's main render
  // fills the whole canvas.
  if (viewHelper) {{
    renderer.autoClear = false;
    viewHelper.render(renderer);
    const dpr = window.devicePixelRatio || 1;
    renderer.setViewport(0, 0, r.width * dpr, r.height * dpr);
  }}
  updateReadout();
}}

function updateReadout() {{
  const d = camera.position.clone().sub(controls.target).normalize();
  readout.textContent =
    `view_dir = (${{d.x.toFixed(3)}}, ${{d.y.toFixed(3)}}, ${{d.z.toFixed(3)}})`;
}}

function loadSource(file_id) {{
  // Hide the previously active group; show or load the new one.
  if (active) active.visible = false;
  partByName = new Map();
  if (loaded.has(file_id)) {{
    active = loaded.get(file_id);
    active.visible = true;
    indexParts(active);
    const upRot = window.IFU_VIEWER?.getActiveUpAxis?.();
    if (upRot) applyUpAxisOverride(upRot); else frame(active);
    return;
  }}
  // Static sources have their GLB baked into the page.  Dynamic
  // (Onshape-imported) sources don't -- fall back to /api/glb/<id>
  // which meshes the server-side shape on demand.
  const bakedB64 = GLB_B64[file_id];
  const _hookGroup = (grp) => {{
    // Onshape-style materials: low metalness for most parts so they
    // read like neutral CAD plastic / aluminium-equivalent surfaces.
    // The IBL environment provides ambient + reflections so we don't
    // need high roughness to hide flat shading.  EnvMapIntensity dials
    // how much the environment shows up on each material.
    grp.traverse(obj => {{
      if (obj.isMesh) {{
        // Pick a palette colour for this part so multi-part assemblies
        // read distinctly the way Onshape's auto-assigned colours do.
        // We look up the part idx (set by trimesh as the "part_NNN"
        // node name) and index into the palette deterministically --
        // the same part always gets the same colour across reloads.
        const baseColor = _bodyColorForObject(obj);
        obj.material = new THREE.MeshStandardMaterial({{
          color: baseColor,
          metalness: 0.05,
          roughness: 0.72,
          envMapIntensity: 1.1,
          transparent: false,
          side: THREE.DoubleSide,
          polygonOffset: true,
          polygonOffsetFactor: 1,
          polygonOffsetUnits: 1,
        }});
        // Shadow casting + receiving so SSAO + the contact shadow plane
        // get accurate occlusion data.
        obj.castShadow = true;
        obj.receiveShadow = true;
        // Feature edges only: 45deg threshold (was 30) skips most of
        // the curved-cylinder facet noise that EdgesGeometry would
        // otherwise overlay on every revolute surface.  Very low
        // opacity -- Onshape's edges are barely there at normal
        // zoom, they just hint at facet geometry without dominating
        // the surface read.
        const edges = new THREE.EdgesGeometry(obj.geometry, 45);
        const lines = new THREE.LineSegments(
          edges,
          new THREE.LineBasicMaterial({{
            color: 0x3a4554,
            transparent: true,
            opacity: 0.22,
            linewidth: 1,
          }})
        );
        lines.userData.isEdge = true;
        obj.add(lines);
        obj.userData.baseColor = baseColor;
      }}
    }});
    loaded.set(file_id, grp);
    scene.add(grp);
    active = grp;
    indexParts(grp);
    const upRot = window.IFU_VIEWER?.getActiveUpAxis?.();
    if (upRot) applyUpAxisOverride(upRot); else frame(grp);
  }};

  const _loadFromB64 = (b64) => {{
    const url = 'data:model/gltf-binary;base64,' + b64;
    const loader = new GLTFLoader();
    loader.load(url, (gltf) => _hookGroup(gltf.scene), undefined, (err) => {{
      console.error('GLB load failed', err);
      readout.textContent = '(GLB load failed - see console)';
    }});
  }};

  if (bakedB64) {{ _loadFromB64(bakedB64); return; }}

  // No baked mesh -- ask the server to generate one.  This can take
  // 5-30 seconds depending on assembly size, so show progress.
  readout.textContent = 'meshing ' + file_id + ' ...';
  fetch(API_BASE + '/api/glb/' + encodeURIComponent(file_id))
    .then(r => {{
      if (!r.ok) {{
        return r.json().then(j => {{
          throw new Error(j.error || ('HTTP ' + r.status));
        }});
      }}
      return r.json();
    }})
    .then(data => {{
      if (!data.b64) throw new Error('no GLB returned');
      readout.textContent = `${{file_id}} : ${{data.parts}} parts, ${{data.kb}} KB`;
      _loadFromB64(data.b64);
    }})
    .catch(err => {{
      console.error('GLB fetch failed', err);
      readout.textContent = '(no 3D mesh: ' + (err.message || err) + ')';
    }});
}}

// ---- Configuration panel (3D overlay) ---------------------------------
// Shows the active source's Onshape configuration parameters as a small
// floating panel anchored to the 3D viewport.  Changing any value
// fires /api/sources/<id>/reconfigure, which re-translates the STEP
// and replaces the in-memory shape; we then evict the local GLB cache
// and reload, so the 3D pane updates in place.

const _cfgPanel  = document.getElementById('cfg-panel');
const _cfgBody   = document.getElementById('cfg-body');
const _cfgStatus = document.getElementById('cfg-status');
const _cfgHdr    = document.getElementById('cfg-header');
const _cfgColl   = document.getElementById('cfg-collapse');
let   _cfgInputs = {{}};          // parameter_id -> <input|select>
let   _cfgCurrentSourceId = null;  // last source we loaded into the panel
let   _cfgReloadTimer = null;
let   _cfgCollapsed = localStorage.getItem('ifu:cfg_collapsed') === '1';

function _cfgSetCollapsed(yes) {{
  _cfgCollapsed = !!yes;
  _cfgBody.style.display   = _cfgCollapsed ? 'none' : 'block';
  _cfgStatus.style.display = _cfgCollapsed ? 'none' : 'block';
  _cfgColl.textContent     = _cfgCollapsed ? '+' : '−';
  localStorage.setItem('ifu:cfg_collapsed', _cfgCollapsed ? '1' : '0');
}}
if (_cfgColl) _cfgColl.addEventListener('click', () =>
  _cfgSetCollapsed(!_cfgCollapsed));
if (_cfgHdr) _cfgHdr.addEventListener('dblclick', () =>
  _cfgSetCollapsed(!_cfgCollapsed));
_cfgSetCollapsed(_cfgCollapsed);

async function _cfgLoadForSource(sourceId) {{
  if (!sourceId || !_cfgPanel) {{ if (_cfgPanel) _cfgPanel.style.display = 'none'; return; }}
  _cfgCurrentSourceId = sourceId;
  _cfgInputs = {{}};
  _cfgBody.innerHTML = '';
  _cfgStatus.textContent = 'loading parameters...';
  _cfgPanel.style.display = 'block';
  let cfg;
  try {{
    const r = await fetch(API_BASE + '/api/sources/'
                            + encodeURIComponent(sourceId) + '/configuration');
    if (!r.ok) {{
      // Unknown / non-Onshape source -- hide the panel quietly
      _cfgPanel.style.display = 'none';
      return;
    }}
    cfg = await r.json();
  }} catch (_e) {{
    _cfgPanel.style.display = 'none';
    return;
  }}
  if (!cfg.has_config || !(cfg.parameters || []).length) {{
    _cfgPanel.style.display = 'none';
    return;
  }}
  _cfgStatus.textContent =
      cfg.parameters.length + ' parameter'
    + (cfg.parameters.length === 1 ? '' : 's')
    + ' -- changes update the 3D in place';
  for (const p of cfg.parameters) {{
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;flex-direction:column;gap:3px;margin-bottom:8px;';
    const lab = document.createElement('label');
    lab.textContent = p.name || p.id || '(unnamed)';
    lab.style.cssText = 'font-weight:500;color:#3f3f46;';
    row.appendChild(lab);
    let input;
    if (p.type === 'enum' && (p.options || []).length) {{
      input = document.createElement('select');
      input.style.cssText = 'width:100%;padding:4px 6px;border:1px solid #d4d4d8;'
                          + 'border-radius:3px;background:#fff;font-size:12px;';
      for (const o of p.options) {{
        const opt = document.createElement('option');
        opt.value = o.value; opt.textContent = o.label;
        if (o.value === p.default) opt.selected = true;
        input.appendChild(opt);
      }}
    }} else if (p.type === 'boolean') {{
      const wrap = document.createElement('label');
      wrap.style.cssText = 'display:flex;align-items:center;gap:6px;cursor:pointer;'
                         + 'font-size:12px;color:#52525b;';
      input = document.createElement('input');
      input.type = 'checkbox';
      if (p.default === true || p.default === 'true') input.checked = true;
      wrap.appendChild(input);
      const sp = document.createElement('span');
      sp.textContent = input.checked ? 'enabled' : 'disabled';
      wrap.appendChild(sp);
      input.addEventListener('change', () => {{
        sp.textContent = input.checked ? 'enabled' : 'disabled';
      }});
      // Normalise value semantics
      Object.defineProperty(input, 'value', {{
        get() {{ return input.checked ? 'true' : 'false'; }},
      }});
      row.appendChild(wrap);
    }} else {{
      input = document.createElement('input');
      input.type = 'text';
      input.style.cssText = 'width:100%;padding:4px 6px;border:1px solid #d4d4d8;'
                          + 'border-radius:3px;background:#fff;font-size:12px;'
                          + 'box-sizing:border-box;';
      if (p.default != null) input.value = String(p.default);
      if (p.unit) input.placeholder = p.unit;
    }}
    if (p.type !== 'boolean') row.appendChild(input);
    _cfgInputs[p.id] = input;
    input.addEventListener('change', () =>
      _cfgScheduleReconfigure(sourceId));
    _cfgBody.appendChild(row);
  }}
  if (_cfgCollapsed) _cfgSetCollapsed(true);
}}

function _cfgScheduleReconfigure(sourceId) {{
  if (_cfgReloadTimer) clearTimeout(_cfgReloadTimer);
  // Small debounce in case the user is tabbing through several
  // controls -- collapse a burst into one re-translation.
  _cfgReloadTimer = setTimeout(() => _cfgApply(sourceId), 250);
}}

async function _cfgApply(sourceId) {{
  if (sourceId !== _cfgCurrentSourceId) return;
  const values = {{}};
  for (const [pid, el] of Object.entries(_cfgInputs)) {{
    const v = el.value;
    if (v !== undefined && v !== null && v !== '') values[pid] = v;
  }}
  _cfgStatus.textContent = 'reconfiguring (Onshape -> STEP -> 3D)...';
  // Disable inputs while re-translating
  for (const el of Object.values(_cfgInputs)) el.disabled = true;
  try {{
    const r = await fetch(
      API_BASE + '/api/sources/' + encodeURIComponent(sourceId)
        + '/reconfigure',
      {{ method: 'POST',
         headers: {{ 'Content-Type': 'application/json' }},
         body: JSON.stringify({{ configuration: values }}) }});
    if (!r.ok) {{
      const j = await r.json().catch(() => ({{}}));
      throw new Error(j.error || ('HTTP ' + r.status));
    }}
    await r.json();
    // Bust the local GLB cache for this source so the next loadSource()
    // pulls the freshly meshed geometry.
    if (loaded && loaded.has && loaded.has(sourceId)) {{
      const grp = loaded.get(sourceId);
      if (grp && grp.parent) grp.parent.remove(grp);
      loaded.delete(sourceId);
    }}
    if (window.GLB_B64) delete window.GLB_B64[sourceId];
    // Reload the 3D mesh in place
    if (typeof loadSource === 'function') loadSource(sourceId);
    _cfgStatus.textContent = '3D updated';
    setTimeout(() => {{
      if (_cfgCurrentSourceId === sourceId) {{
        _cfgStatus.textContent =
            'changes update the 3D in place';
      }}
    }}, 2000);
  }} catch (e) {{
    _cfgStatus.textContent = 'reconfigure failed: ' + (e.message || e);
    (window.IFU_UI?.toast || function(){{}})(
      'Reconfigure failed: ' + (e.message || e), 'error');
    (window._toggleServerLog || function(){{}})(true);
  }} finally {{
    for (const el of Object.values(_cfgInputs)) el.disabled = false;
  }}
}}

// Wire up: when the legacy editor's file selector changes, refresh
// the configuration panel against the new source.  Initial load
// after page boot is handled by a one-shot timer because the file
// selector is populated AFTER this script runs.
if (typeof fileSel !== 'undefined' && fileSel) {{
  fileSel.addEventListener('change', () =>
    _cfgLoadForSource(fileSel.value));
  setTimeout(() => _cfgLoadForSource(fileSel.value), 250);
}}

window.IFU_VIEWER.reloadConfig = (sid) =>
  _cfgLoadForSource(sid || _cfgCurrentSourceId);

function indexParts(grp) {{
  partByName = new Map();
  grp.traverse(obj => {{
    if (obj.isMesh && obj.name) {{
      // node names from trimesh come back as the geometry name; keep both
      partByName.set(obj.name, obj);
    }}
    // walk parents to capture node-level names too
    if (obj.userData && obj.userData.name) {{
      partByName.set(obj.userData.name, obj);
    }}
  }});
  // also walk gltf scene children which carry node names
  grp.children.forEach(child => {{
    if (child.name) partByName.set(child.name, child);
    child.traverse(o => {{ if (o.name) partByName.set(o.name, o); }});
  }});
}}

function frame(grp) {{
  const bbox = new THREE.Box3().setFromObject(grp);
  const size = bbox.getSize(new THREE.Vector3());
  const center = bbox.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  controls.target.copy(center);
  // approach from the stored iso preset if available, else default
  let vd = window.IFU_VIEWER?.getActiveViewDir?.() || [-0.5, -1.0, 0.7];
  const dir = new THREE.Vector3(vd[0], vd[1], vd[2]).normalize();
  camera.position.copy(center).add(dir.multiplyScalar(maxDim * 2.2));
  if (camera.isOrthographicCamera) {{
    // Project the bbox 8 corners to camera-local axes, then size the
    // ortho frustum to enclose them with a 10% pad.  This matches the
    // HLR projection's natural fit on the SAME view_dir so 2D and 3D
    // pane have equivalent zoom/extent.
    const cornersWorld = [
      new THREE.Vector3(bbox.min.x, bbox.min.y, bbox.min.z),
      new THREE.Vector3(bbox.min.x, bbox.min.y, bbox.max.z),
      new THREE.Vector3(bbox.min.x, bbox.max.y, bbox.min.z),
      new THREE.Vector3(bbox.min.x, bbox.max.y, bbox.max.z),
      new THREE.Vector3(bbox.max.x, bbox.min.y, bbox.min.z),
      new THREE.Vector3(bbox.max.x, bbox.min.y, bbox.max.z),
      new THREE.Vector3(bbox.max.x, bbox.max.y, bbox.min.z),
      new THREE.Vector3(bbox.max.x, bbox.max.y, bbox.max.z),
    ];
    // Make sure camera matrices are current before we use them
    camera.lookAt(center);
    camera.updateMatrixWorld();
    let minX = +Infinity, maxX = -Infinity, minY = +Infinity, maxY = -Infinity;
    for (const c of cornersWorld) {{
      const local = c.clone().applyMatrix4(camera.matrixWorldInverse);
      if (local.x < minX) minX = local.x;
      if (local.x > maxX) maxX = local.x;
      if (local.y < minY) minY = local.y;
      if (local.y > maxY) maxY = local.y;
    }}
    const padX = (maxX - minX) * 0.05;
    const padY = (maxY - minY) * 0.05;
    let left = minX - padX, right = maxX + padX;
    let top = maxY + padY, bottom = minY - padY;
    // Keep aspect ratio to the canvas so the model isn't stretched
    const r = canvas.getBoundingClientRect();
    const aspect = (r.width || 1) / (r.height || 1);
    const w = right - left;
    const h = top - bottom;
    if (w / h > aspect) {{
      // wider than canvas: expand vertically
      const want_h = w / aspect;
      const extra = (want_h - h) / 2;
      top += extra; bottom -= extra;
    }} else {{
      const want_w = h * aspect;
      const extra = (want_w - w) / 2;
      left -= extra; right += extra;
    }}
    camera.left = left;
    camera.right = right;
    camera.top = top;
    camera.bottom = bottom;
    camera.near = -maxDim * 10;
    camera.far = maxDim * 10;
  }} else {{
    camera.near = maxDim / 100;
    camera.far = maxDim * 20;
  }}
  camera.updateProjectionMatrix();
  controls.update();
}}

function _partIdxOf(obj) {{
  // walk up the chain looking for "part_NNN" - trimesh's GLB nests the
  // mesh inside a named node a level or two up
  let cur = obj;
  while (cur && cur !== active) {{
    const m = cur.name && cur.name.match(/^part_(\d+)$/);
    if (m) return parseInt(m[1]);
    cur = cur.parent;
  }}
  return null;
}}

// Onshape-inspired soft pastel palette.  Stays in the low-saturation
// range so the assembly reads as "CAD" rather than "primary-coloured
// toy".  All entries should look fine on the gradient backdrop with
// SSAO darkening corners -- avoid anything that goes muddy when
// shaded.  The first entry is intentionally the steel-blue Onshape
// uses by default so single-part figures look familiar.
const _BODY_PALETTE = [
  0x9dbcda,  // steel blue (Onshape default)
  0xa6c8d6,  // sky
  0xb9c8ce,  // light grey-blue
  0xb5d2bd,  // mint
  0xc5d2a8,  // pale olive
  0xd6d199,  // pale sand
  0xd6b094,  // peach
  0xd5a3b3,  // rose
  0xc8a8cd,  // lavender
  0xa9b4d3,  // periwinkle
  0xa8cbc4,  // pale teal
  0xbac4b0,  // sage
];

function _hexHash(s) {{
  // Cheap deterministic hash so unidentified parts (no idx) still
  // get a stable colour based on whatever name we DO have.
  let h = 5381;
  for (let i = 0; i < s.length; i++) {{
    h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  }}
  return Math.abs(h);
}}

function _bodyColorForObject(obj) {{
  // 1) Onshape import / trimesh GLB: "part_NNN" -> idx -> palette
  let cur = obj;
  let nameStack = [];
  while (cur) {{
    if (cur.name) nameStack.push(cur.name);
    const m = cur.name && cur.name.match(/^part_(\d+)$/);
    if (m) return _BODY_PALETTE[parseInt(m[1]) % _BODY_PALETTE.length];
    cur = cur.parent;
  }}
  // 2) Fall back to hashing the deepest name we found
  const key = nameStack.join('|') || (obj.uuid || 'unknown');
  return _BODY_PALETTE[_hexHash(key) % _BODY_PALETTE.length];
}}

function applyHighlights3D(set) {{
  if (!active) return;
  const any = set && set.size > 0;
  active.traverse(o => {{
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    const hit = any && idx != null && set.has(idx);
    // Restore the part's PALETTE colour when not selected, instead of
    // the old hardcoded grey -- otherwise clearing a selection makes
    // every part wash to the same shade.  userData.baseColor was set
    // at material-creation time in _hookGroup().
    const baseColor = (o.userData && o.userData.baseColor != null)
                       ? o.userData.baseColor
                       : _bodyColorForObject(o);
    o.material.color.setHex(hit ? 0x00836a : baseColor);
    if (any && !hit) {{
      o.material.opacity = 0.18;
      o.material.transparent = true;
      o.material.depthWrite = false;
    }} else {{
      o.material.opacity = 1.0;
      o.material.transparent = false;
      o.material.depthWrite = true;
    }}
  }});
}}

function snapToPresetView() {{
  if (!active) return;
  frame(active);
}}

// Up-axis override: rotate the loaded group so the user-picked axis lands
// on world Z.  The rotation comes from the same {{axis, angle}} table the
// classic script uses; the Python side reads the same tuple from SOURCES.
function applyUpAxisOverride(rot) {{
  if (!active || !rot) return;
  const axis = new THREE.Vector3(rot.axis[0], rot.axis[1], rot.axis[2])
    .normalize();
  const q = new THREE.Quaternion()
    .setFromAxisAngle(axis, (rot.angle || 0) * Math.PI / 180);
  active.setRotationFromQuaternion(q);
  frame(active);
}}

// Replaces the old toggle button: the classic-script segmented control
// drives layout, and just tells us whether the WebGL pane is visible.
function set3DActive(on) {{
  if (on) {{
    init();           // idempotent
    // CSS already showed the canvas; resize after the next reflow so the
    // renderer matches the new pane width (especially when entering Split).
    requestAnimationFrame(() => {{
      resize();
      const fid = window.IFU_VIEWER.getActiveFileId();
      loadSource(fid);
    }});
  }}
  // When off, no extra work needed -- CSS hides the canvas; we keep the
  // scene loaded so re-entering doesn't pay the GLB-parse cost again.
}}

document.getElementById('btn-lock-view').addEventListener('click', () => {{
  const d = camera.position.clone().sub(controls.target).normalize();
  const tup = `(${{d.x.toFixed(3)}}, ${{d.y.toFixed(3)}}, ${{d.z.toFixed(3)}})`;
  navigator.clipboard?.writeText(tup);
  readout.textContent = `copied ${{tup}}`;
  setTimeout(updateReadout, 1500);
}});

document.getElementById('btn-reset-3d').addEventListener('click', () => {{
  if (active) frame(active);
}});

// Sync with the file picker: switching source while 3D is on screen swaps GLB.
window.IFU_VIEWER.onFileChange(() => {{
  if (is3DVisible()) loadSource(window.IFU_VIEWER.getActiveFileId());
}});
// Switching the 2D view preset snaps the 3D camera to that direction too,
// so the two panes stay roughly aligned in Split mode.
window.IFU_VIEWER.onViewChange(() => {{
  if (is3DVisible()) snapToPresetView();
}});

// Expose for the classic script's selection + orientation + layout handlers,
// plus debug/test access to the underlying three.js scene.
window.IFU_VIEWER.applyHighlights3D = applyHighlights3D;
window.IFU_VIEWER._scene = () => scene;
window.IFU_VIEWER._camera = () => camera;
window.IFU_VIEWER._renderer = () => renderer;
window.IFU_VIEWER._active = () => active;
window.IFU_VIEWER.applyUpAxisOverride = (rot) => {{
  applyUpAxisOverride(rot);
}};
window.IFU_VIEWER.set3DActive = set3DActive;
window.IFU_VIEWER.getCurrentViewDir = () => {{
  if (!camera || !controls) return null;
  const d = camera.position.clone().sub(controls.target).normalize();
  return [d.x, d.y, d.z];
}};

// Per-part colour info for tests + tooling.  Returns an array of
// objects with keys idx + color_hex for every mesh in the active group.
window.IFU_VIEWER.getActivePartColors = () => {{
  if (!active) return null;
  const out = [];
  active.traverse(o => {{
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    if (idx == null) return;
    const c = o.material && o.material.color;
    out.push({{
      idx,
      color_hex: c ? '#' + c.getHexString() : null,
      base_hex: (o.userData && o.userData.baseColor != null)
                  ? '#' + o.userData.baseColor.toString(16).padStart(6, '0')
                  : null,
    }});
  }});
  return out;
}};

window.IFU_VIEWER.getBodyPalette = () => _BODY_PALETTE.slice();

// Renderer + scene state inspector -- used by tests + the future
// graphics-quality controls.  Returns null when the 3D viewer hasn't
// initialised yet.
window.IFU_VIEWER.getRendererState = () => {{
  if (!renderer || !scene) return null;
  let hasShadowPlane = false;
  scene.traverse(o => {{
    if (o.userData && o.userData._shadowPlane) hasShadowPlane = true;
  }});
  return {{
    toneMapping: renderer.toneMapping,
    outputColorSpace: renderer.outputColorSpace,
    toneMappingExposure: renderer.toneMappingExposure,
    shadowMapEnabled: !!(renderer.shadowMap && renderer.shadowMap.enabled),
    hasEnvironment: !!scene.environment,
    hasComposer: !!composer,
    hasSSAO: !!ssaoPass,
    hasShadowPlane,
  }};
}};

// Camera position + target as world-space tuples.  Used by the saved-views
// feature to capture and recall exact viewpoints (no view_dir conversion).
window.IFU_VIEWER.getCameraEyeTarget = () => {{
  if (!camera || !controls) return null;
  return {{
    eye:    [camera.position.x, camera.position.y, camera.position.z],
    target: [controls.target.x,  controls.target.y,  controls.target.z],
  }};
}};

// Manually advance the depth-click cycle.  The classic-side button uses
// this when the user wants the NEXT pixel-click to drill one layer deeper
// even though their mouse may have moved slightly.
window.IFU_VIEWER.advanceClickCycle = () => {{
  _lastClickRayCycle++;
  console.log('[depth-click] next click will be layer', _lastClickRayCycle);
}};

// Override the classic-side stub so the screenshot exporter can force a
// fresh render into the back-buffer immediately before reading pixels.
window.renderer3d_request_present = () => {{
  if (renderer && scene && camera) {{
    // preserveDrawingBuffer might not be on; render explicitly into the
    // visible canvas right before the screenshot reads it
    renderer.render(scene, camera);
  }}
}};

window.IFU_VIEWER.snapCameraTo = (eye, target) => {{
  if (!camera || !controls) return;
  camera.position.set(eye[0], eye[1], eye[2]);
  controls.target.set(target[0], target[1], target[2]);
  camera.lookAt(controls.target);
  // Re-fit the ortho frustum to the new direction WITHOUT moving the
  // camera back to the framed default.  We just want the bounds redone.
  if (active && camera.isOrthographicCamera) {{
    const bbox = new THREE.Box3().setFromObject(active);
    camera.updateMatrixWorld();
    let minX = +Infinity, maxX = -Infinity,
        minY = +Infinity, maxY = -Infinity;
    const cs = [bbox.min, bbox.max];
    for (const cx of [cs[0].x, cs[1].x])
      for (const cy of [cs[0].y, cs[1].y])
        for (const cz of [cs[0].z, cs[1].z]) {{
          const p = new THREE.Vector3(cx, cy, cz)
            .applyMatrix4(camera.matrixWorldInverse);
          if (p.x < minX) minX = p.x;
          if (p.x > maxX) maxX = p.x;
          if (p.y < minY) minY = p.y;
          if (p.y > maxY) maxY = p.y;
        }}
    const padX = (maxX - minX) * 0.05;
    const padY = (maxY - minY) * 0.05;
    let l = minX - padX, r = maxX + padX,
        t = maxY + padY, bm = minY - padY;
    const rect = canvas.getBoundingClientRect();
    const aspect = (rect.width || 1) / (rect.height || 1);
    const w = r - l, h = t - bm;
    if (w / h > aspect) {{
      const wantH = w / aspect, extra = (wantH - h) / 2;
      t += extra; bm -= extra;
    }} else {{
      const wantW = h * aspect, extra = (wantW - w) / 2;
      l -= extra; r += extra;
    }}
    camera.left = l; camera.right = r;
    camera.top  = t; camera.bottom = bm;
    camera.updateProjectionMatrix();
  }}
  controls.update();
}};

// Per-part 3D styling: colour, opacity per part_idx.  Each idx maps to an
// optional override; meshes with no entry stay at the default.
window.IFU_VIEWER.applyPartStyles3D = (stylesByIdx) => {{
  if (!active) return;
  active.traverse(o => {{
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    if (idx == null) return;
    const st = stylesByIdx[idx];
    if (st) {{
      const hex = (st.stroke || '#00836a').replace('#', '');
      const n = parseInt(hex, 16);
      o.material.color.setHex(isNaN(n) ? 0x00836a : n);
      o.material.opacity = (st.opacity != null) ? st.opacity : 1.0;
      o.material.transparent = (o.material.opacity < 1.0);
    }} else {{
      o.material.color.setHex(0xe8e8ea);
      o.material.opacity = 1.0;
      o.material.transparent = false;
    }}
  }});
}};

// --- Generate-from-3D: button in the 3D toolbar -----------------------------
// Calls the local server's /api/render with the current camera direction,
// then injects the returned SVG as a special "live" view in the 2D pane.
// If the server isn't running, the button greys out with a helpful tooltip.
const btnGen = document.getElementById('btn-generate');

// Server URL: same-origin when viewer is loaded via http://, else hop to
// the standard local server.  Works whether the user opened
// http://localhost:5000/ or a file:// build.  Promoted to window so the
// classic-script silhouette fetcher (in the other <script> block) can
// reach it too.
const API_BASE = (location.protocol === 'http:' || location.protocol === 'https:')
  ? ''
  : 'http://localhost:5000';
window.API_BASE = API_BASE;

async function probeServer() {{
  try {{
    const r = await fetch(API_BASE + '/api/healthz', {{ cache: 'no-store' }});
    if (!r.ok) throw new Error('healthz ' + r.status);
    const data = await r.json();
    return data && data.ok;
  }} catch (_e) {{
    return false;
  }}
}}

async function generateLiveSVG() {{
  if (!camera || !controls) return;
  const fid = window.IFU_VIEWER.getActiveFileId();
  // Send the camera as {{eye, target}} -- two explicit world-space points.
  // Unambiguous: HLR sets up its projection from the exact same camera
  // OrbitControls is currently driving.  No view_dir sign convention,
  // no separate focal arg, no chance of meaning the opposite side.
  const eye    = [camera.position.x, camera.position.y, camera.position.z];
  const target = [controls.target.x,  controls.target.y,  controls.target.z];
  // Send the current Up: override so the server rotates the cached shape
  // the same way the 3D view did before running HLR -- otherwise the SVG
  // comes back in the model's native (unrotated) orientation.
  const upRot = window.IFU_VIEWER.getActiveUpAxis?.();
  const body = {{ file_id: fid, eye, target }};
  if (upRot && upRot.angle && upRot.angle !== 0) {{
    body.up_axis = {{ axis: upRot.axis, angle: upRot.angle }};
  }}

  const orig = btnGen.innerHTML;
  btnGen.disabled = true;
  btnGen.innerHTML = '&#8987; rendering ...';
  // Freeze the orbit so the 3D pane can't drift away from the angle the
  // server is rendering -- otherwise the user sees a "matching" 2D that
  // doesn't match what the 3D is now showing.
  controls.enabled = false;

  try {{
    const r = await fetch(API_BASE + '/api/render', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
    if (!r.ok) {{
      const err = await r.json().catch(() => ({{ error: 'HTTP ' + r.status }}));
      throw new Error(err.error || 'HTTP ' + r.status);
    }}
    const svgText = await r.text();
    const elapsed = r.headers.get('X-Render-Seconds') || '?';
    const breakdown = r.headers.get('X-Render-Breakdown') || '';
    // injectLiveSVG stores a view_dir for the Live entry; derive it from
    // the eye/target we just sent so the dropdown's Live preset still
    // round-trips for snap-back.
    const _vdx = eye[0] - target[0], _vdy = eye[1] - target[1], _vdz = eye[2] - target[2];
    const _vdL = Math.hypot(_vdx, _vdy, _vdz) || 1;
    const view_dir = [_vdx / _vdL, _vdy / _vdL, _vdz / _vdL];
    // Cache the camera context for the silhouette endpoint (it has to
    // project into the same (u,v) space as the SVG we just received).
    window.IFU_VIEWER._setLiveCamCtx?.(fid, {{
      eye, target, up_axis: body.up_axis || null,
    }});
    window.IFU_VIEWER.injectLiveSVG(fid, view_dir, svgText);
    // Auto-switch to Split so the new SVG appears on the left next to the 3D
    window.IFU_VIEWER.setLayout?.('split');
    const polysHeader = r.headers.get('X-Render-Polylines');
    const polys = polysHeader ? parseInt(polysHeader, 10) : null;
    if (polys === 0) {{
      // The SVG was generated but contains no visible lines.  This is
      // the exact "nothing appeared" failure mode.  Surface it loudly
      // so the user knows it's not a UI bug.
      btnGen.innerHTML = '&#9888; 0 lines';
      btnGen.title =
          'HLR produced 0 polylines for this source/view.\n'
        + 'Common causes:\n'
        + '  - source loaded but has no solid bodies (only sketches/surfaces)\n'
        + '  - camera is inside the model or at a degenerate angle\n'
        + '  - mesh_defl is too coarse for very small geometry\n'
        + 'Open the Server log (header: log) to see what the backend did.';
      (window.IFU_UI?.toast || function(){{}})(
        'Render returned 0 lines -- check the server log', 'error');
      (window._toggleServerLog || function(){{}})(true);
    }} else {{
      btnGen.innerHTML =
        `&#10003; ${{elapsed}}s${{polys != null ? ` (${{polys}} lines)` : ''}}`;
    }}
    if (breakdown) {{
      readout.title = `last render: ${{elapsed}}s -- ${{breakdown}}`;
      console.log(`[generate] ${{elapsed}}s -- ${{breakdown}}`
                    + (polys != null ? ` -- ${{polys}} polylines` : ''));
    }}
  }} catch (e) {{
    console.error('generate failed:', e);
    btnGen.innerHTML = '&#10007; ' + (e.message || 'render failed');
    (window.IFU_UI?.toast || function(){{}})(
      'Render failed: ' + (e.message || e), 'error');
    (window._toggleServerLog || function(){{}})(true);
  }} finally {{
    controls.enabled = true;
    setTimeout(() => {{ btnGen.disabled = false; btnGen.innerHTML = orig; }}, 4000);
  }}
}}

btnGen.addEventListener('click', generateLiveSVG);

// Programmatic render entry point: same pipeline as the user-driven
// "generate 2D" button, but you supply eye/target/up_axis explicitly
// instead of reading them from the live three.js camera.  Used by
// EditorScreen's auto-render-on-open path so a new figure inside a
// View shows the View's drawing right away -- no manual click.
async function generateLiveSVGForCamera(camInfo) {{
  if (!camInfo || !camInfo.eye || !camInfo.target) return;
  const fid = window.IFU_VIEWER.getActiveFileId();
  if (!fid) return;
  const eye    = [camInfo.eye[0], camInfo.eye[1], camInfo.eye[2]];
  const target = [camInfo.target[0], camInfo.target[1], camInfo.target[2]];
  const upRot = window.IFU_VIEWER.getActiveUpAxis?.();
  const body = {{ file_id: fid, eye, target }};
  if (upRot && upRot.angle && upRot.angle !== 0) {{
    body.up_axis = {{ axis: upRot.axis, angle: upRot.angle }};
  }}
  // Visual feedback on the (button) so the user sees the render in
  // flight even though they didn't click it.
  const orig = btnGen ? btnGen.innerHTML : '';
  if (btnGen) {{ btnGen.disabled = true; btnGen.innerHTML = '&#8987; rendering...'; }}
  try {{
    const r = await fetch(API_BASE + '/api/render', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const svgText = await r.text();
    const _vdx = eye[0] - target[0], _vdy = eye[1] - target[1], _vdz = eye[2] - target[2];
    const _vdL = Math.hypot(_vdx, _vdy, _vdz) || 1;
    const view_dir = [_vdx / _vdL, _vdy / _vdL, _vdz / _vdL];
    window.IFU_VIEWER._setLiveCamCtx?.(fid, {{
      eye, target, up_axis: body.up_axis || null,
    }});
    window.IFU_VIEWER.injectLiveSVG(fid, view_dir, svgText);
    window.IFU_VIEWER.setLayout?.('split');
    if (btnGen) {{
      const elapsed = r.headers.get('X-Render-Seconds') || '?';
      btnGen.innerHTML = `&#10003; ${{elapsed}}s`;
    }}
  }} catch (e) {{
    console.warn('[auto-render] failed:', e?.message || e);
    if (btnGen) btnGen.innerHTML = '&#10007; ' + (e.message || 'render failed');
    (window.IFU_UI?.toast || function(){{}})(
      'Render failed: ' + (e.message || e), 'error');
  }} finally {{
    if (typeof hideCanvasLoading === 'function') hideCanvasLoading();
    setTimeout(() => {{
      if (btnGen) {{ btnGen.disabled = false; btnGen.innerHTML = orig; }}
    }}, 3000);
  }}
}}
window.generateLiveSVGForCamera = generateLiveSVGForCamera;

// Decide whether the server is reachable at load time and grey the button
// out if not (file:// or stand-alone deployment).
probeServer().then((alive) => {{
  if (!alive) {{
    btnGen.classList.add('unavailable');
    btnGen.disabled = true;
    btnGen.title = "Local server not reachable. Start it with:\\n"
                   + "  python serve.py\\n"
                   + "then open http://localhost:5000";
  }}
  // Footprint prefetch is NO LONGER fired here -- it's slow (~2 min
  // for siderail z-buffer raster, and click-anywhere now works via
  // the client-side convex-hull layer with no server roundtrip).
  // The shade-fill flow triggers the prefetch lazily when the user
  // toggles shade on, so we only pay the rasterization cost when
  // shading is actually needed.
}});
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
