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

import threading

# OCCT meshes (BRepMesh_IncrementalMesh) live on the TopoDS shape and
# persist across calls.  Re-running the mesher with a coarser-or-equal
# deflection is a no-op semantically, but the call still walks every
# face checking what to update.  Track the finest deflection ever
# applied to each shape (by id()) and skip when the request is already
# satisfied.  Saves the dominant cost (~30%) of _extract_projected_triangles
# when /api/render has just run on this shape at a finer mesh_defl.
_MESH_DEFL_REGISTRY: dict[int, float] = {}
_MESH_DEFL_LOCK = threading.Lock()


def _ensure_meshed(shape, mesh_defl: float) -> None:
    """Call BRepMesh_IncrementalMesh on ``shape`` only if it hasn't
    already been meshed at a finer-or-equal deflection.  Thread-safe.

    NB: a shape's id() is stable for its lifetime in Python, so the
    registry survives across requests as long as the same TopoDS shape
    object is in _SHAPES.  Reconfigure rebuilds the shape -> the id
    changes -> the registry entry is naturally invalidated.  We don't
    actively GC stale entries -- they're tiny (one float per id) and
    Python eventually reuses ids of freed objects (we'd update the
    entry on the next call anyway).
    """
    sid = id(shape)
    with _MESH_DEFL_LOCK:
        recorded = _MESH_DEFL_REGISTRY.get(sid)
        if recorded is not None and recorded <= mesh_defl:
            # We've already meshed this shape at a finer (or equal)
            # deflection; OCCT would re-walk every face just to find
            # nothing to do.  Skip.
            return
    BRepMesh_IncrementalMesh(shape, mesh_defl, False, 0.5, True)
    with _MESH_DEFL_LOCK:
        prior = _MESH_DEFL_REGISTRY.get(sid)
        if prior is None or mesh_defl < prior:
            _MESH_DEFL_REGISTRY[sid] = mesh_defl


def _invalidate_mesh_cache(shape) -> None:
    """Forget what deflection ``shape`` has been meshed at.  Call this
    after any operation that mutates the TopoDS triangulation in place
    (rotate_shape returns a copy, so it doesn't need this -- but if a
    future code path edits a shape in place, route it here).
    """
    sid = id(shape)
    with _MESH_DEFL_LOCK:
        _MESH_DEFL_REGISTRY.pop(sid, None)


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
    """Build an HLR projector that matches three.js's camera convention.

    view_dir = (eye - focal) -- direction from the focal point to the camera.
    The camera is at +view_dir side of the scene.

    Ax2 setup:
      Z = +view_dir              (the "to-camera" direction; HLR's observer
                                  is at +Ax2_Z infinity, so this puts the
                                  observer on the same side three.js has it)
      X = up x view_dir          (camera-right, matching three.js's
                                  Matrix4.lookAt which does
                                  x_axis = up x z_axis, z_axis = view_dir)
      Y = Z x X  (computed by OCCT to keep the frame right-handed)

    Previously the code used Z = -view_dir which put the OCCT observer on
    the OPPOSITE side of the model from three.js, producing a back-of-the-
    bed view that the SVG-side X-mirror tried (and failed for non-axial
    view_dirs) to compensate for.

    Returns (projector, x_axis_tuple, y_axis_tuple, focal_tuple) so callers
    can project arbitrary 3D points to projection (u, v) coords for
    bbox-based source tagging.
    """
    cam = gp_Vec(*view_dir)
    cam.Normalize()
    z_dir = gp_Dir(cam.X(), cam.Y(), cam.Z())

    if abs(cam.Z()) < 0.95:
        up = gp_Vec(0, 0, 1)
    else:
        up = gp_Vec(0, 1, 0)
    # screen-right axis = up x view_dir, matches three.js
    x_vec = up.Crossed(cam)
    x_vec.Normalize()
    x_dir = gp_Dir(x_vec.X(), x_vec.Y(), x_vec.Z())
    ax = gp_Ax2(gp_Pnt(*focal), z_dir, x_dir)
    # screen-up axis = Z x X = view_dir x (up x view_dir) = up - vd*(vd.up)
    y_vec = cam.Crossed(x_vec)
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
    _ensure_meshed(shape, mesh_defl)
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


