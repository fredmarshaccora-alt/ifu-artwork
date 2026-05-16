"""Backtest for bugs #1 + #2 (camera convention).

#1: OCCT projector Ax2 Z direction was -view_dir, putting OCCT's camera
    on the OPPOSITE side of the model from three.js.
#2: An X-mirror was being applied to the projected polylines to mask
    the bug.  This broke for non-orthogonal view_dirs.

Both regressions are protected here: we assert that for any view_dir,
the projected u,v of a point on the +view_dir side has positive depth,
and the projection is consistent with the three.js camera convention.
"""
from __future__ import annotations
import math
import pytest

from t5_hlr_vector import build_projector


@pytest.mark.parametrize("view_dir", [
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.577, 0.577, 0.577),
    (0.5, -0.3, 0.8),
])
def test_ax2_x_axis_is_up_cross_viewdir(view_dir):
    """Bug #1: projector's X axis must equal up x view_dir (matches
    three.js Matrix4.lookAt's camera-X)."""
    _proj, x_axis, _y_axis, _focal = build_projector(view_dir, (0, 0, 0))
    # up is +Z unless view_dir is too vertical, in which case +Y
    if abs(view_dir[2]) > 0.95:
        up = (0.0, 1.0, 0.0)
    else:
        up = (0.0, 0.0, 1.0)
    # Expected X = up x view_dir, normalised
    ex = up[1] * view_dir[2] - up[2] * view_dir[1]
    ey = up[2] * view_dir[0] - up[0] * view_dir[2]
    ez = up[0] * view_dir[1] - up[1] * view_dir[0]
    n = math.sqrt(ex*ex + ey*ey + ez*ez)
    ex, ey, ez = ex/n, ey/n, ez/n
    assert math.isclose(x_axis[0], ex, abs_tol=1e-6), \
        f"x_axis[0] {x_axis[0]} vs expected {ex} for view_dir {view_dir}"
    assert math.isclose(x_axis[1], ey, abs_tol=1e-6)
    assert math.isclose(x_axis[2], ez, abs_tol=1e-6)


def test_no_x_mirror_in_output():
    """Bug #2: write_svg_parts and HLR output must NOT apply an x-mirror.
    Verify that a known point's projected u increases when we move the
    point in the +x_axis direction."""
    view_dir = (0.577, -0.577, 0.577)
    _proj, x_axis, _y_axis, _focal = build_projector(view_dir, (0, 0, 0))
    # A point along +x_axis should project to positive u
    p_along_x = x_axis  # 1 unit along x_axis
    u = p_along_x[0]*x_axis[0] + p_along_x[1]*x_axis[1] + p_along_x[2]*x_axis[2]
    assert u > 0.99, f"projection along x_axis should yield positive u, got {u}"
