"""Regression: footprint coordinates must live in the SAME (u, v) frame
as run_hlr_per_solid's polylines, so the bold-outline overlay lines up
on top of the SVG edges instead of being translated by `focal · axis`.

This bug has reverted twice already.  The trap is subtle:

  - OCCT's HLRAlgo_Projector.Project(P) returns ROTATION-ONLY screen
    coords -- u = P · x_axis, v = P · y_axis.  It does NOT subtract
    the gp_Ax2 Location (which we set to `focal`).
  - The "obvious" manual projection -- (P - focal) · axis -- is wrong
    by `focal · axis`, a constant offset per (camera, focal) pair.

Whenever the focal is non-zero (every Onshape import, since chair-style
models tend to be centred on Y ~= 376), this constant offset puts the
bold-outline silhouette in a totally different region of the SVG from
the part it's supposed to be tracing.

The fix: every manual projection in the pipeline must use
``u = P · x_axis`` (no focal subtraction).  Functions touched:

  compute_visible_footprints  (rasterized footprints for the bold edge)
  _project_solid_bboxes       (exact-algo polyline tagging)

This test pins both contracts.  If a future refactor adds a
``(P - focal) · axis`` back in, this test fails immediately.
"""
from __future__ import annotations
import pytest

from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Ax2, gp_Dir, gp_Vec


def _hlr_project(P, view_dir, focal):
    """The ground-truth projection: what OCCT's HLRAlgo_Projector
    actually does to a point."""
    cam = gp_Vec(*view_dir); cam.Normalize()
    up = gp_Vec(0, 0, 1) if abs(cam.Z()) < 0.95 else gp_Vec(0, 1, 0)
    x_vec = up.Crossed(cam); x_vec.Normalize()
    ax = gp_Ax2(gp_Pnt(*focal),
                 gp_Dir(cam.X(), cam.Y(), cam.Z()),
                 gp_Dir(x_vec.X(), x_vec.Y(), x_vec.Z()))
    proj = HLRAlgo_Projector(ax)
    out = gp_Pnt2d()
    proj.Project(gp_Pnt(*P), out)
    return out.X(), out.Y()


def _manual_project(P, view_dir, focal):
    """The projection inside compute_visible_footprints (after the fix).
    Must match HLR exactly -- no focal subtraction."""
    from t5_hlr_vector import build_projector
    proj, x_axis, y_axis, focal_pt = build_projector(view_dir, focal)
    ax, ay, az = x_axis
    bx, by, bz = y_axis
    u = P[0] * ax + P[1] * ay + P[2] * az
    v = P[0] * bx + P[1] * by + P[2] * bz
    return u, v


# ---- The contract -----------------------------------------------------

@pytest.mark.parametrize("focal", [
    (0, 0, 0),
    (0, 376, 102),       # the actual Onshape-chair focal that revealed
                          # the bug last time
    (-200, 50, 800),
    (1000, 1000, 1000),
])
@pytest.mark.parametrize("view_dir", [
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.7299, 0.4555, 0.5095),   # the chair-screenshot camera
])
def test_footprint_projection_matches_hlr(view_dir, focal):
    """For any point and camera, the manual projection used by
    compute_visible_footprints must equal OCCT HLR's Project() output."""
    # A handful of probe points covering the model bbox
    points = [
        (0, 0, 0),
        (100, 200, 300),
        (-204, 106, 204),   # part 0 centre from the chair
        (500, -300, 50),
    ]
    for P in points:
        u_hlr, v_hlr = _hlr_project(P, view_dir, focal)
        u_man, v_man = _manual_project(P, view_dir, focal)
        assert abs(u_hlr - u_man) < 1e-3, (
            f"u offset {u_hlr - u_man:.3f} at P={P} vd={view_dir} "
            f"focal={focal} -- compute_visible_footprints projection is "
            f"out of sync with HLR")
        assert abs(v_hlr - v_man) < 1e-3, (
            f"v offset {v_hlr - v_man:.3f} at P={P} vd={view_dir} "
            f"focal={focal}")


def test_solid_bbox_projection_matches_hlr():
    """_project_solid_bboxes must produce (u, v) in HLR's frame, not
    in the (P - focal) frame that doesn't match the actual edge
    polylines."""
    from t5_hlr_vector import _project_solid_bboxes, build_projector
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    # A simple 10x20x30 box at world (50, 60, 70) -- non-trivial extents
    # and a translated origin so the bug would show up.
    box = BRepPrimAPI_MakeBox(gp_Pnt(50, 60, 70),
                                gp_Pnt(60, 80, 100)).Shape()
    solids = [(0, "test_box", box)]

    focal = (0, 376, 102)
    view_dir = (0.73, 0.456, 0.51)
    _proj, xax, yax, fp = build_projector(view_dir, focal)
    bboxes = _project_solid_bboxes(solids, xax, yax, fp)
    assert bboxes, "no bbox returned"
    _idx, _lbl, umin, vmin, umax, vmax = bboxes[0]

    # The bbox must contain HLR's projection of all 8 corners
    corners = [(x, y, z)
               for x in (50, 60) for y in (60, 80) for z in (70, 100)]
    for P in corners:
        u, v = _hlr_project(P, view_dir, focal)
        assert umin - 1e-3 <= u <= umax + 1e-3, \
            f"HLR-projected corner u={u} outside bbox [{umin},{umax}]"
        assert vmin - 1e-3 <= v <= vmax + 1e-3, \
            f"HLR-projected corner v={v} outside bbox [{vmin},{vmax}]"