def _extract_projected_triangles(shape, view_dir, focal, mesh_defl):
    """OCCT-bound phase of compute_visible_footprints: mesh the shape,
    project every triangle to ``(u, v, depth)`` and return a flat list.

    Split out from compute_visible_footprints so callers that hold the
    OCCT lock can release it after this phase -- the subsequent
    rasterise + contour-trace is pure numpy/cv2 and runs concurrently
    with /api/render.

    Returns ``tri_data`` as a list of
    ``(idx, u1, v1, d1, u2, v2, d2, u3, v3, d3)`` tuples.
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    proj, x_axis, y_axis, focal_pt = build_projector(view_dir, focal)
    ax, ay, az = x_axis
    bx, by, bz = y_axis
    cx, cy, cz = view_dir

    _ensure_meshed(shape, mesh_defl)
    solids = split_solids(shape)

    tri_data = []
    for idx, _label, solid in solids:
        face_exp = TopExp_Explorer(solid, TopAbs_FACE)
        while face_exp.More():
            face = TopoDS.Face_s(face_exp.Current())
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            face_exp.Next()
            if tri is None:
                continue
            trsf = loc.Transformation()
            nodes_2d = [None] * (tri.NbNodes() + 1)   # 1-indexed
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                # NB: project P directly (no focal subtraction) so we
                # land in OCCT HLR's coordinate frame.  See comment at
                # top of compute_visible_footprints.
                px, py, pz = p.X(), p.Y(), p.Z()
                u = px * ax + py * ay + pz * az
                v = px * bx + py * by + pz * bz
                d = px * cx + py * cy + pz * cz
                nodes_2d[i] = (u, v, d)
            for i in range(1, tri.NbTriangles() + 1):
                t = tri.Triangle(i)
                a, b, c = t.Get()
                p1 = nodes_2d[a]; p2 = nodes_2d[b]; p3 = nodes_2d[c]
                tri_data.append((idx,
                                  p1[0], p1[1], p1[2],
                                  p2[0], p2[1], p2[2],
                                  p3[0], p3[1], p3[2]))
    return tri_data


def _rasterise_visible_footprints(tri_data, part_indices, resolution=3000):
    """Pure-numpy/cv2 phase of compute_visible_footprints: take the
    projected triangles, paint them back-to-front into a depth-tested
    ID buffer, and trace closed contours per requested part.

    Lock-free: the OCCT shape is no longer touched, so this can run
    concurrently with /api/render without holding _HLR_LOCK.
    """
    import numpy as np
    import cv2

    if not tri_data:
        return {i: [] for i in part_indices}

    # bbox from all triangle vertices
    all_u = np.array([t[1] for t in tri_data] +
                     [t[4] for t in tri_data] +
                     [t[7] for t in tri_data])
    all_v = np.array([t[2] for t in tri_data] +
                     [t[5] for t in tri_data] +
                     [t[8] for t in tri_data])
    u_min, u_max = float(all_u.min()), float(all_u.max())
    v_min, v_max = float(all_v.min()), float(all_v.max())
    span = max(u_max - u_min, v_max - v_min, 1.0)
    px_per_mm = (resolution - 2) / span
    w_px = int((u_max - u_min) * px_per_mm) + 2
    h_px = int((v_max - v_min) * px_per_mm) + 2

    id_buf = np.zeros((h_px, w_px), dtype=np.int32)
    # z_buf: depth of the currently-winning triangle per pixel.  Init
    # to -inf so the FIRST triangle paints unconditionally.  Higher
    # depth = closer to camera = wins.
    z_buf = np.full((h_px, w_px), -np.inf, dtype=np.float64)
    # Per-part painted-pixel bbox (in pixel space) so the contour pass
    # below only scans each part's own region instead of the whole frame
    # once per part.  {idx: [x_lo, y_lo, x_hi, y_hi]}.
    part_bbox: dict = {}

    # NB: I tried "vectorising" this loop by packing tri_data into a
    # single (N, 10) numpy array and indexing per-iteration.  Bench
    # showed a 19-32% REGRESSION across small/medium/large workloads:
    # the numpy-scalar dereferences and array-slice overhead dominated
    # the saved Python multiplications.  The natural tuple unpack
    # below is fastest at this scale because each iteration's inner
    # work (meshgrid + barycentric across the triangle bbox) is what
    # actually dominates -- the per-iteration setup is in the noise.
    for (idx, u1, v1, d1, u2, v2, d2, u3, v3, d3) in tri_data:
        # Pixel-space vertices
        x1 = (u1 - u_min) * px_per_mm
        y1 = (v1 - v_min) * px_per_mm
        x2 = (u2 - u_min) * px_per_mm
        y2 = (v2 - v_min) * px_per_mm
        x3 = (u3 - u_min) * px_per_mm
        y3 = (v3 - v_min) * px_per_mm

        # Bounding box (clipped)
        x_lo = max(int(np.floor(min(x1, x2, x3))), 0)
        x_hi = min(int(np.ceil(max(x1, x2, x3))) + 1, w_px)
        y_lo = max(int(np.floor(min(y1, y2, y3))), 0)
        y_hi = min(int(np.ceil(max(y1, y2, y3))) + 1, h_px)
        if x_hi <= x_lo or y_hi <= y_lo:
            continue

        # 2x signed area; degenerate triangle => skip
        area2 = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
        if area2 == 0:
            continue
        inv_area2 = 1.0 / area2

        # Pixel grid in the bbox (integer centres at +0.5 conventionally;
        # using pixel-corner is fine for this resolution).
        xs = np.arange(x_lo, x_hi, dtype=np.float64) + 0.5
        ys = np.arange(y_lo, y_hi, dtype=np.float64) + 0.5
        XX, YY = np.meshgrid(xs, ys)  # YY: rows, XX: cols

        # Barycentric weights via edge functions
        w1 = ((x2 - XX) * (y3 - YY) - (x3 - XX) * (y2 - YY)) * inv_area2
        w2 = ((x3 - XX) * (y1 - YY) - (x1 - XX) * (y3 - YY)) * inv_area2
        w3 = 1.0 - w1 - w2

        inside = (w1 >= 0) & (w2 >= 0) & (w3 >= 0)
        if not inside.any():
            continue

        # Interpolated depth at each pixel
        depths = w1 * d1 + w2 * d2 + w3 * d3
        # Z-test: paint where inside AND closer (higher depth) than buf
        sub_z = z_buf[y_lo:y_hi, x_lo:x_hi]
        win = inside & (depths > sub_z)
        if not win.any():
            continue
        sub_z[win] = depths[win]
        id_buf[y_lo:y_hi, x_lo:x_hi][win] = int(idx) + 1
        # Grow this part's painted-pixel bbox (winning region only).
        bb = part_bbox.get(int(idx))
        if bb is None:
            part_bbox[int(idx)] = [x_lo, y_lo, x_hi, y_hi]
        else:
            if x_lo < bb[0]: bb[0] = x_lo
            if y_lo < bb[1]: bb[1] = y_lo
            if x_hi > bb[2]: bb[2] = x_hi
            if y_hi > bb[3]: bb[3] = y_hi

    # DEBUG: save the ID buffer as a coloured PNG so we can eyeball
    # what the rasterizer is actually producing.  Each part gets a
    # distinct deterministic colour; empty space is white.
    import os
    if os.environ.get("FOOTPRINT_DEBUG") == "1":
        debug_path = Path(__file__).parent / "out" / "_footprint_debug.png"
        h, w = id_buf.shape
        rgb = np.full((h, w, 3), 255, dtype=np.uint8)
        # Hash idx -> distinct RGB.  +1 offset so 0 stays white.
        ids_present = np.unique(id_buf)
        for raw_id in ids_present:
            if raw_id == 0:
                continue
            # Simple deterministic hash for visual contrast
            r = (raw_id * 73) % 255
            g = (raw_id * 151) % 255
            b = (raw_id * 211) % 255
            mask = (id_buf == raw_id)
            rgb[mask] = (r, g, b)
        # OpenCV expects BGR
        cv2.imwrite(str(debug_path), rgb[:, :, ::-1])
        print(f"  [DEBUG] wrote {debug_path}  ({w}x{h}, "
              f"{len(ids_present)-1} parts rasterized)", flush=True)

    # ---- 3) extract closed contours per requested part ---------------
    # Per-part outlines: erode 1 px so adjacent parts' contours stay
    # clear of each other.
    out = {}
    for idx in part_indices:
        bb = part_bbox.get(int(idx))
        if bb is None:
            out[idx] = []
            continue
        # Only scan this part's own region (+2 px pad so the erosion /
        # contour at the edge isn't clipped).  Far cheaper than a
        # full-frame `id_buf == idx` for every one of N parts.
        x0 = max(bb[0] - 2, 0)
        y0 = max(bb[1] - 2, 0)
        x1 = min(bb[2] + 2, w_px)
        y1 = min(bb[3] + 2, h_px)
        sub = id_buf[y0:y1, x0:x1]
        mask = (sub == idx + 1).astype(np.uint8) * 255
        # Offset u_min/v_min by the sub-region origin so traced pixel
        # coords map back to absolute (u, v).
        # erode=False: the bold highlight perimeter must sit ON the part's
        # true edge (== the base HLR line).  The old 1-px erosion pulled it
        # inward so the base line peeked out alongside it.
        out[idx] = _trace_mask_to_polylines(
            mask, px_per_mm,
            u_min + x0 / px_per_mm,
            v_min + y0 / px_per_mm,
            erode=False)
    # Attach the id_buf to the returned dict via a private key so the
    # caller can compute additional union outlines without rebuilding
    # the raster.  The marker is a tuple to avoid collision with int keys.
    out[("__id_buf__",)] = (id_buf, px_per_mm, u_min, v_min)
    return out


def _trace_mask_to_polylines(mask, px_per_mm, u_min, v_min,
                              erode=True, smooth_close=False):
    """Convert a binary uint8 mask into a list of closed polylines in
    (u, v) space.  Single-source-of-truth for every contour-trace in
    the footprint pipeline (per-part, per-group union, assembly).

    erode: 1-px erosion to keep adjacent parts' contours from kissing.
           Disabled for unions/assemblies because the seam IS where we
           want the contour.
    smooth_close: MORPH_CLOSE to bridge 1-pixel gaps between rasterised
                   parts that should be touching.  Useful for unions to
                   fuse adjacent parts whose pixel boundaries fall on
                   slightly-different grid lines.
    """
    import numpy as np
    import cv2

    if not mask.any():
        return []
    kernel = np.ones((3, 3), np.uint8)
    if smooth_close:
        # Close 1-pixel cracks BEFORE the morph-open / erode.  Adjacent
        # rasterised parts often leave a single-pixel diagonal gap from
        # anti-aliasing; the union mask would expose it as a notch in
        # the merged silhouette unless we bridge it.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    # MORPH_OPEN drops stray pixel islands.
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    if erode:
        mask = cv2.erode(mask, kernel, iterations=1)
    if not mask.any():
        return []
    # CCOMP gives external boundaries + 1 level of internal holes.
    contours, _hier = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    MIN_AREA_PX = 9
    polys = []
    for cnt in contours:
        if len(cnt) < 3:
            continue
        if cv2.contourArea(cnt) < MIN_AREA_PX:
            continue
        # +0.5: the rasteriser samples each pixel at its CENTRE
        # (meshgrid `arange + 0.5`), so a contour vertex at pixel index
        # `px` corresponds to true position (px + 0.5) / px_per_mm.
        # Without this the whole footprint is shifted half a pixel toward
        # the (u_min, v_min) corner -- visible as the bold highlight
        # perimeter sitting just off the base line.
        pl = [((float(px) + 0.5) / px_per_mm + u_min,
               (float(py) + 0.5) / px_per_mm + v_min)
              for [[px, py]] in cnt]
        pl.append(pl[0])
        polys.append(pl)
    # Keep most of the traced contour points so small circular features
    # (bolt holes etc.) stay round.  The old max(0.5, 1.0/px_per_mm) tol
    # collapsed a ~30-point hole contour down to a 6-8 sided polygon --
    # the "spiky circles".  ~0.35 px tol preserves the curve while still
    # dropping collinear points along straight edges.
    dp_tol = max(0.15, 0.35 / px_per_mm)
    polys = [_dp_simplify(pl, dp_tol) for pl in polys]
    polys = [pl for pl in polys if len(pl) >= 3]
    return polys


def compute_assembly_silhouette_from_raster(raster_handle):
    """Given a raster handle (the id_buf tuple injected into
    _rasterise_visible_footprints' return value), trace the union of
    every non-empty pixel as the COMBINED assembly silhouette.

    Use case: replace the per-part outline_v layer with the true outer
    silhouette of the whole assembly, so adjacent parts no longer show
    their shared edge as a heavy line.
    """
    import numpy as np
    id_buf, px_per_mm, u_min, v_min = raster_handle
    mask = (id_buf != 0).astype(np.uint8) * 255
    return _trace_mask_to_polylines(
        mask, px_per_mm, u_min, v_min,
        erode=False, smooth_close=True)


def compute_group_silhouettes_from_raster(raster_handle, groups):
    """Given a raster handle and a dict of {group_key: [part_idx, ...]},
    return {group_key: [polylines]} for each group's union silhouette.

    Used when N adjacent parts share the same highlight style -- we
    want one merged closed loop, not N per-part loops with seams.
    """
    import numpy as np
    id_buf, px_per_mm, u_min, v_min = raster_handle
    out = {}
    for key, idxs in groups.items():
        if not idxs:
            out[key] = []
            continue
        # Build the union mask
        mask = np.zeros_like(id_buf, dtype=np.uint8)
        for idx in idxs:
            mask |= (id_buf == idx + 1).astype(np.uint8)
        mask *= 255
        out[key] = _trace_mask_to_polylines(
            mask, px_per_mm, u_min, v_min,
            erode=False, smooth_close=True)
    return out


def compute_visible_footprints(shape, part_indices, view_dir, focal=(0, 0, 0),
                                mesh_defl=0.4, resolution=3000):
    """For each selected part, return the boundary polyline(s) of its
    VISIBLE 2D footprint -- i.e. the closed polygon outlining what the
    user actually sees of that part on screen, with occluder cuts
    correctly drawn along the occluder's boundary.

    Method:
      1. Mesh the assembly via BRepMesh_IncrementalMesh.
      2. Project every triangle to (u, v) and compute its mean depth.
      3. Sort triangles back-to-front (painter's algorithm).
      4. Rasterize each triangle into an int32 ID buffer using cv2.fillPoly
         -- pixel value = part_idx of the front-most triangle covering it.
      5. For each requested part_idx, threshold the buffer to a binary
         mask and trace closed contours via cv2.findContours.
      6. Convert pixel coords back to (u, v) and DP-simplify the result.

    Returns: dict {idx: [polyline, ...]} where each polyline is a closed
    list of (u, v) tuples in the same projection space as the baked SVG.

    Now a thin wrapper over ``_extract_projected_triangles`` (OCCT-bound,
    needs the HLR lock) + ``_rasterise_visible_footprints`` (pure numpy/
    cv2, lock-free).  Callers that want to release the OCCT lock between
    phases can call the two halves directly -- see ``serve._kick_footprint_raster``.
    """
    if not part_indices:
        return {}
    tri_data = _extract_projected_triangles(shape, view_dir, focal, mesh_defl)
    out = _rasterise_visible_footprints(tri_data, part_indices, resolution)
    # Strip the private raster handle so legacy callers see only the
    # {idx: polylines} shape they expect.
    out.pop(("__id_buf__",), None)
    return out


def run_hlr_in_region(shape, view_dir, focal=(0, 0, 0),
                       bbox_uv=None,
                       mesh_defl=0.4, sample_defl=0.4,
                       padding_mm=10.0):
    """HLR on JUST the solids whose projected bbox overlaps ``bbox_uv``.

    Used by the "render this zoom window at higher detail" workflow.
    A few thousand parts get cut down to (typically) tens, and we can
    run at fine mesh/sample without paying the full-assembly cost.

    Args:
      bbox_uv    : (u_min, v_min, u_max, v_max) in projector space, or
                   None to keep every solid (= same as run_hlr_per_solid
                   but with the finer detail defaults).
      padding_mm : Extend bbox_uv outward by this much when filtering, so
                   occluders just outside the visible window still cull
                   correctly.  10mm is enough for a thin bracket lip.

    Trade-off: occluders OUTSIDE bbox_uv + padding are ignored, so a
    big plate fully outside the window won't hide a tube inside it.
    For typical "zoom in to inspect a feature" use, the padding handles
    it.  Document if you hit a case where it matters.
    """
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder

    proj, x_axis, y_axis, focal_pt = build_projector(view_dir, focal)
    all_solids = split_solids(shape)

    # Project each solid's 3D bbox to (u,v) and filter
    if bbox_uv is not None:
        u_min, v_min, u_max, v_max = bbox_uv
        u_min -= padding_mm; v_min -= padding_mm
        u_max += padding_mm; v_max += padding_mm
        solid_bboxes = _project_solid_bboxes(all_solids, x_axis, y_axis, focal_pt)
        kept_idxs = set()
        for idx, _label, su0, sv0, su1, sv1 in solid_bboxes:
            # Bbox intersection test
            if su1 < u_min or su0 > u_max or sv1 < v_min or sv0 > v_max:
                continue
            kept_idxs.add(idx)
        solids = [s for s in all_solids if s[0] in kept_idxs]
    else:
        solids = all_solids

    if not solids:
        return []

    # Build a compound of just the kept solids -- this is what HLR runs on
    if len(solids) == 1:
        compound = solids[0][2]
    else:
        compound = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(compound)
        for _idx, _label, solid in solids:
            builder.Add(compound, solid)

    # Run the full per-solid HLR pipeline on the filtered compound at
    # the requested fine detail.  Reuses existing dedup + DP simplify.
    return run_hlr_per_solid(compound, view_dir, focal=focal,
                              mesh_defl=mesh_defl, sample_defl=sample_defl,
                              progress=False)


def run_group_silhouette(shape, part_indices, view_dir, focal=(0, 0, 0),
                          mesh_defl=0.4, sample_defl=0.3):
    """Single TRUE silhouette of the COMPOUND of the selected solids.

    Builds a compound of the named solids, runs PolyAlgo on it in
    isolation (no other parts), and returns the visible silhouette +
    sharp edges that form the outer profile of the GROUP -- not the
    individual parts.  Used for "outline as group" mode in the viewer.

    Returns: list of polylines (each a list of (u,v) tuples).
    """
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder

    proj, _x, _y, _f = build_projector(view_dir, focal)
    all_solids = split_solids(shape)
    by_idx = {idx: solid for idx, _label, solid in all_solids}
    chosen = [by_idx[i] for i in part_indices if i in by_idx]
    if not chosen:
        return []
    if len(chosen) == 1:
        # Single-solid compound is just the solid -- reuse the per-part path
        compound = chosen[0]
    else:
        compound = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(compound)
        for s in chosen:
            builder.Add(compound, s)
    try:
        _ensure_meshed(compound, mesh_defl)
        algo = HLRBRep_PolyAlgo()
        algo.Load(compound)
        algo.Projector(proj)
        algo.Update()
        extr = HLRBRep_PolyHLRToShape()
        extr.Update(algo)
        v_outline = filter_outliers(
            sample_edges(extr.OutLineVCompound(), sample_defl))
        v_sharp = sample_edges(extr.VCompound(), sample_defl)
        dp_tol = sample_defl * 0.5
        polys = [_dp_simplify(pl, dp_tol) for pl in (v_outline + v_sharp)]
        return [pl for pl in polys if len(pl) >= 2]
    except Exception as exc:
        print(f"  group silhouette failed: {type(exc).__name__}: {exc}")
        return []


def run_part_silhouettes(shape, part_indices, view_dir, focal=(0, 0, 0),
                         mesh_defl=0.4, sample_defl=0.3):
    """Per-part TRUE silhouettes, ignoring occlusion from other parts.

    Runs PolyAlgo on each requested solid IN ISOLATION, so the returned
    outline polylines form the full closed profile the part would have
    if no neighbouring parts existed.  This is what we need for a clean
    bold edge + closed fill in the viewer.

    Returns: dict {idx: [polyline, ...]} where each polyline is a list
    of (u, v) tuples in the same projection space as run_hlr_per_solid,
    so the SVG (u,v) coordinates line up exactly.
    """
    proj, _x, _y, _f = build_projector(view_dir, focal)
    all_solids = split_solids(shape)
    by_idx = {idx: solid for idx, _label, solid in all_solids}
    out = {}
    for idx in part_indices:
        solid = by_idx.get(idx)
        if solid is None:
            out[idx] = []
            continue
        try:
            _ensure_meshed(solid, mesh_defl)
            algo = HLRBRep_PolyAlgo()
            algo.Load(solid)
            algo.Projector(proj)
            algo.Update()
            extr = HLRBRep_PolyHLRToShape()
            extr.Update(algo)
            # Outline-V alone misses parts viewed end-on (cylinders' long
            # edges are sharp_v, not silhouette).  Combine BOTH visible
            # categories so the bold profile covers tubes, plates, every
            # thin geometry.  We filter outliers on outline only (sharp
            # edges are real model edges and shouldn't be culled).
            v_outline = filter_outliers(
                sample_edges(extr.OutLineVCompound(), sample_defl))
            v_sharp = sample_edges(extr.VCompound(), sample_defl)
            # DP-simplify so single-part silhouettes are as light as the
            # baked SVG polylines.
            dp_tol = sample_defl * 0.5
            polys = [_dp_simplify(pl, dp_tol) for pl in (v_outline + v_sharp)]
            polys = [pl for pl in polys if len(pl) >= 2]
        except Exception as exc:
            print(f"  silhouette part {idx} failed: "
                  f"{type(exc).__name__}: {exc}")
            polys = []
        out[idx] = polys
    return out


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
        _ensure_meshed(shape, mesh_defl)
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
        _ensure_meshed(shape, mesh_defl)
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

    # 3) Exact path - compound add.  Per-solid Add was tried (so VCompound
    #    selectors would work) but OCCT's Hide goes O(N^2) on 700+ solids
    #    and the render stalls.  We stick with compound Add and lean on
    #    improved bbox-tagging (full-polyline-bbox containment) to avoid
    #    the "scattered fragments" misassignment problem.
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
        # Douglas-Peucker simplification at half the sample tolerance.
        # Drops collinear runs from straight tubes / plates (most of an
        # assembly's geometry) without visible change at typical zoom.
        dp_tol = sample_defl * 0.5
        pb, pa = _simplify_polylines_in_place(parts, dp_tol)
        if progress:
            print(f"  per-solid extract done {time.time()-t1:.1f}s  "
                  f"({len(parts)} parts with edges; "
                  f"deduped {n_total}->{n_kept}, dropped {n_degen} degenerate; "
                  f"DP@{dp_tol:.2f}mm pts {pb}->{pa} "
                  f"({100*(pb-pa)//max(pb,1)}%))",
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
    dp_tol = sample_defl * 0.5
    pb, pa = _simplify_polylines_in_place(parts, dp_tol)
    if progress:
        print(f"  bbox-tagged {n_total} polylines into {len(parts)} parts "
              f"in {time.time()-t2:.1f}s (deduped {nT}->{nK}, "
              f"dropped {nD} degenerate; DP@{dp_tol:.2f}mm pts "
              f"{pb}->{pa} ({100*(pb-pa)//max(pb,1)}%))", flush=True)
    return parts


def _dedup_polylines_in_place(parts, precision=1):
    """Remove duplicate polylines (across all parts) and drop degenerate ones.

    HLR on a multi-solid compound returns shared edges once per neighbouring
    solid -- so the same outline edge between two adjacent panels appears in
    both solids' extracted polyline lists.  Without dedup the SVG draws each
    shared edge 2x, 6x, even 10x on top of itself.

    Dedup key is the rounded coordinate sequence so floating-point jitter
    between extractions doesn't defeat the comparison.  precision=1 (0.1 mm)
    catches OCCT's natural float jitter between extractions on neighbouring
    solids.  precision=2 leaves real duplicates because float noise exceeds
    0.01 mm, blowing SVG size up by 2x for ~3% of duplicates caught.
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


def _dp_simplify(pl, tol):
    """Douglas-Peucker polyline simplification (iterative, stack-based).

    Drops collinear runs within `tol` of the chord.  Preserves endpoints,
    keeps the polyline's shape at the chosen viewing scale.  Iterative
    to avoid Python's 1000-deep recursion limit on long polylines.
    """
    n = len(pl)
    if n < 3:
        return list(pl)
    keep = [False] * n
    keep[0] = True
    keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol * tol
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 < 2:
            continue
        ax, ay = pl[i0]
        bx, by = pl[i1]
        dx = bx - ax
        dy = by - ay
        seg_len2 = dx * dx + dy * dy
        max_d2 = -1.0
        max_i = -1
        if seg_len2 <= 1e-20:
            # zero-length chord: distance == sqrt((x-ax)^2+(y-ay)^2)
            for i in range(i0 + 1, i1):
                px, py = pl[i]
                d2 = (px - ax) ** 2 + (py - ay) ** 2
                if d2 > max_d2:
                    max_d2 = d2
                    max_i = i
        else:
            for i in range(i0 + 1, i1):
                px, py = pl[i]
                # perpendicular distance squared from point to chord
                cross = (px - ax) * dy - (py - ay) * dx
                d2 = (cross * cross) / seg_len2
                if d2 > max_d2:
                    max_d2 = d2
                    max_i = i
        if max_d2 > tol2:
            keep[max_i] = True
            stack.append((i0, max_i))
            stack.append((max_i, i1))
    return [pl[i] for i, k in enumerate(keep) if k]


def _simplify_polylines_in_place(parts, tol):
    """Run DP simplification on every polyline of every part.

    Returns (n_total_pts_before, n_total_pts_after).
    """
    n_before = 0
    n_after = 0
    for part in parts:
        for cat, pls in part.get("polys", {}).items():
            kept = []
            for pl in pls:
                n_before += len(pl)
                simp = _dp_simplify(pl, tol)
                if len(simp) >= 2:
                    n_after += len(simp)
                    kept.append(simp)
            part["polys"][cat] = kept
    return n_before, n_after


def _project_solid_bboxes(solids, x_axis, y_axis, focal_pt):
    """Return list of (idx, label, u_min, v_min, u_max, v_max) per solid.

    NB: OCCT's HLR projector is rotation-only (u = P · axis, no focal
    subtraction).  We project bbox corners the same way so the (u, v)
    bbox is in the SAME frame as HLR's polylines and the
    point-in-rectangle test in _tag_by_bbox works.  ``focal_pt`` is
    kept in the signature for API stability but no longer used.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    out = []
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
                    us.append(x * ax_x + y * ay_x + z * az_x)
                    vs.append(x * ax_y + y * ay_y + z * az_y)
        out.append((idx, label, min(us), min(vs), max(us), max(vs)))
    return out


def _tag_by_bbox(polys_by_cat, solid_bboxes_2d):
    """Tag each polyline by the solid whose projected bbox contains the
    polyline's FULL extent (not just its centroid).

    Centroid-only matching gave scattered, incoherent highlighting on
    densely-packed assemblies (Presto): the centroid of a polyline
    belonging to part A would happen to fall inside the bbox of an
    unrelated nearby part B with a smaller bbox, so part B "stole" the
    edge.  Full-bbox containment means a polyline only matches a solid
    whose 2D bbox FULLY ENCLOSES the polyline's 2D bbox -- much more
    accurate.  We still pick the smallest-area enclosing bbox so a leaf
    body inside an assembly beats the assembly's outer bbox.

    Fallback chain: full containment -> centroid containment (smaller
    bbox wins) -> nearest center, but only within 5% of envelope
    diagonal.  Outside that, the polyline is dropped as a spurious HLR
    artifact.
    """
    parts_map = {}
    bb_arr = [(idx, label, u0, v0, u1, v1, (u1 - u0) * (v1 - v0))
              for idx, label, u0, v0, u1, v1 in solid_bboxes_2d]
    if bb_arr:
        env_u0 = min(b[2] for b in bb_arr)
        env_v0 = min(b[3] for b in bb_arr)
        env_u1 = max(b[4] for b in bb_arr)
        env_v1 = max(b[5] for b in bb_arr)
        env_diag = ((env_u1 - env_u0) ** 2 + (env_v1 - env_v0) ** 2) ** 0.5
        max_fallback_dist = env_diag * 0.05
    else:
        max_fallback_dist = 0.0

    n_dropped = 0
    n_full = 0; n_centroid = 0; n_nearest = 0
    for cat, polylines in polys_by_cat.items():
        for pl in polylines:
            if not pl:
                continue
            xs = [p[0] for p in pl]
            ys = [p[1] for p in pl]
            pu0, pu1 = min(xs), max(xs)
            pv0, pv1 = min(ys), max(ys)
            cx = (pu0 + pu1) / 2.0
            cy = (pv0 + pv1) / 2.0

            # Tier 1: smallest-area solid bbox that FULLY ENCLOSES the
            # polyline bbox.  This is the authoritative test.
            best_idx = None
            best_label = "unknown"
            best_area = float("inf")
            for idx, label, u0, v0, u1, v1, area in bb_arr:
                if u0 <= pu0 and pu1 <= u1 and v0 <= pv0 and pv1 <= v1 \
                        and area < best_area:
                    best_idx = idx
                    best_label = label
                    best_area = area
            if best_idx is not None:
                n_full += 1
            else:
                # Tier 2: centroid containment (legacy heuristic).  Allows
                # polylines that slightly cross a bbox boundary -- common
                # near edge tangents and shared-edge HLR artefacts.
                for idx, label, u0, v0, u1, v1, area in bb_arr:
                    if u0 <= cx <= u1 and v0 <= cy <= v1 and area < best_area:
                        best_idx = idx
                        best_label = label
                        best_area = area
                if best_idx is not None:
                    n_centroid += 1

            if best_idx is None:
                # Tier 3: nearest-center fallback, capped at 5% of envelope.
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
                    n_nearest += 1
                else:
                    n_dropped += 1
                    continue

            p = parts_map.setdefault(best_idx, {
                "idx": best_idx, "label": best_label, "polys": {}})
            p["polys"].setdefault(cat, []).append(pl)
    print(f"  bbox-tag: full={n_full} centroid={n_centroid} nearest={n_nearest} "
          f"dropped={n_dropped}")
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

    # Merge every polyline of a (part, layer) into ONE <path> via repeated
    # M ... L ... subpaths.  Visually identical (each "M" lifts the pen)
    # but ~200x fewer DOM nodes, which is the real bottleneck for the
    # 80MB+ Presto/Contesa bundle's pan/zoom and selection performance.
    def _merge_d(polylines):
        return " ".join(
            "M " + " L ".join(f"{fmt % x} {fmt % y}" for x, y in pl)
            for pl in polylines if len(pl) >= 2
        )

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
            d = _merge_d(pls)
            if not d:
                continue
            lines.append(
                f'<g class="part part-{p["idx"]:03d}" '
                f'data-part="{p["idx"]}" data-label="{p["label"]}">'
                f'<path d="{d}"/></g>'
            )
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
        d = _merge_d(all_polys)
        if not d:
            continue
        lines.append(
            f'<g class="part part-{p["idx"]:03d}" '
            f'data-part="{p["idx"]}" data-label="{p["label"]}">'
            f'<path d="{d}"/></g>'
        )
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
