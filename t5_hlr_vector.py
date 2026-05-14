"""t5: Composer-style analytical HLR + vector SVG output.

Uses OCCT HLRBRep_PolyAlgo to compute view-dependent hidden-line removal
analytically on the triangulated B-rep. Each edge of the model is classified
by visibility (visible/hidden) and category (silhouette/sharp/smooth) - the
same buckets Composer's "Profile Lines / Sharp Edges / Smooth Outlines"
expose.

Output:
  *.svg - vector, publication-clean at any zoom (the real deliverable)
  *.png - PIL rasterisation of the same paths for inline preview

Modes:
  smart    silhouettes + sharp; smooth (fillet tangents) suppressed
           - this is Composer's "Smart Outlines" default look
  detailed silhouettes + sharp + thin smooth tangent lines
  hidden   smart + dashed hidden-sharp + dashed hidden-silhouette
           (engineering / assembly-instruction look)
"""
from __future__ import annotations
import time
import math
from pathlib import Path
import cadquery as cq
from OCP.HLRBRep import (HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape,
                          HLRBRep_Algo, HLRBRep_HLRToShape)
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.TopAbs import TopAbs_SOLID
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound
from OCP.gp import gp_Trsf, gp_Ax1
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir, gp_Vec
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopoDS import TopoDS
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_UniformDeflection
from PIL import Image, ImageDraw


# Edge category style defaults (IFU print weights, in mm at 1:1 SVG)
DEFAULT_STYLES = {
    "outline_v":  dict(stroke="#000000", width=0.70, dash=None),
    "sharp_v":    dict(stroke="#000000", width=0.30, dash=None),
    "smooth_v":   dict(stroke="#7a7a7a", width=0.20, dash=None),
    "hidden_sharp":   dict(stroke="#808080", width=0.20, dash="2 1.5"),
    "hidden_outline": dict(stroke="#808080", width=0.30, dash="3 2"),
}

# Which categories appear in each mode
MODE_CATEGORIES = {
    "smart":    ["outline_v", "sharp_v"],
    "detailed": ["outline_v", "sharp_v", "smooth_v"],
    "hidden":   ["hidden_outline", "hidden_sharp", "outline_v", "sharp_v"],
}


def rotate_shape(shape, axis_dir, angle_deg, origin=(0, 0, 0)):
    """Rotate a TopoDS_Shape in place around an axis (world frame)."""
    if angle_deg == 0:
        return shape
    trsf = gp_Trsf()
    axis = gp_Ax1(gp_Pnt(*origin), gp_Dir(*axis_dir))
    trsf.SetRotation(axis, math.radians(angle_deg))
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def build_projector(view_dir, focal=(0, 0, 0)):
    """Build an HLR projector matching the VTK iso convention.

    view_dir = camera position direction relative to focal point (eye - focal).
    The HLR Ax2's Z is the *projection direction* = scene -> camera, i.e. -cam.
    X is the image-right axis; chosen so world +X projects to image right.

    Returns (projector, x_axis_tuple, y_axis_tuple, focal_tuple) so callers
    can project arbitrary 3D points to projection (u, v) coords for
    bbox-based source tagging.
    """
    cam = gp_Vec(*view_dir)
    cam.Normalize()
    # projection direction = -camera (camera looks toward scene origin)
    proj_v = gp_Vec(-cam.X(), -cam.Y(), -cam.Z())
    z_dir = gp_Dir(proj_v.X(), proj_v.Y(), proj_v.Z())

    if abs(cam.Z()) < 0.95:
        up = gp_Vec(0, 0, 1)
    else:
        up = gp_Vec(0, 1, 0)
    # camera_x = up x proj = up x (-cam) = cam x up   (right-handed, world +Z up)
    x_vec = cam.Crossed(up)
    x_vec.Normalize()
    x_dir = gp_Dir(x_vec.X(), x_vec.Y(), x_vec.Z())
    ax = gp_Ax2(gp_Pnt(*focal), z_dir, x_dir)
    # camera_y = z x x = proj_v x x_vec
    y_vec = proj_v.Crossed(x_vec)
    y_vec.Normalize()
    return (HLRAlgo_Projector(ax),
            (x_vec.X(), x_vec.Y(), x_vec.Z()),
            (y_vec.X(), y_vec.Y(), y_vec.Z()),
            tuple(focal))


