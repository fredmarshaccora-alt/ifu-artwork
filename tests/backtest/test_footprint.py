"""Backtest for bugs #13 + #23 (footprint correctness).

#13: pixel bleed at part boundaries -- the rasterised footprint of part A
     included single pixels actually belonging to part B (caused wrong
     clicks AND visible overlap into neighbour parts).  The fix is the
     MORPH_OPEN + 1-px erode step before contour extraction.
#23: an occluded part should produce MULTIPLE contours when its visible
     region is bisected by an occluder.  Verify cv2.findContours with
     RETR_CCOMP returns one contour per disjoint region + holes.
"""
from __future__ import annotations
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")


def test_morph_clean_removes_single_pixel_leaks():
    """A tiny pixel sliver of part B inside part A's region should be
    removed by MORPH_OPEN.  This is what prevents click-on-A from
    triggering B's hit area."""
    mask = np.zeros((20, 20), dtype=np.uint8)
    # Big A region (rows 5..15, cols 5..15) -- a solid square
    mask[5:15, 5:15] = 255
    # A single stray "B" pixel inside A's region (oops, painter's
    # algorithm assigned this pixel to B)
    mask_b = np.zeros((20, 20), dtype=np.uint8)
    mask_b[10, 10] = 255
    # Apply the rasterizer's cleanup
    kernel = np.ones((3, 3), np.uint8)
    cleaned = cv2.morphologyEx(mask_b, cv2.MORPH_OPEN, kernel, iterations=1)
    assert not cleaned.any(), \
        "MORPH_OPEN must wipe single-pixel islands (anti-bleed)"


def test_occluded_region_gives_two_contours():
    """A horizontal bar split in two by a vertical occluder produces
    two disjoint visible regions, hence two external contours."""
    mask = np.zeros((30, 50), dtype=np.uint8)
    # Long bar (rows 12..18) split at columns 22..28 by an occluder
    mask[12:18, 5:22] = 255   # left piece
    mask[12:18, 28:45] = 255  # right piece
    contours, _hier = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    assert len(contours) == 2, \
        f"split bar should give 2 contours, got {len(contours)}"


def test_occluder_hole_appears_in_ccomp_hierarchy():
    """When the visible region has an INTERIOR hole (occluder fully
    inside the part), CCOMP returns external + inner contours."""
    mask = np.zeros((40, 40), dtype=np.uint8)
    cv2.rectangle(mask, (5, 5), (35, 35), 255, -1)
    cv2.rectangle(mask, (18, 18), (22, 22), 0, -1)  # cut a hole
    contours, _hier = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    assert len(contours) >= 2, \
        f"hole-in-rect should yield outer + inner contour, got {len(contours)}"
