"""Regression: combined silhouette of N adjacent parts is one closed
loop, not N per-part loops with seams between them.

Pins the union-of-masks primitive used by both:
  - The baseline assembly silhouette (outline of every visible part)
  - The per-style highlight group outline (adjacent same-styled parts
    merging into one combined loop)
"""
from __future__ import annotations
import numpy as np


def _two_touching_squares_id_buf():
    """Build a synthetic id_buf where part 0 = a 60x60 square at the
    left and part 1 = a 60x60 square abutting it on the right.  Total
    visible footprint is a 120x60 rectangle = one closed loop after
    union; per-part it's two squares with a shared seam at x=60."""
    buf = np.zeros((80, 140), dtype=np.int32)
    buf[10:70, 10:70] = 1   # part 0 -> id 1
    buf[10:70, 70:130] = 2  # part 1 -> id 2
    return (buf, 1.0, 0.0, 0.0)   # px_per_mm = 1, u_min = v_min = 0


def test_assembly_silhouette_traces_union():
    from t5_hlr_vector import compute_assembly_silhouette_from_raster
    handle = _two_touching_squares_id_buf()
    polys = compute_assembly_silhouette_from_raster(handle)
    # Two abutting squares -> one combined contour (not two)
    assert len(polys) == 1, (
        f"expected 1 union contour, got {len(polys)}: "
        f"sizes={[len(p) for p in polys]}"
    )
    # Bounding box should span the full 120x60 rectangle (give or take
    # the morph close/erode tolerance)
    xs = [p[0] for p in polys[0]]
    ys = [p[1] for p in polys[0]]
    assert max(xs) - min(xs) > 100, "union should span both squares"
    assert max(ys) - min(ys) > 50


def test_group_silhouettes_merge_adjacent():
    """Two parts in the same group => one merged outline; two parts in
    separate groups => two outlines."""
    from t5_hlr_vector import compute_group_silhouettes_from_raster

    handle = _two_touching_squares_id_buf()
    # Group both parts as one
    merged = compute_group_silhouettes_from_raster(handle,
                                                    {"hi": [0, 1]})
    assert "hi" in merged
    assert len(merged["hi"]) == 1, (
        f"adjacent parts grouped together must produce one loop, "
        f"got {len(merged['hi'])}"
    )

    # Each part in its own group -> two outlines (one per group)
    split = compute_group_silhouettes_from_raster(handle,
                                                   {"a": [0], "b": [1]})
    assert len(split["a"]) == 1 and len(split["b"]) == 1


def test_group_silhouettes_disjoint_parts():
    """A group of two non-adjacent parts should produce two
    disconnected polylines under the same group key."""
    buf = np.zeros((80, 200), dtype=np.int32)
    buf[10:70, 10:70] = 1
    buf[10:70, 130:190] = 2   # 60 px gap between part 0 and part 1
    from t5_hlr_vector import compute_group_silhouettes_from_raster
    out = compute_group_silhouettes_from_raster(
        (buf, 1.0, 0.0, 0.0), {"hi": [0, 1]})
    assert len(out["hi"]) == 2, (
        f"non-adjacent parts must keep separate loops, got "
        f"{len(out['hi'])}"
    )
