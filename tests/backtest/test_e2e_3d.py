"""Backtest for bug #25 (3D viewer quality).

The 3D pane currently has no lighting, no materials, coarse GLB, no
helpers.  Documented in PLAN.md Phase 1.5.  Until that's done, this
test xfails -- when we ship the fix, it flips to xpass and prompts
removal of the xfail mark.
"""
from __future__ import annotations
import pytest


@pytest.mark.xfail(reason="Phase 1.5: 3D viewer rebuild pending")
def test_3d_scene_has_lighting(page):
    """three.js scene should have at least one Light + a material with
    a real lighting model (not the default Lambert flat-color)."""
    page.evaluate("""() => {
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
        // Activate 3D pane
        window.IFU_VIEWER && window.IFU_VIEWER.setLayout && window.IFU_VIEWER.setLayout('3d');
    }""")
    page.wait_for_timeout(2000)
    info = page.evaluate("""() => {
        const scene = window.IFU_VIEWER && window.IFU_VIEWER._scene;
        if (!scene) return null;
        let nLights = 0, nMeshes = 0;
        scene.traverse(o => {
            if (o.isLight) nLights++;
            if (o.isMesh) nMeshes++;
        });
        return {nLights, nMeshes};
    }""")
    assert info is not None
    assert info["nLights"] >= 2, \
        f"expected >= 2 lights (ambient + directional), got {info['nLights']}"