def sample_edges(compound, deflection):
    polylines = []
    if compound is None or compound.IsNull():
        return polylines
    exp = TopExp_Explorer(compound, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        try:
            ad = BRepAdaptor_Curve(edge)
            samp = GCPnts_UniformDeflection(ad, deflection)
            if samp.IsDone() and samp.NbPoints() >= 2:
                pts = [(samp.Value(i).X(), samp.Value(i).Y())
                       for i in range(1, samp.NbPoints() + 1)]
                polylines.append(pts)
        except Exception:
            pass
        exp.Next()
    return polylines


def _polyline_length(pl):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(pl, pl[1:]))


def filter_outliers(polylines, ratio_threshold=3.0, max_passes=4):
    """Drop polylines whose length is > ratio_threshold * the next-largest.

    Exact HLRBRep_Algo on complex assemblies sometimes emits a single
    spurious outline polyline an order of magnitude larger than any real
    geometry edge. One ratio pass kills the classic artifact; we run up to
    a few passes to catch stacked artefacts.
    """
    out = list(polylines)
    for _ in range(max_passes):
        if len(out) < 2:
            break
        lens = sorted(((_polyline_length(pl), i) for i, pl in enumerate(out)),
                      reverse=True)
        if lens[0][0] <= lens[1][0] * ratio_threshold:
            break
        bad_idx = lens[0][1]
        out = [pl for i, pl in enumerate(out) if i != bad_idx]
    return out


def _run_poly(shape, proj, mesh_defl, per_solid=False):
    BRepMesh_IncrementalMesh(shape, mesh_defl, False, 0.5, True)
    algo = HLRBRep_PolyAlgo()
    if per_solid:
        # Workaround for PolyAlgo's Standard_OutOfRange bug on some multi-part
        # compounds: add each solid separately.
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        added = 0
        while exp.More():
            try:
                algo.Load(exp.Current())
                added += 1
            except Exception:
                pass
            exp.Next()
        if added == 0:
            algo.Load(shape)
    else:
        algo.Load(shape)
    algo.Projector(proj)
    algo.Update()
    extr = HLRBRep_PolyHLRToShape()
    extr.Update(algo)
    return extr


def _run_exact(shape, proj):
    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(proj)
    algo.Update()
    algo.Hide()
    return HLRBRep_HLRToShape(algo)


def _extract_categories(extr, sample_defl):
    getters = {
        "sharp_v":        extr.VCompound,
        "outline_v":      extr.OutLineVCompound,
        "smooth_v":       extr.Rg1LineVCompound,
        "hidden_sharp":   extr.HCompound,
        "hidden_outline": extr.OutLineHCompound,
    }
    return {name: sample_edges(g(), sample_defl) for name, g in getters.items()}


def run_hlr(shape, view_dir, focal=(0, 0, 0),
            mesh_defl=0.4, sample_defl=0.3, exact=False):
    """Single-shape HLR (whole assembly together).

    Falls back PolyAlgo (compound) -> PolyAlgo (per-solid) -> exact Algo.
    Returns dict of category -> list of polylines.
    """
    proj, _x, _y, _f = build_projector(view_dir, focal)
    extr = None
    if not exact:
        try:
            extr = _run_poly(shape, proj, mesh_defl, per_solid=False)
        except Exception as e:
            print(f"  PolyAlgo (compound) failed ({type(e).__name__}); "
                  f"trying per-solid")
            try:
                extr = _run_poly(shape, proj, mesh_defl, per_solid=True)
            except Exception as e2:
                print(f"  PolyAlgo (per-solid) failed ({type(e2).__name__}); "
                      f"falling back to exact HLR")
    if extr is None:
        extr = _run_exact(shape, proj)
    polys = _extract_categories(extr, sample_defl)
    n_before = len(polys["outline_v"])
    polys["outline_v"] = filter_outliers(polys["outline_v"])
    if len(polys["outline_v"]) != n_before:
        print(f"  filtered {n_before - len(polys['outline_v'])} outlier outlines")
    return polys


