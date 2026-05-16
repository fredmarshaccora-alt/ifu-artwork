"""Backtest for bug #22 (Contesa bbox-tagging mis-attribution).

For sources where the exact HLRBRep_Algo path is taken (Presto), edges
are tagged to parts by bbox containment.  Tier 1 (full polyline bbox
containment, smallest area wins) is the new primary; centroid + nearest
are fallbacks.

Verify that for Contesa (which uses PolyAlgo, so per-part extract is
direct) NO polyline appears in two parts -- i.e. dedup invariant holds.
"""
from __future__ import annotations
import pytest


def test_no_polyline_appears_in_two_parts(contesa_step):
    """After dedup, every polyline (by rounded coord sequence) belongs
    to exactly one part."""
    import cadquery as cq
    from t5_hlr_vector import run_hlr_per_solid, rotate_shape

    shape = cq.importers.importStep(str(contesa_step)).val().wrapped
    shape = rotate_shape(shape, (1, 0, 0), 90)
    parts = run_hlr_per_solid(shape, (0.577, 0.577, 0.577),
                               mesh_defl=3.0, sample_defl=1.5,
                               progress=False)
    # Build a (rounded polyline) -> [idx, ...] map; ensure no list has > 1 idx
    appearances: dict = {}
    for p in parts:
        for cat, pls in p["polys"].items():
            for pl in pls:
                key = tuple((round(x, 1), round(y, 1)) for x, y in pl)
                if len(set(key)) < 2:
                    continue   # degenerate
                appearances.setdefault(key, []).append(p["idx"])
    multi = {k: v for k, v in appearances.items() if len(set(v)) > 1}
    assert not multi, \
        f"{len(multi)} polylines tagged to multiple parts (bug #22)"
