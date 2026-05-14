"""Build the interactive HTML IFU viewer.

For each STEP file, runs per-solid HLR at a handful of standard views,
emits part-tagged SVG, then assembles every SVG into one self-contained
HTML page with:

  - file / view / mode pickers
  - 2D mode (HLR vector): pan + zoom, click-to-highlight, callout arrows,
    layer toggles (silhouette / sharp / smooth / hidden)
  - 3D mode (three.js): Z-locked orbit view-finder over a triangulated
    GLB of the same source.  Verticals always project vertical.  Camera
    direction read out live; "lock view" copies a `view_dir` tuple to
    the clipboard for pasting into ``STD_VIEWS``.
  - Onshape feature tree (when ``onshape`` IDs are provided): live instance
    tree pulled from the assembly endpoint; click a node to highlight the
    matching solid in either mode (name-match against STEP solid labels).
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


HERE = Path(__file__).parent
OUT = HERE / "out"

# Onshape client - shared with fetch_contesa_step.py.  Optional: viewer
# still builds without it (the Onshape feature-tree sidebar just stays empty
# for any source whose ``onshape`` entry is None or the import fails).
_ONSHAPE_CLIENT_PATHS = [
    Path(r"C:\Users\FredMarshAccora\Projects\onshape-analytics"),
]
for _p in _ONSHAPE_CLIENT_PATHS:
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
    # Load that project's .env (Onshape API keys + base URL) since the
    # client constructor only reads env vars, never loads dotenv itself.
    _env = _p / ".env"
    if _env.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env)
        except Exception:
            pass
try:
    from onshape_analytics.client import OnshapeClient as _OnshapeClient
except Exception as _e:
    _OnshapeClient = None
    print(f"  (Onshape client unavailable: {_e}; feature trees will be empty)",
          flush=True)


# Each source: (id, label, STEP path, hlr kwargs, pre-rotation, onshape ids)
#   pre_rotation: ((axis_x, axis_y, axis_z), angle_deg) or None.  Re-orients
#     the model so its long axis is world X and "up" is world Z - the frame
#     our STD_VIEWS are built around.
#   onshape: {"did", "wid", "eid"} for the assembly; enables the feature-tree
#     sidebar with click-to-highlight.  None = tree sidebar stays empty.
SOURCES = [
    ("siderail",  "Folding siderail",
     Path(r"C:\Users\FredMarshAccora\Downloads\P194-03-00 Folding siderail ASSE.STEP"),
     {"mesh_defl": 0.4, "sample_defl": 0.4},
     None,
     None),
    ("presto",    "Presto bed (top assembly)",
     HERE.parent / "step_lineart_test" / "presto_top_level.step",
     {"mesh_defl": 1.5, "sample_defl": 1.0},
     ((0, 1, 0), -90),
     {"did": "835e6bd90b01779d102c6244",
      "wid": "57594ac630641ef7dd431b7a",
      "eid": "41130e2363641e1fb1763b3b"}),
    ("contesa",   "Contesa V2 / FL8 (top assembly)",
     HERE / "contesa_top_level.step",
     # 61MB STEP - coarser tessellation to keep mesh memory reasonable
     {"mesh_defl": 3.0, "sample_defl": 1.5},
     # Native bbox: X=2153 (length), Y=1448 (height incl. headboard),
     # Z=1016 (width).  Contesa STEP is Y-up; rotate +90deg about X to
     # put height on world Z so the iso view comes out upright.
     ((1, 0, 0), 90),
     {"did": "b112cdaa5ec09a28f81ca7c7",
      "wid": "0c1fa64d6ea5b9f87d9bdb3e",
      "eid": "0a03a83f17a3c3550242614b"}),
]

# Per-view 2D HLR renders.  Each entry is (id, label, (x, y, z)) where the
# tuple is the camera direction in the model's (X=length, Z=up) frame.
#
# To add a new view: orbit in the 3D pane, click "copy view_dir" in the
# floating toolbar (puts a tuple on your clipboard), then paste a line here
# and rerun `python build_viewer.py`.  ONE file edit, no STD_VIEWS dance.
VIEWS = [
    ("iso",   "Iso 3/4 (front-right-above)", (-0.5, -1.0,  0.7)),
    ("front", "Front elevation",              ( 0.0, -1.0,  0.25)),
    ("side",  "Side elevation",               (-1.0,  0.0,  0.25)),
    # ("hero",  "Hero shot",                    (0.456, -0.234, 0.789)),
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


def _solid_mesh_arrays(solid):
    """Return (vertices Nx3, faces Mx3) numpy arrays for one TopoDS_Solid.

    Caller must have already called BRepMesh_IncrementalMesh on the parent
    shape (or this solid).  Vertices are in the solid's coordinate frame.
    """
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    vs, ts = [], []
    voff = 0
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                vs.append((p.X(), p.Y(), p.Z()))
            reversed_face = face.Orientation() == 1
            for i in range(1, tri.NbTriangles() + 1):
                t = tri.Triangle(i)
                a, b, c = t.Get()
                if reversed_face:
                    ts.append((voff + b - 1, voff + a - 1, voff + c - 1))
                else:
                    ts.append((voff + a - 1, voff + b - 1, voff + c - 1))
            voff += tri.NbNodes()
        exp.Next()
    if not vs or not ts:
        return None, None
    return np.array(vs, dtype=np.float32), np.array(ts, dtype=np.uint32)


def export_glb_b64(shape, mesh_defl):
    """Mesh every solid and pack into a GLB; return base64 string + summary.

    Each solid becomes a named scene node (``part_NNN``) so the WebGL
    viewer can highlight individual parts by name.  Mesh deflection is
    intentionally coarser than HLR's: the 3D view-finder only needs to be
    navigable, not print-quality.
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    BRepMesh_IncrementalMesh(shape, mesh_defl, False, 0.5, True)

    scene = trimesh.Scene()
    n_parts = 0
    n_tris = 0
    for idx, label, solid in split_solids(shape):
        v, t = _solid_mesh_arrays(solid)
        if v is None or len(v) == 0 or len(t) == 0:
            continue
        m = trimesh.Trimesh(vertices=v, faces=t, process=False)
        node_name = f"part_{idx:03d}"
        scene.add_geometry(m, node_name=node_name, geom_name=label)
        n_parts += 1
        n_tris += len(t)
    if n_parts == 0:
        return None, {"parts": 0, "tris": 0, "kb": 0}
    glb_bytes = scene.export(file_type="glb")
    b64 = base64.b64encode(glb_bytes).decode("ascii")
    return b64, {"parts": n_parts, "tris": n_tris,
                 "kb": len(glb_bytes) // 1024}


def fetch_onshape_tree(ids):
    """Pull the assembly instance tree from Onshape.

    Returns a nested list of dicts shaped like::

      [{"name": "...", "type": "Part" or "Assembly",
        "partId": "...", "children": [...]}, ...]

    Returns None on any failure (network, auth, missing client).  The
    viewer falls back to the empty tree gracefully.
    """
    if _OnshapeClient is None or ids is None:
        return None
    try:
        c = _OnshapeClient()
        did, wid, eid = ids["did"], ids["wid"], ids["eid"]
        # /assemblies/d/{d}/w/{w}/e/{e} returns the full definition with
        # instances + subAssemblies.  We don't recurse via the API; we
        # just walk the returned definition.
        asm = c.get(f"/assemblies/d/{did}/w/{wid}/e/{eid}",
                    params={"includeMateFeatures": "false",
                            "includeNonSolids": "false",
                            "includeMateConnectors": "false"})
        root = asm.get("rootAssembly") or {}
        sub_asms = {sa.get("elementId", "") + "/" + sa.get("documentId", ""): sa
                    for sa in (asm.get("subAssemblies") or [])}

        def build(instances):
            nodes = []
            for inst in instances or []:
                node = {
                    "name": inst.get("name") or inst.get("partId") or "?",
                    "type": inst.get("type", "Part"),
                    "part_id": inst.get("partId") or "",
                    "children": [],
                }
                if inst.get("type") == "Assembly":
                    sa_key = inst.get("elementId", "") + "/" + inst.get("documentId", "")
                    sa = sub_asms.get(sa_key)
                    if sa is not None:
                        node["children"] = build(sa.get("instances") or [])
                nodes.append(node)
            return nodes

        return build(root.get("instances") or [])
    except Exception as exc:
        print(f"  Onshape tree fetch failed: {type(exc).__name__}: {exc}",
              flush=True)
        return None


def generate_svgs():
    """Run per-solid HLR for every (file, view) pair, write tagged SVG.

    Returns metadata list: [{file_id, file_label, views: [...], parts: [...],
                              glb_b64, onshape_tree}]
    """
    catalogue = []
    for file_id, file_label, sp, hlr_kw, pre_rotate, onshape_ids in SOURCES:
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

        # 3D view-finder GLB.  Coarser deflection than HLR's so the inline
        # base64 blob stays manageable.
        glb_defl = max(hlr_kw.get("mesh_defl", 1.0) * 2.5, 4.0)
        t_glb = time.time()
        glb_b64, glb_info = export_glb_b64(shape, glb_defl)
        print(f"  glb defl={glb_defl}  parts={glb_info['parts']}  "
              f"tris={glb_info['tris']}  size={glb_info['kb']}KB  "
              f"({time.time()-t_glb:.1f}s)", flush=True)

        # Onshape feature tree (optional).
        tree = None
        if onshape_ids is not None:
            t_tree = time.time()
            tree = fetch_onshape_tree(onshape_ids)
            if tree is not None:
                print(f"  onshape tree: {_count_tree(tree)} nodes "
                      f"({time.time()-t_tree:.1f}s)", flush=True)

        file_entry = {
            "file_id": file_id,
            "file_label": file_label,
            "parts": solid_meta,
            "views": [],
            "glb_b64": glb_b64,
            "onshape_tree": tree,
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


def _count_tree(nodes):
    if not nodes:
        return 0
    return sum(1 + _count_tree(n.get("children") or []) for n in nodes)


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
  .webgl-wrap  {{ position: relative; overflow: hidden; background: white; }}
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
  <div class="seg-ctl" role="tablist" aria-label="Layout">
    <button id="lay-2d"    class="seg-btn active" title="2D drawing only">2D</button>
    <button id="lay-split" class="seg-btn"        title="2D + 3D side-by-side">Split</button>
    <button id="lay-3d"    class="seg-btn"        title="3D explore only">3D</button>
  </div>
  <span style="flex:1"></span>
  <button id="btn-annotate">+ callout</button>
  <button id="btn-clear">clear callouts</button>
  <button id="btn-export">export SVG</button>
</header>
<main>
  <aside class="left">
    <h2>Saved views</h2>
    <p style="font-size:11px; color: var(--muted); margin: 0 0 6px 0;">
      Camera angles you've saved for this source.</p>
    <div style="display:flex; gap:4px; margin-bottom:6px;">
      <input type="text" id="view-name" placeholder="name..."
             style="flex:1; padding:4px 6px; font-size:12px;
                    border:1px solid var(--line); border-radius:3px;">
      <button id="btn-save-view" title="Save current camera angle">save</button>
    </div>
    <ul id="saved-views" class="saved-views-list"></ul>

    <h2>Onshape tree</h2>
    <input type="search" id="tree-search" placeholder="filter tree..."
           autocomplete="off" spellcheck="false">
    <p style="font-size:11px; color: var(--muted); margin: 4px 0 8px 0;"
       id="tree-status">No tree for this source.</p>
    <ul class="tree-root" id="tree-root"></ul>
    <h2>Solids (STEP order)</h2>
    <p style="font-size:11px; color: var(--muted); margin: 0 0 8px 0;">
      Click a row to highlight. Click again to clear.</p>
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
  <div class="webgl-wrap" id="webgl-wrap">
    <canvas id="webgl-canvas"></canvas>
    <div class="three-toolbar">
      <button id="btn-generate" class="primary"
              title="Render an HLR SVG of the current camera angle and show it on the left. Requires the local server (python serve.py).">
        &#9889; generate 2D
      </button>
      <span class="tb-sep"></span>
      <span id="viewdir-readout">view_dir = (—, —, —)</span>
      <button id="btn-lock-view" title="Copy view_dir tuple to clipboard">copy view_dir</button>
      <button id="btn-reset-3d" title="Frame the model from the active 2D view direction">reset camera</button>
      <span class="tb-sep"></span>
      <label class="tb-label" title="Override what axis is 'up'. 3D-side preview only - paste the resulting tuple into SOURCES and rebuild to bake into 2D HLR.">Up:
        <select id="up-axis-sel">
          <option value="Z" selected>Z</option>
          <option value="Y">Y</option>
          <option value="X">X</option>
          <option value="-Z">-Z</option>
          <option value="-Y">-Y</option>
          <option value="-X">-X</option>
        </select>
      </label>
      <button id="btn-copy-orient" title="Copy pre_rotate tuple to clipboard">copy pre_rotate</button>
    </div>
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

    <h2>Selection styling</h2>
    <p style="font-size:11px; color: var(--muted); margin: 0 0 6px 0;">
      Properties applied to the currently-highlighted parts.</p>
    <div id="style-panel" style="font-size: 12px;">
      <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
        Stroke
        <input type="color" id="sty-stroke" value="#00836a" style="width:30px;">
      </label>
      <label style="display:flex; align-items:center; gap:6px; margin:4px 0;">
        Width
        <input type="range" id="sty-width" min="0.1" max="3" step="0.1"
               value="0.7" style="flex:1;">
        <span id="sty-width-val">0.7</span>
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
      <div style="display:flex; gap:4px; margin-top:6px; flex-wrap:wrap;">
        <button id="btn-apply-style" title="Apply to highlighted parts">apply</button>
        <button id="btn-reset-style" title="Clear style for highlighted parts">reset</button>
        <button id="btn-reset-all-style" title="Clear all style overrides for this source">reset all</button>
      </div>
      <div style="display:flex; gap:4px; margin-top:8px; flex-wrap:wrap;">
        <button id="btn-expand-parent" title="Add all siblings under the same Onshape Assembly to the selection">+ Onshape group</button>
        <button id="btn-cycle-deeper" title="In 3D mode, next click at the same pixel goes one layer deeper">depth-click ↻</button>
      </div>
    </div>

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
  const upStored = loadUpAxisFor(fileSel.value);
  window.IFU_VIEWER?.applyUpAxisOverride?.(UP_AXIS_ROT[upStored]);
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

function applyHighlights() {{
  const st = getState(fileSel.value, viewSel.value);
  const set = st.highlights || new Set();
  const any = set.size > 0;
  const svg = activeSvg();
  if (svg) {{
    svg.querySelectorAll('.part').forEach(p => {{
      const idx = parseInt(p.dataset.part);
      const hit = set.has(idx);
      p.classList.toggle('highlight', hit);
      p.classList.toggle('dim', any && !hit);
    }});
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
  window.IFU_VIEWER?.applyHighlights3D?.(set);
}}

// Esc clears selection
window.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape') clearHighlights();
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
  document.querySelectorAll('.svg-pane').forEach(p => p.classList.remove('active'));
  const pane = activePane();
  if (!pane) return;
  pane.classList.add('active');
  attachInteractivity(pane);
  refreshPartList();
  applyMode();
  // re-apply highlights (Set) onto the new pane's DOM
  applyHighlights();
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
  // positional leaf->solid map + parent-back-pointers
  _annotateParents(tree, null);
  const leaves = [];
  _flattenLeaves(tree, leaves);
  const partsById = new Map(fe.parts.map(p => [p.idx, p]));
  leaves.forEach((leaf, i) => {{
    leaf._mapped_idx = (i < fe.parts.length) ? fe.parts[i].idx : null;
  }});
  // Reverse map: solid idx -> tree node (used by "+ Onshape group")
  _leafByPartIdx = new Map();
  for (const leaf of leaves) {{
    if (leaf._mapped_idx != null) _leafByPartIdx.set(leaf._mapped_idx, leaf);
  }}
  treeStatus.textContent = `${{leaves.length}} part instances, mapped to ` +
    `${{Math.min(leaves.length, fe.parts.length)}} solids (positional).`;

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
      const idx = _tree_to_part_idx[id];
      if (idx == null) return;
      togglePartHighlight(idx, {{append: ev.ctrlKey || ev.metaKey}});
    }});
    return li;
  }}
  tree.forEach(n => treeRoot.appendChild(buildNode(n)));
}}

