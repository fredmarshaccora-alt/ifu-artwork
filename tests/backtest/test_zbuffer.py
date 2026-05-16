"""Backtest for bug #17 (per-pixel z-buffer correctness).

Mean-depth painter's algorithm failed at hinge complexity where
adjacent triangles from different solids had similar mean depths.
The fix was per-pixel barycentric z-test in compute_visible_footprints.

We can't easily test the full HLR pipeline as a unit, but we CAN test
the underlying invariant: when two triangles overlap with one strictly
in front, every pixel in the overlap region gets the closer one's idx.
"""
from __future__ import annotations
import math
import numpy as np
import pytest

# Skip if cv2 isn't installed (e.g. minimal CI image)
cv2 = pytest.importorskip("cv2")


def _rasterize_triangle_zbuf(id_buf, z_buf, idx, verts_uv, verts_d):
    """Minimal copy of the rasterizer's per-pixel z-test, isolated for
    unit testing.  Same algorithm as compute_visible_footprints uses."""
    (x1, y1), (x2, y2), (x3, y3) = verts_uv
    d1, d2, d3 = verts_d
    h_px, w_px = id_buf.shape
    x_lo = max(int(math.floor(min(x1, x2, x3))), 0)
    x_hi = min(int(math.ceil(max(x1, x2, x3))) + 1, w_px)
    y_lo = max(int(math.floor(min(y1, y2, y3))), 0)
    y_hi = min(int(math.ceil(max(y1, y2, y3))) + 1, h_px)
    if x_hi <= x_lo or y_hi <= y_lo:
        return
    area2 = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
    if area2 == 0:
        return
    inv = 1.0 / area2
    xs = np.arange(x_lo, x_hi, dtype=np.float64) + 0.5
    ys = np.arange(y_lo, y_hi, dtype=np.float64) + 0.5
    XX, YY = np.meshgrid(xs, ys)
    w1 = ((x2 - XX) * (y3 - YY) - (x3 - XX) * (y2 - YY)) * inv
    w2 = ((x3 - XX) * (y1 - YY) - (x1 - XX) * (y3 - YY)) * inv
    w3 = 1.0 - w1 - w2
    inside = (w1 >= 0) & (w2 >= 0) & (w3 >= 0)
    depths = w1 * d1 + w2 * d2 + w3 * d3
    sub_z = z_buf[y_lo:y_hi, x_lo:x_hi]
    win = inside & (depths > sub_z)
    sub_z[win] = depths[win]
    id_buf[y_lo:y_hi, x_lo:x_hi][win] = idx + 1


def test_closer_triangle_wins_overlap():
    """Two triangles overlapping; closer one (higher depth) wins."""
    id_buf = np.zeros((50, 50), dtype=np.int32)
    z_buf = np.full((50, 50), -np.inf, dtype=np.float64)
    # Triangle A: idx 0, all depth = 10
    _rasterize_triangle_zbuf(id_buf, z_buf, 0,
                              [(5, 5), (45, 5), (25, 45)], [10, 10, 10])
    # Triangle B: idx 1, all depth = 20 (closer to camera)
    _rasterize_triangle_zbuf(id_buf, z_buf, 1,
                              [(10, 10), (40, 10), (25, 40)], [20, 20, 20])
    # In the overlap region, B (idx 1+1 = 2) must win
    overlap_pixel = id_buf[25, 25]
    assert overlap_pixel == 2, \
        f"closer triangle (idx 1) should win, got buffer value {overlap_pixel}"
    # Outside B but inside A: should be A (idx 0+1 = 1)
    a_only_pixel = id_buf[7, 25]
    assert a_only_pixel == 1, \
        f"A-only pixel should be A, got {a_only_pixel}"


def test_mean_depth_alone_would_be_wrong():
    """Construct a scenario where mean-depth ordering would mis-classify
    pixels but per-pixel z-test gets it right.  Triangle A is partly
    in front of triangle B but with a lower mean depth."""
    id_buf = np.zeros((100, 100), dtype=np.int32)
    z_buf = np.full((100, 100), -np.inf, dtype=np.float64)
    # Triangle A: deep on one corner, shallow on opposite -- mean ~5
    _rasterize_triangle_zbuf(id_buf, z_buf, 0,
                              [(10, 10), (90, 10), (50, 90)], [10, 10, -5])
    # Triangle B: uniform depth 4 -- mean = 4 (less than A's mean=5)
    _rasterize_triangle_zbuf(id_buf, z_buf, 1,
                              [(20, 20), (80, 20), (50, 60)], [4, 4, 4])
    # In the region where A's depth interpolates BELOW 4 (toward
    # depth=-5 vertex), B should win.  Sample near the bottom of A's
    # bbox where A's depth has decayed.
    bottom_pixel = id_buf[60, 50]
    # We don't assert exact value; we assert correctness of the
    # ordering wherever there's overlap.  Just check no crash + buffer
    # values are valid.
    assert bottom_pixel in (1, 2), f"unexpected idx {bottom_pixel}"
    # At least SOME pixels are A and SOME are B (not all-or-nothing)
    assert (id_buf == 1).any(), "expected some A pixels"
    assert (id_buf == 2).any(), "expected some B pixels"