def split_solids(shape):
    """Yield (index, label, TopoDS_Solid) for every solid in the compound."""
    solids = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    i = 0
    while exp.More():
        try:
            s = TopoDS.Solid_s(exp.Current())
            solids.append((i, f"part_{i:03d}", s))
            i += 1
        except Exception:
            pass
        exp.Next()
    return solids


def _build_full_hlr(shape, solids, proj, mesh_defl):
    """Run a SINGLE assembly-wide HLR by adding the COMPOUND (not each solid
    individually).  After Update+Hide, extr.VCompound(sub_solid) still works
    to extract per-solid edges - tested.

    Three tries in order:
      1. PolyAlgo on the whole compound (fastest path; works on most assemblies)
      2. PolyAlgo with each solid loaded separately (works around
         Standard_OutOfRange on compound-load for complex assemblies; still
         multi-solid so cross-solid occlusion is preserved)
      3. Exact HLRBRep_Algo (correct fallback, 3-5x slower than PolyAlgo)

    Returns (extr, kind) where kind in {"poly", "poly_per_solid", "exact"}.
    """
    # 1) PolyAlgo - compound add
    try:
        BRepMesh_IncrementalMesh(shape, mesh_defl, False, 0.5, True)
        algo = HLRBRep_PolyAlgo()
        algo.Load(shape)
        algo.Projector(proj)
        algo.Update()
        extr = HLRBRep_PolyHLRToShape()
        extr.Update(algo)
        return extr, "poly"
    except Exception as e:
        print(f"  PolyAlgo (compound) failed ({type(e).__name__}); "
              f"trying per-solid PolyAlgo")

    # 2) PolyAlgo - per-solid add (workaround for Standard_OutOfRange on
    #    multi-part compounds; PolyAlgo internally indexes faces per-shape,
    #    and Loading individual solids one at a time sidesteps the bug
    #    while still feeding ALL solids into the same algo instance so
    #    occlusion between them is computed correctly).
    try:
        BRepMesh_IncrementalMesh(shape, mesh_defl, False, 0.5, True)
        algo = HLRBRep_PolyAlgo()
        added = 0
        for _, _, solid in solids:
            try:
                algo.Load(solid)
                added += 1
            except Exception:
                pass
        if added > 0:
            algo.Projector(proj)
            algo.Update()
            extr = HLRBRep_PolyHLRToShape()
            extr.Update(algo)
            return extr, "poly_per_solid"
    except Exception as e:
        print(f"  PolyAlgo (per-solid) failed ({type(e).__name__}); "
              f"falling back to exact HLRBRep_Algo")

    # 3) Exact path - compound add
    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(proj)
    algo.Update()
    algo.Hide()
    extr = HLRBRep_HLRToShape(algo)
    return extr, "exact"


