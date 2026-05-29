"""G.6 unit: STEP files that list each part as a top-level sibling
(Onshape part-studio export shape) must load as a Compound that
exposes every solid -- not just the first one.

This regression check pins down the .vals() vs .val() bug that caused
the user to import a part studio with 10 parts and see only 1 rivet.
"""
from __future__ import annotations
import pytest
from pathlib import Path


def _have_step(path):
    return Path(path).exists()


def test_load_step_as_compound_handles_multi_solid(tmp_path):
    """Synthesise a tiny multi-solid STEP and verify _load_step_as_compound
    sees every solid."""
    import cadquery as cq
    from serve import _load_step_as_compound
    from t5_hlr_vector import split_solids

    # Three little boxes side-by-side, exported as a Workplane so each
    # ends up as its own top-level solid in the STEP (no enclosing
    # compound).  This matches Onshape's part-studio export shape.
    wp = cq.Workplane("XY").box(10, 10, 10) \
        .union(cq.Workplane("XY").box(10, 10, 10).translate((20, 0, 0))) \
        .union(cq.Workplane("XY").box(10, 10, 10).translate((40, 0, 0)))
    # Force three siblings: use exporters with assemblies
    asm = cq.Assembly()
    for i in range(3):
        asm.add(cq.Workplane("XY").box(5, 5, 5).translate((i * 20, 0, 0)),
                 name=f"box{i}")
    out = tmp_path / "tri.step"
    asm.save(str(out), "STEP")

    shape = _load_step_as_compound(out)
    solids = list(split_solids(shape))
    assert len(solids) >= 3, \
        f"expected at least 3 solids from the multi-part STEP; got {len(solids)}"


def test_load_step_as_compound_handles_single_solid(tmp_path):
    """A regular single-solid STEP still loads as a usable shape."""
    import cadquery as cq
    from serve import _load_step_as_compound
    from t5_hlr_vector import split_solids

    wp = cq.Workplane("XY").box(20, 10, 5)
    out = tmp_path / "one.step"
    cq.exporters.export(wp, str(out))

    shape = _load_step_as_compound(out)
    solids = list(split_solids(shape))
    assert len(solids) == 1


def test_load_step_as_compound_rejects_empty_step(tmp_path):
    """An empty / corrupt STEP raises rather than silently returning
    a useless shape."""
    from serve import _load_step_as_compound
    out = tmp_path / "garbage.step"
    out.write_text("not a valid STEP file", encoding="utf-8")
    with pytest.raises(Exception):
        _load_step_as_compound(out)


def test_load_step_as_compound_rejects_truncated_step(tmp_path):
    """A truncated STEP (network blip mid-download) should fail loudly
    rather than being registered as a usable source."""
    from serve import _load_step_as_compound
    out = tmp_path / "tiny.step"
    out.write_bytes(b"")  # 0 bytes -- the worst case
    with pytest.raises(RuntimeError, match="too small"):
        _load_step_as_compound(out)


def test_load_step_as_compound_rejects_zero_solid_step(tmp_path):
    """A valid STEP wrapping an empty assembly (no solids inside)
    must fail rather than register a broken source.  This is the
    Onshape-empty-partstudio shape."""
    import cadquery as cq
    from serve import _load_step_as_compound

    asm = cq.Assembly()  # no parts added
    out = tmp_path / "empty.step"
    try:
        asm.save(str(out), "STEP")
    except Exception:
        pytest.skip("cadquery refused to write an empty assembly STEP")

    if not out.exists() or out.stat().st_size < 200:
        pytest.skip("empty-assembly STEP wasn't written large enough to test")

    with pytest.raises(RuntimeError):
        _load_step_as_compound(out)
