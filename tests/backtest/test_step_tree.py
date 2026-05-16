"""Backtest for bug #8 (STEP tree leaf count).

XCAF Part leaves can be multi-body; cadquery's importer flattens them
to N solids.  fetch_step_tree must walk TopAbs_SOLID exploration on
each leaf so the tree-leaf count == cadquery solid count.
"""
from __future__ import annotations
import pytest


def test_siderail_tree_leaves_match_solids(siderail_step):
    """For siderail, the count of leaf-Parts in fetch_step_tree must
    equal the count of cadquery solids."""
    import cadquery as cq
    from build_viewer import fetch_step_tree

    shape = cq.importers.importStep(str(siderail_step)).val().wrapped
    # Count solids the cadquery way
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    n_solids = 0
    while exp.More():
        n_solids += 1
        exp.Next()

    # Count leaves in the STEP tree
    tree = fetch_step_tree(siderail_step)

    def _count_solid_leaves(nodes):
        total = 0
        for n in nodes:
            indices = n.get("_solid_indices") or []
            total += len(indices)
            kids = n.get("children") or []
            total += _count_solid_leaves(kids)
        return total

    n_leaves = _count_solid_leaves(tree)
    assert n_leaves == n_solids, \
        f"tree solid-leaf count {n_leaves} != cadquery solid count {n_solids}"
