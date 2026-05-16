"""Backtest for bug #9 (dedup precision).

precision=2 (0.01mm) left ~30% duplicates because OCCT float jitter
between extractions exceeds 0.01mm.  precision=1 (0.1mm) catches the
jitter and dedupes correctly.  Verify the implementation is at
precision=1 and a synthetic test catches the right ratio.
"""
from __future__ import annotations
import pytest

from t5_hlr_vector import _dedup_polylines_in_place


def test_precision_1_dedups_floating_jitter():
    """Two near-identical polylines (differ by 0.05mm jitter) should
    dedupe at precision=1 (0.1mm tolerance)."""
    # Jitter is well within 0.05mm so float-banker rounding can't bump
    # any value across a tenth-of-a-mm boundary.
    parts = [{
        "idx": 0, "label": "p0",
        "polys": {
            "outline_v": [
                [(0.00, 0.00), (10.00, 0.00), (10.00, 10.00)],
                # Same polyline with tiny float jitter (<= 0.03)
                [(0.02, 0.01), (9.98, 0.02), (10.01, 9.98)],
            ],
        },
    }]
    n_total, n_kept, _n_degen = _dedup_polylines_in_place(parts, precision=1)
    assert n_total == 2
    assert n_kept == 1, \
        f"precision=1 should dedupe near-identical polylines, kept {n_kept}"


def test_precision_2_keeps_jittered_duplicates():
    """At precision=2 (0.01mm), the jitter polyline is NOT recognised
    as a duplicate.  This is the regression scenario: SVG bloat would
    happen here."""
    parts = [{
        "idx": 0, "label": "p0",
        "polys": {
            "outline_v": [
                [(0.00, 0.00), (10.00, 0.00), (10.00, 10.00)],
                [(0.02, 0.01), (9.98, 0.02), (10.01, 9.98)],
            ],
        },
    }]
    n_total, n_kept, _n_degen = _dedup_polylines_in_place(parts, precision=2)
    assert n_total == 2
    assert n_kept == 2, \
        "precision=2 should NOT dedupe (this verifies the bug existed)"


def test_default_precision_is_1():
    """Verify the default arg of _dedup_polylines_in_place is still 1.
    If someone changes it back to 2, SVG sizes will double."""
    import inspect
    sig = inspect.signature(_dedup_polylines_in_place)
    assert sig.parameters["precision"].default == 1, \
        "default precision must remain 1 (see bug #9)"