def run_hlr_per_solid(shape, view_dir, focal=(0, 0, 0),
                       mesh_defl=0.4, sample_defl=0.3,
                       exact=False, max_solids=None,
                       progress=True):
    """Assembly-wide HLR with per-solid tagging.

    Adds every solid separately to ONE HLR algo, so cross-part occlusion is
    correct (a small bracket behind a panel disappears, exactly as Composer
    does it). After Update+Hide, calls VCompound(solid) etc. for each solid
    to extract only THAT solid's visible/hidden edges, correctly occluded by
    all the others.

    Returns:
      parts: list of dicts with {idx, label, polys: {category: [polyline]}}
    """
    proj, x_axis, y_axis, focal_pt = build_projector(view_dir, focal)
    solids = split_solids(shape)
    if max_solids:
        solids = solids[:max_solids]
    if progress:
        print(f"  assembly-wide HLR over {len(solids)} solids...",
              flush=True)

    t0 = time.time()
    extr, kind = _build_full_hlr(shape, solids, proj, mesh_defl)
    if progress:
        print(f"  HLR ({kind}) done in {time.time()-t0:.1f}s; "
              f"extracting per-solid...", flush=True)

    cat_getters = (
        ("sharp_v",        "VCompound"),
        ("outline_v",      "OutLineVCompound"),
        ("smooth_v",       "Rg1LineVCompound"),
        ("hidden_sharp",   "HCompound"),
        ("hidden_outline", "OutLineHCompound"),
    )

    if kind in ("poly", "poly_per_solid"):
        # PolyAlgo: per-shape selector works directly
        parts = []
        t1 = time.time()
        for idx, label, solid in solids:
            polys = {}
            for cat, getter_name in cat_getters:
                try:
                    compound = getattr(extr, getter_name)(solid)
                    polys[cat] = sample_edges(compound, sample_defl)
                except Exception:
                    polys[cat] = []
            polys["outline_v"] = filter_outliers(polys["outline_v"])
            non_empty = any(len(v) for v in polys.values())
            if non_empty:
                parts.append({"idx": idx, "label": label, "polys": polys})
            if progress and (idx + 1) % 50 == 0:
                print(f"    ...{idx + 1}/{len(solids)}  "
                      f"({time.time()-t1:.1f}s extract)", flush=True)
        n_total, n_kept, n_degen = _dedup_polylines_in_place(parts)
        if progress:
            print(f"  per-solid extract done {time.time()-t1:.1f}s  "
                  f"({len(parts)} parts with edges; "
                  f"deduped {n_total}->{n_kept}, dropped {n_degen} degenerate)",
                  flush=True)
        return parts

    # exact Algo: per-shape selector returns nothing when compound was added.
    # Strategy: extract assembly-wide polylines per category, then tag each
    # polyline by which solid's projected bbox contains its centroid.
    if progress:
        print("  bbox-based source tagging (exact-Algo path)...", flush=True)
    t1 = time.time()
    all_polys = {}
    for cat, getter_name in cat_getters:
        try:
            compound = getattr(extr, getter_name)()
            all_polys[cat] = sample_edges(compound, sample_defl)
        except Exception:
            all_polys[cat] = []
    all_polys["outline_v"] = filter_outliers(all_polys["outline_v"])
    n_total = sum(len(v) for v in all_polys.values())
    if progress:
        print(f"  extracted {n_total} polylines in {time.time()-t1:.1f}s",
              flush=True)

    # Project each solid's 3D bbox to (u, v) plane
    t2 = time.time()
    solid_bboxes_2d = _project_solid_bboxes(solids, x_axis, y_axis, focal_pt)
    parts = _tag_by_bbox(all_polys, solid_bboxes_2d)
    nT, nK, nD = _dedup_polylines_in_place(parts)
    if progress:
        print(f"  bbox-tagged {n_total} polylines into {len(parts)} parts "
              f"in {time.time()-t2:.1f}s (deduped {nT}->{nK}, "
              f"dropped {nD} degenerate)", flush=True)
    return parts


def _dedup_polylines_in_place(parts, precision=1):
    """Remove duplicate polylines (across all parts) and drop degenerate ones.

    HLR on a multi-solid compound returns shared edges once per neighbouring
    solid -- so the same outline edge between two adjacent panels appears in
    both solids' extracted polyline lists.  Without dedup the SVG draws each
    shared edge 2x, 6x, even 10x on top of itself.  Effects: bloated file
    size, dark overdraw artifacts on edges that should be hairline (the
    "big circle" in dense viewports), and slower interactive pan/zoom.

    Dedup key is the rounded coordinate sequence so floating-point jitter
    between extractions doesn't defeat the comparison.  Within-part order
    is preserved.
    """
    seen = set()
    n_total = 0
    n_kept = 0
    n_degen = 0
    for part in parts:
        for cat in list(part.get("polys", {}).keys()):
            pls = part["polys"][cat]
            n_total += len(pls)
            kept = []
            for pl in pls:
                # drop degenerate (0 or 1 distinct points)
                if len(pl) < 2:
                    n_degen += 1
                    continue
                rounded = tuple(
                    (round(x, precision), round(y, precision)) for x, y in pl
                )
                if len(set(rounded)) < 2:
                    n_degen += 1
                    continue
                # canonicalise: same polyline traversed in reverse counts as same
                key_fwd = rounded
                key_rev = rounded[::-1]
                key = min(key_fwd, key_rev)
                if key in seen:
                    continue
                seen.add(key)
                kept.append(pl)
            part["polys"][cat] = kept
            n_kept += len(kept)
    return n_total, n_kept, n_degen


