"""Source -> SVG pipeline orchestrator.

For each ``SOURCES`` entry: import the STEP, pre-rotate to the
common (X=length, Z=up) frame, mesh + GLB-export for the 3D pane,
fetch the part hierarchy (Onshape API first, STEP fallback), then
run per-solid HLR for every view in ``VIEWS`` (filtered by
``SOURCE_VIEW_SUBSET``).  Emits part-tagged SVG and returns the
catalogue describing what was produced.
"""
from __future__ import annotations
import time
import cadquery as cq

from t5_hlr_vector import (run_hlr_per_solid, write_svg_parts,
                            split_solids, rotate_shape)

from .config import (SOURCES, VIEWS, SOURCE_VIEW_SUBSET,
                       SOURCE_SKIP_CATEGORIES, OUT)
from .glb import export_glb_b64
from .onshape_tree import fetch_onshape_tree
from .step_tree import fetch_step_tree, count_tree


def generate_svgs() -> list[dict]:
    """Run per-solid HLR for every (file, view) pair, write tagged SVG.

    Returns the catalogue list::

        [{file_id, file_label, parts: [...], views: [...],
          glb_b64, onshape_tree}, ...]
    """
    catalogue = []
    for file_id, file_label, sp, hlr_kw, pre_rotate, onshape_ids in SOURCES:
        if not sp.exists():
            print(f"  missing: {sp}")
            continue
        print(f"\n===== {file_label} =====", flush=True)
        t0 = time.time()
        shape = cq.importers.importStep(str(sp)).val().wrapped
        print(f"  load {time.time()-t0:.1f}s", flush=True)
        if pre_rotate is not None:
            axis, angle = pre_rotate
            shape = rotate_shape(shape, axis, angle)
            print(f"  pre-rotated {angle} deg about {axis}", flush=True)
        _log_bbox(shape)

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

        tree = None
        if onshape_ids is not None:
            t_tree = time.time()
            tree = fetch_onshape_tree(onshape_ids)
            if tree is not None:
                print(f"  onshape tree: {count_tree(tree)} nodes "
                      f"({time.time()-t_tree:.1f}s)", flush=True)
        if tree is None:
            t_tree = time.time()
            tree = fetch_step_tree(sp)
            if tree is not None:
                print(f"  STEP tree: {count_tree(tree)} nodes "
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


def _log_bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    bb = Bnd_Box()
    BRepBndLib.Add_s(shape, bb)
    try:
        xmin, ymin, zmin, xmax, ymax, zmax = bb.Get()
        print(f"  bbox  X[{xmin:.0f}..{xmax:.0f}={xmax-xmin:.0f}]  "
              f"Y[{ymin:.0f}..{ymax:.0f}={ymax-ymin:.0f}]  "
              f"Z[{zmin:.0f}..{zmax:.0f}={zmax-zmin:.0f}]", flush=True)
    except Exception as e:
        print(f"  bbox failed: {e}", flush=True)