// Inject a freshly-rendered SVG (from the local server's /api/render) as a
// "live" view for the given source.  Per-source: each source has its own
// __live__ slot that gets overwritten on every generate.
function injectLiveSVG(file_id, view_dir, svgText) {{
  // Strip any XML prolog and stamp an id on the <svg> so existing helpers
  // (applyTransform / attachInteractivity) can find it.
  const cleaned = svgText
    .replace(/<\\?xml[^>]*\\?>\\s*/, '')
    .replace('<svg', `<svg id="svg_${{file_id}}___live__"`);

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

  // Add or update the "Live" option in the View dropdown (per-source).
  const fe = CATALOGUE.find(x => x.file_id === file_id);
  if (fe) {{
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
  }}
  // Refresh the View dropdown if this is the active source
  if (fileSel.value === file_id) {{
    refreshViews();
    viewSel.value = '__live__';
    refreshPane();
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
function applyStyleSheet() {{
  const fid = fileSel.value;
  const m = loadPartStyles(fid);
  let css = '';
  for (const [idx, st] of Object.entries(m)) {{
    const sel = `.svg-pane[data-file="${{fid}}"] svg .part.part-${{String(idx).padStart(3, '0')}} path`;
    const rules = [];
    if (st.stroke)  rules.push(`stroke: ${{st.stroke}}`);
    if (st.width != null)  rules.push(`stroke-width: ${{st.width}}`);
    if (st.opacity != null) rules.push(`opacity: ${{st.opacity}}`);
    if (st.dash)    rules.push(`stroke-dasharray: ${{st.dash}}`);
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
}}

$('sty-width').addEventListener('input', (e) => {{
  $('sty-width-val').textContent = parseFloat(e.target.value).toFixed(1);
}});
$('sty-opacity').addEventListener('input', (e) => {{
  $('sty-opacity-val').textContent = parseFloat(e.target.value).toFixed(2);
}});
$('btn-apply-style').addEventListener('click', () => {{
  const st = getState(fileSel.value, viewSel.value);
  if (!st.highlights || !st.highlights.size) {{
    alert('Select one or more parts first.');
    return;
  }}
  const style = {{
    stroke:  $('sty-stroke').value,
    width:   parseFloat($('sty-width').value),
    opacity: parseFloat($('sty-opacity').value),
    dash:    $('sty-dash').value || null,
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
    // gather every Part descendant of the parent assembly
    const siblings = [];
    _flattenLeaves([leaf._parent], siblings);
    for (const s of siblings) {{
      if (s._mapped_idx != null) newSel.add(s._mapped_idx);
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
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
import {{ GLTFLoader }} from 'three/addons/loaders/GLTFLoader.js';

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

let scene, camera, renderer, controls;
let loaded = new Map();      // file_id -> THREE.Group
let active = null;           // currently visible group
let partByName = new Map();  // "part_NNN" -> THREE.Object3D
let inited = false;

function init() {{
  if (inited) return;
  inited = true;

  scene = new THREE.Scene();
  scene.background = new THREE.Color(0xffffff);

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

  renderer = new THREE.WebGLRenderer({{ canvas, antialias: true }});
  renderer.setSize(r.width, r.height, false);
  renderer.setPixelRatio(window.devicePixelRatio || 1);

  scene.add(new THREE.AmbientLight(0xffffff, 0.7));
  const sun = new THREE.DirectionalLight(0xffffff, 0.55);
  sun.position.set(1, -1, 1.5);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xffffff, 0.25);
  fill.position.set(-1, 0.5, -0.5);
  scene.add(fill);

  controls = new OrbitControls(camera, canvas);
  controls.target.set(0, 0, 0);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.update();

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
}}

function animate() {{
  requestAnimationFrame(animate);
  if (!controls || !is3DVisible()) return;
  controls.update();
  // Pane width can change when entering Split; keep the renderer in sync.
  const r = canvas.getBoundingClientRect();
  if (renderer.domElement.width !== Math.round(r.width * (window.devicePixelRatio || 1)) ||
      renderer.domElement.height !== Math.round(r.height * (window.devicePixelRatio || 1))) {{
    resize();
  }}
  renderer.render(scene, camera);
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
  const b64 = GLB_B64[file_id];
  if (!b64) {{
    readout.textContent = '(no 3D mesh for this source)';
    return;
  }}
  const url = 'data:model/gltf-binary;base64,' + b64;
  const loader = new GLTFLoader();
  loader.load(url, (gltf) => {{
    const grp = gltf.scene;
    grp.traverse(obj => {{
      if (obj.isMesh) {{
        obj.material = new THREE.MeshLambertMaterial({{
          color: 0xe8e8ea, transparent: false, side: THREE.DoubleSide,
          polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
        }});
        // crease edges only (>=30deg dihedral) for a Composer-ish look
        const edges = new THREE.EdgesGeometry(obj.geometry, 30);
        const lines = new THREE.LineSegments(
          edges,
          new THREE.LineBasicMaterial({{ color: 0x000000, linewidth: 1 }})
        );
        lines.userData.isEdge = true;
        obj.add(lines);
        obj.userData.baseColor = 0xe8e8ea;
      }}
    }});
    loaded.set(file_id, grp);
    scene.add(grp);
    active = grp;
    indexParts(grp);
    const upRot = window.IFU_VIEWER?.getActiveUpAxis?.();
    if (upRot) applyUpAxisOverride(upRot); else frame(grp);
  }}, undefined, (err) => {{
    console.error('GLB load failed', err);
    readout.textContent = '(GLB load failed - see console)';
  }});
}}

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

function applyHighlights3D(set) {{
  if (!active) return;
  const any = set && set.size > 0;
  active.traverse(o => {{
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    const hit = any && idx != null && set.has(idx);
    o.material.color.setHex(hit ? 0x00836a : 0xe8e8ea);
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

// Expose for the classic script's selection + orientation + layout handlers.
window.IFU_VIEWER.applyHighlights3D = applyHighlights3D;
window.IFU_VIEWER.applyUpAxisOverride = (rot) => {{
  applyUpAxisOverride(rot);
}};
window.IFU_VIEWER.set3DActive = set3DActive;
window.IFU_VIEWER.getCurrentViewDir = () => {{
  if (!camera || !controls) return null;
  const d = camera.position.clone().sub(controls.target).normalize();
  return [d.x, d.y, d.z];
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
// http://localhost:5000/ or a file:// build.
const API_BASE = (location.protocol === 'http:' || location.protocol === 'https:')
  ? ''
  : 'http://localhost:5000';

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
    window.IFU_VIEWER.injectLiveSVG(fid, view_dir, svgText);
    // Auto-switch to Split so the new SVG appears on the left next to the 3D
    window.IFU_VIEWER.setLayout?.('split');
    btnGen.innerHTML = `&#10003; ${{elapsed}}s`;
    if (breakdown) {{
      readout.title = `last render: ${{elapsed}}s -- ${{breakdown}}`;
      console.log(`[generate] ${{elapsed}}s -- ${{breakdown}}`);
    }}
  }} catch (e) {{
    console.error('generate failed:', e);
    btnGen.innerHTML = '&#10007; ' + (e.message || 'render failed');
  }} finally {{
    controls.enabled = true;
    setTimeout(() => {{ btnGen.disabled = false; btnGen.innerHTML = orig; }}, 2500);
  }}
}}

btnGen.addEventListener('click', generateLiveSVG);

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
}});
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