def _project_solid_bboxes(solids, x_axis, y_axis, focal_pt):
    """Return list of (idx, label, u_min, v_min, u_max, v_max) per solid."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    out = []
    fx, fy, fz = focal_pt
    ax_x, ay_x, az_x = x_axis
    ax_y, ay_y, az_y = y_axis
    for idx, label, solid in solids:
        bb = Bnd_Box()
        try:
            BRepBndLib.Add_s(solid, bb)
            xmin, ymin, zmin, xmax, ymax, zmax = bb.Get()
        except Exception:
            continue
        # 8 corners
        us = []
        vs = []
        for x in (xmin, xmax):
            for y in (ymin, ymax):
                for z in (zmin, zmax):
                    dx = x - fx; dy = y - fy; dz = z - fz
                    us.append(dx * ax_x + dy * ay_x + dz * az_x)
                    vs.append(dx * ax_y + dy * ay_y + dz * az_y)
        out.append((idx, label, min(us), min(vs), max(us), max(vs)))
    return out


def _tag_by_bbox(polys_by_cat, solid_bboxes_2d):
    """Tag each polyline by which solid's projected bbox contains its centroid.

    On multi-match: pick the smallest-area bbox (most specific part).
    On no match: assign to the NEAREST bbox ONLY if the centroid is within
    one bbox-diagonal of the model envelope; otherwise drop the polyline
    entirely.  HLR on complex assemblies sometimes emits spurious arcs /
    silhouettes far from any real part; the legacy "nearest" fallback was
    pulling these in and they were dominating the SVG viewBox.
    """
    parts_map = {}
    # Pre-compute areas + overall model envelope
    bb_arr = [(idx, label, u0, v0, u1, v1, (u1 - u0) * (v1 - v0))
              for idx, label, u0, v0, u1, v1 in solid_bboxes_2d]
    if bb_arr:
        env_u0 = min(b[2] for b in bb_arr)
        env_v0 = min(b[3] for b in bb_arr)
        env_u1 = max(b[4] for b in bb_arr)
        env_v1 = max(b[5] for b in bb_arr)
        env_diag = ((env_u1 - env_u0) ** 2 + (env_v1 - env_v0) ** 2) ** 0.5
        max_fallback_dist = env_diag * 0.05    # 5% of envelope diagonal
    else:
        max_fallback_dist = 0.0

    n_dropped = 0
    for cat, polylines in polys_by_cat.items():
        for pl in polylines:
            if not pl:
                continue
            n = len(pl)
            cx = sum(p[0] for p in pl) / n
            cy = sum(p[1] for p in pl) / n
            best_idx = None
            best_label = "unknown"
            best_area = float("inf")
            for idx, label, u0, v0, u1, v1, area in bb_arr:
                if u0 <= cx <= u1 and v0 <= cy <= v1 and area < best_area:
                    best_idx = idx
                    best_label = label
                    best_area = area
            if best_idx is None:
                # No containing bbox: only fall back to the nearest if the
                # polyline is near the model envelope.  Far-away polylines
                # are spurious HLR output and are dropped.
                best_dist = float("inf")
                near_idx = None
                near_label = "unknown"
                for idx, label, u0, v0, u1, v1, _a in bb_arr:
                    bcx = (u0 + u1) / 2.0
                    bcy = (v0 + v1) / 2.0
                    d = ((bcx - cx) ** 2 + (bcy - cy) ** 2) ** 0.5
                    if d < best_dist:
                        best_dist = d
                        near_idx = idx
                        near_label = label
                if best_dist <= max_fallback_dist:
                    best_idx = near_idx
                    best_label = near_label
                else:
                    n_dropped += 1
                    continue   # drop spurious polyline
            p = parts_map.setdefault(best_idx, {
                "idx": best_idx, "label": best_label, "polys": {}})
            p["polys"].setdefault(cat, []).append(pl)
    if n_dropped:
        print(f"  dropped {n_dropped} polylines outside the model envelope")
    return list(parts_map.values())


def merge_parts(parts):
    """Concatenate per-solid polylines into assembly-wide buckets, dropping
    part identity. Used when the caller doesn't need tagging."""
    out = {k: [] for k in DEFAULT_STYLES}
    for p in parts:
        for cat, pls in p["polys"].items():
            out.setdefault(cat, []).extend(pls)
    return out


