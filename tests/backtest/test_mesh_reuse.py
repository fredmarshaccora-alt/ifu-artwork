"""Regression: _ensure_meshed skips redundant BRepMesh calls.

After /api/render runs the OCCT shape is meshed at e.g. 0.4 mm. The
follow-up /api/part_footprints (or background prefetch) calls the
mesher again at a coarser deflection (e.g. 0.6 mm); OCCT would
re-walk every face to do nothing. The registry skips the call.
"""
from __future__ import annotations


def test_ensure_meshed_skips_when_already_meshed_finer():
    """Track via the registry that a coarser request after a fine
    request doesn't redo the work."""
    from t5_hlr_vector import (
        _ensure_meshed,
        _MESH_DEFL_REGISTRY,
        _MESH_DEFL_LOCK,
    )

    class _FakeShape:
        """OCCT shape stand-in. _ensure_meshed only needs id() to
        differ between objects; the real BRepMesh call will fail
        on a non-shape, so we monkey-patch it for the test."""

    import t5_hlr_vector as mod
    calls = {"n": 0}
    orig = mod.BRepMesh_IncrementalMesh

    def _spy(shape, defl, *a, **kw):
        calls["n"] += 1
        # Don't call the real mesher
        return None

    mod.BRepMesh_IncrementalMesh = _spy
    try:
        shp = _FakeShape()
        sid = id(shp)
        # Clean state
        with _MESH_DEFL_LOCK:
            _MESH_DEFL_REGISTRY.pop(sid, None)

        # First call meshes
        _ensure_meshed(shp, 0.4)
        assert calls["n"] == 1, "first call must mesh"

        # Same deflection -> skipped
        _ensure_meshed(shp, 0.4)
        assert calls["n"] == 1, "same defl should be skipped"

        # Coarser deflection -> skipped (existing mesh is finer)
        _ensure_meshed(shp, 0.6)
        assert calls["n"] == 1, "coarser defl should be skipped"

        # Finer deflection -> meshes again (existing isn't fine enough)
        _ensure_meshed(shp, 0.2)
        assert calls["n"] == 2, "finer defl must re-mesh"

        # Now even coarser than the original -> still skipped
        _ensure_meshed(shp, 1.0)
        assert calls["n"] == 2, "coarser-than-finest should be skipped"
    finally:
        mod.BRepMesh_IncrementalMesh = orig
        with _MESH_DEFL_LOCK:
            _MESH_DEFL_REGISTRY.pop(sid, None)


def test_invalidate_mesh_cache_forces_remesh():
    from t5_hlr_vector import (
        _ensure_meshed,
        _invalidate_mesh_cache,
        _MESH_DEFL_REGISTRY,
        _MESH_DEFL_LOCK,
    )
    import t5_hlr_vector as mod

    class _FakeShape: pass
    calls = {"n": 0}
    orig = mod.BRepMesh_IncrementalMesh
    mod.BRepMesh_IncrementalMesh = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    try:
        shp = _FakeShape()
        _ensure_meshed(shp, 0.4)
        assert calls["n"] == 1
        _ensure_meshed(shp, 0.4)
        assert calls["n"] == 1
        _invalidate_mesh_cache(shp)
        _ensure_meshed(shp, 0.4)
        assert calls["n"] == 2, "invalidate should force re-mesh"
    finally:
        mod.BRepMesh_IncrementalMesh = orig
        with _MESH_DEFL_LOCK:
            _MESH_DEFL_REGISTRY.pop(id(shp), None)
