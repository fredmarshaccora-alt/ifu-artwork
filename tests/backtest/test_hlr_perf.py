"""Backtest for bug #3 (HLR perf on Presto).

Per-solid HLRBRep_Algo.Add went O(N^2) on Presto's 700+ solids and
stalled.  We reverted to compound-Add + bbox tagging.  Verify Presto
renders in a reasonable time (< 180s for one view).
"""
from __future__ import annotations
import time
import pytest


def test_presto_compound_add_completes(presto_step):
    """Presto must render via the existing pipeline in under 180s.
    If anyone reverts to per-solid Add on the EXACT path, this stalls."""
    import cadquery as cq
    from t5_hlr_vector import run_hlr_per_solid, rotate_shape

    shape = cq.importers.importStep(str(presto_step)).val().wrapped
    # Presto needs the same pre-rotation the SOURCES list applies
    shape = rotate_shape(shape, (0, 1, 0), -90)

    t0 = time.time()
    parts = run_hlr_per_solid(shape, (0.577, 0.577, 0.577),
                               mesh_defl=1.5, sample_defl=1.0,
                               progress=False)
    elapsed = time.time() - t0
    assert parts, "Presto HLR returned no parts"
    assert elapsed < 180, \
        f"Presto HLR took {elapsed:.0f}s (>180s -- bug #3 regression)"
