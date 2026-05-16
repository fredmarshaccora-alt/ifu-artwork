"""Backtest for bug #20 (convex hull oversized for concave parts).

This is a KNOWN limitation rather than a bug -- we document it via an
xfail test so the maintainer is aware:  L-bracket / C-channel parts
have a click-area larger than their visible region.

If we ever switch to an alpha-shape implementation, this xfail flips
to xpass and prompts re-evaluation.
"""
from __future__ import annotations
import math
import pytest


def _convex_hull(points):
    """Andrew's monotone chain (CCW)."""
    pts = sorted(points)
    if len(pts) < 3:
        return list(pts)
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _polygon_area(pts):
    n = len(pts)
    s = 0.0
    for i in range(n):
        j = (i+1) % n
        s += pts[i][0]*pts[j][1] - pts[j][0]*pts[i][1]
    return abs(s) / 2


def test_convex_hull_basic_correctness():
    """Sanity: hull of a triangle is the triangle."""
    pts = [(0, 0), (10, 0), (5, 8)]
    hull = _convex_hull(pts)
    assert len(hull) == 3
    assert math.isclose(_polygon_area(hull), 40, abs_tol=0.1)


@pytest.mark.xfail(reason="known limitation: hull oversized for L-shaped parts",
                   strict=False)
def test_l_bracket_hull_matches_true_area():
    """An L-shaped polyline has a true area smaller than its convex hull.
    Today this fails; xfail until we switch to alpha-shape.
    L bracket: 10x10 with a 6x6 notch in the upper-right."""
    pts = [(0,0),(10,0),(10,4),(4,4),(4,10),(0,10),(0,0)]
    true_area = 10*4 + 4*6  # 64
    hull = _convex_hull(pts)
    hull_area = _polygon_area(hull)
    # Hull is the full 10x10 square = 100.  We "expect" it to match
    # true_area within 5% -- it won't (hull is 36 mm² too big).
    assert hull_area <= true_area * 1.05, \
        f"hull area {hull_area} too generous vs true {true_area}"