def parts_bbox(parts, categories):
    xs, ys = [], []
    for p in parts:
        for cat in categories:
            for pl in p["polys"].get(cat, []):
                for x, y in pl:
                    xs.append(x); ys.append(y)
    if not xs:
        return (-100, -100, 100, 100)
    return (min(xs), min(ys), max(xs), max(ys))


def polyline_bbox(polylines_by_cat):
    xs, ys = [], []
    for pls in polylines_by_cat.values():
        for pl in pls:
            for x, y in pl:
                xs.append(x); ys.append(y)
    if not xs:
        return (-100, -100, 100, 100)
    return (min(xs), min(ys), max(xs), max(ys))


def write_svg_parts(parts, out_path: Path,
                     categories=("hidden_outline", "hidden_sharp",
                                  "smooth_v", "sharp_v", "outline_v"),
                     styles=None, pad_frac=0.04,
                     extra_attrs="",
                     precision=1,
                     skip_categories=()):
    """Part-aware SVG: every visible category becomes a layer; inside each
    layer, polylines are grouped by part with data-part and class hooks for
    interactive highlighting.

    Layers are ordered (back to front):
      hidden_outline -> hidden_sharp -> smooth_v -> sharp_v -> outline_v
    So the heavy silhouette always sits on top.
    """
    styles = {**DEFAULT_STYLES, **(styles or {})}
    active_cats = [c for c in categories if c not in skip_categories]
    x0, y0, x1, y1 = parts_bbox(parts, active_cats)
    w_mm = x1 - x0; h_mm = y1 - y0
    pad = max(w_mm, h_mm) * pad_frac
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    w_mm = x1 - x0; h_mm = y1 - y0
    fmt = f"%.{precision}f"

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'viewBox="{x0:.3f} {-y1:.3f} {w_mm:.3f} {h_mm:.3f}" '
             f'width="100%" height="100%" '
             f'preserveAspectRatio="xMidYMid meet"{extra_attrs}>',
             '<g transform="scale(1,-1)" fill="none" '
             'stroke-linecap="round" stroke-linejoin="round">']

    for cat in active_cats:
        st = styles[cat]
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        lines.append(
            f'<g class="layer layer-{cat}" stroke="{st["stroke"]}" '
            f'stroke-width="{st["width"]:.3f}"{dash}>'
        )
        for p in parts:
            pls = p["polys"].get(cat, [])
            if not pls:
                continue
            lines.append(
                f'<g class="part part-{p["idx"]:03d}" '
                f'data-part="{p["idx"]}" data-label="{p["label"]}">'
            )
            for pl in pls:
                d = "M " + " L ".join(f"{fmt % x} {fmt % y}" for x, y in pl)
                lines.append(f'<path d="{d}"/>')
            lines.append('</g>')
        lines.append('</g>')

    # Hit-area layer: invisible thick strokes per part for reliable clicking.
    # Visible IFU strokes are 0.2-0.7 mm wide; clicking on hair-thin lines is
    # near-impossible. This layer sits on top with stroke-opacity=0 and a
    # 3 mm "fat" stroke so any click within ~1.5 mm of a part's outline still
    # registers as that part. pointer-events="stroke" is needed because the
    # default ("visiblePainted") ignores fully-transparent paint.
    HIT_STROKE_MM = 3.0
    lines.append(
        f'<g class="layer layer-hit" pointer-events="stroke" '
        f'stroke="#000" stroke-opacity="0" stroke-width="{HIT_STROKE_MM}" '
        f'fill="none">'
    )
    for p in parts:
        all_polys = []
        for cat in active_cats:
            all_polys.extend(p["polys"].get(cat, []))
        if not all_polys:
            continue
        lines.append(
            f'<g class="part part-{p["idx"]:03d}" '
            f'data-part="{p["idx"]}" data-label="{p["label"]}">'
        )
        for pl in all_polys:
            d = "M " + " L ".join(f"{fmt % x} {fmt % y}" for x, y in pl)
            lines.append(f'<path d="{d}"/>')
        lines.append('</g>')
    lines.append('</g>')

    lines.append('</g></svg>')
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return (x0, y0, x1, y1)


def write_svg(polylines_by_cat, out_path: Path, mode="smart",
              styles=None, pad_frac=0.04):
    styles = {**DEFAULT_STYLES, **(styles or {})}
    cats = MODE_CATEGORIES[mode]

    x0, y0, x1, y1 = polyline_bbox({k: polylines_by_cat[k] for k in cats
                                    if k in polylines_by_cat})
    w_mm = x1 - x0
    h_mm = y1 - y0
    pad = max(w_mm, h_mm) * pad_frac
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    w_mm = x1 - x0; h_mm = y1 - y0

    # SVG Y axis points DOWN; projection Y axis points UP. Flip via viewBox.
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{x0:.3f} {-y1:.3f} {w_mm:.3f} {h_mm:.3f}" '
        f'width="{w_mm:.2f}mm" height="{h_mm:.2f}mm">'
    )
    lines.append('<g transform="scale(1,-1)" fill="none" '
                 'stroke-linecap="round" stroke-linejoin="round">')

    for cat in cats:
        if cat not in polylines_by_cat:
            continue
        st = styles[cat]
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        lines.append(f'<g stroke="{st["stroke"]}" '
                     f'stroke-width="{st["width"]:.3f}"{dash}>')
        for pl in polylines_by_cat[cat]:
            d = "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in pl)
            lines.append(f'<path d="{d}"/>')
        lines.append('</g>')

    lines.append('</g></svg>')
    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_png(polylines_by_cat, out_path: Path, mode="smart",
              styles=None, width=3000, height=None, pad_frac=0.04,
              bg="white"):
    styles = {**DEFAULT_STYLES, **(styles or {})}
    cats = MODE_CATEGORIES[mode]
    x0, y0, x1, y1 = polyline_bbox({k: polylines_by_cat[k] for k in cats
                                    if k in polylines_by_cat})
    w_mm = x1 - x0; h_mm = y1 - y0
    pad = max(w_mm, h_mm) * pad_frac
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
    w_mm = x1 - x0; h_mm = y1 - y0

    px_per_mm = width / w_mm
    if height is None:
        height = int(round(h_mm * px_per_mm))

    # Supersample for cleaner lines, then downscale
    ss = 2
    W, H = width * ss, height * ss
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    def to_px(x, y):
        # flip Y (image is top-down, projection is bottom-up)
        px = (x - x0) * px_per_mm * ss
        py = (y1 - y) * px_per_mm * ss
        return (px, py)

    for cat in cats:
        if cat not in polylines_by_cat:
            continue
        st = styles[cat]
        w_px = max(1, int(round(st["width"] * px_per_mm * ss)))
        # PIL doesn't support stroke-dasharray. For dashed categories,
        # chop polylines into dashed segments.
        if st["dash"]:
            dash_pat = [float(v) for v in st["dash"].split()]
            for pl in polylines_by_cat[cat]:
                dash_polyline(draw, pl, to_px, dash_pat, st["stroke"], w_px)
        else:
            for pl in polylines_by_cat[cat]:
                pts = [to_px(*p) for p in pl]
                draw.line(pts, fill=st["stroke"], width=w_px, joint="curve")

    img = img.resize((width, height), Image.LANCZOS)
    img.save(out_path)
    return out_path


def dash_polyline(draw, polyline, to_px, dash_pat_mm, color, width_px):
    """Walk polyline, alternating draw/skip segments per dash pattern (mm)."""
    if len(polyline) < 2:
        return
    # Compute pixel-space dash from mm pattern. dash_pat is in viewport mm.
    pts_px = [to_px(*p) for p in polyline]
    # cumulative length
    segs = []
    total = 0.0
    for (x0, y0), (x1, y1) in zip(pts_px, pts_px[1:]):
        seg_len = math.hypot(x1 - x0, y1 - y0)
        segs.append((x0, y0, x1, y1, seg_len))
        total += seg_len
    # dash_pat_mm needs to be in px - we can't recover the scale here so we use
    # a heuristic: assume dash unit ~ width_px * 4. PIL still gives a dashed
    # appearance, just not exactly viewport mm.
    on = max(2.0, width_px * 5.0)
    off = max(1.5, width_px * 3.0)
    if len(dash_pat_mm) >= 2:
        ratio = dash_pat_mm[0] / dash_pat_mm[1]
        on = max(2.0, width_px * 5.0 * (ratio / 1.3))
    cursor = 0.0
    draw_on = True
    pos = 0.0
    seg_i = 0
    while seg_i < len(segs) and pos <= total:
        x0, y0, x1, y1, slen = segs[seg_i]
        within = pos - sum(s[4] for s in segs[:seg_i])
        chunk = on if draw_on else off
        avail = slen - within
        if avail <= 0:
            seg_i += 1; continue
        take = min(chunk - 0, avail)
        t0 = within / slen
        t1 = (within + take) / slen
        ax = x0 + (x1 - x0) * t0; ay = y0 + (y1 - y0) * t0
        bx = x0 + (x1 - x0) * t1; by = y0 + (y1 - y0) * t1
        if draw_on:
            draw.line([(ax, ay), (bx, by)], fill=color, width=width_px)
        pos += take
        if take >= chunk:
            draw_on = not draw_on
        if within + take >= slen:
            seg_i += 1


def run_per_solid(step_path: Path, out_dir: Path, view_name: str, view_dir,
                   mesh_defl=0.4, sample_defl=0.3):
    """Returns (parts, bbox) - per-solid HLR + writes one tagged SVG."""
    print(f"\n=== {step_path.name} / {view_name}  [per-solid] ===")
    t0 = time.time()
    shape = cq.importers.importStep(str(step_path)).val().wrapped
    print(f"  load {time.time()-t0:.1f}s")

    parts = run_hlr_per_solid(shape, view_dir,
                              mesh_defl=mesh_defl, sample_defl=sample_defl)
    stem = step_path.stem
    svg_path = out_dir / f"{stem}__{view_name}__t5_parts.svg"
    bbox = write_svg_parts(parts, svg_path)
    print(f"  wrote {svg_path.name}  {svg_path.stat().st_size/1024:.0f}KB")
    return parts, bbox


def run(step_path: Path, out_dir: Path, view_name: str, view_dir,
        modes=("smart", "detailed", "hidden"),
        mesh_defl=0.4, sample_defl=0.3,
        png_width=3500):
    print(f"\n=== {step_path.name} / {view_name} ===")
    t0 = time.time()
    shape = cq.importers.importStep(str(step_path)).val().wrapped
    print(f"  load {time.time()-t0:.1f}s")
    t1 = time.time()
    cats = run_hlr(shape, view_dir,
                   mesh_defl=mesh_defl, sample_defl=sample_defl)
    elapsed = time.time() - t1
    counts = {k: len(v) for k, v in cats.items()}
    print(f"  hlr  {elapsed:.1f}s  {counts}")
    stem = step_path.stem
    for mode in modes:
        svg_path = out_dir / f"{stem}__{view_name}__t5_{mode}.svg"
        png_path = out_dir / f"{stem}__{view_name}__t5_{mode}.png"
        write_svg(cats, svg_path, mode=mode)
        write_png(cats, png_path, mode=mode, width=png_width)
        print(f"  [{mode}] svg {svg_path.stat().st_size/1024:.0f}KB  "
              f"png {png_path.stat().st_size/1024:.0f}KB")


# Standard IFU views.  Each is a camera-position direction relative to focal.
# Tuned so the part's long axis projects roughly horizontal.
STD_VIEWS = {
    "iso":   (-0.5, -1.0,  0.7),   # front-right-above 3/4 iso
    "front": ( 0.0, -1.0,  0.25),  # mostly front, slight tilt
    "side":  (-1.0,  0.0,  0.25),  # mostly side, slight tilt
    "top":   ( 0.0,  0.0,  1.0),   # plan view from above
    "iso_r": ( 0.5, -1.0,  0.7),   # mirror iso
}


if __name__ == "__main__":
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    targets = [
        Path(r"C:\Users\FredMarshAccora\Downloads\P194-03-00 Folding siderail ASSE.STEP"),
        Path(__file__).parent.parent / "step_lineart_test" / "presto_top_level.step",
    ]
    # All three line-art modes for the existing png comparison + per-solid
    # tagged SVG for the interactive viewer.
    for sp in targets:
        if not sp.exists():
            print("missing:", sp); continue
        for vn in ("iso",):
            run(sp, out_dir, vn, STD_VIEWS[vn])
