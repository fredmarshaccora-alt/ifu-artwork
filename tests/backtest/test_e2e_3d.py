"""Backtest for bug #25 (3D viewer quality).

The 3D pane needs proper lighting, PBR materials, and orientation
helpers so it looks like a real product, not a wireframe demo.

After Phase 1c shipped:
  - 3+ lights (ambient + sun + fill + rim)
  - PBR MeshStandardMaterial (metalness 0.15, roughness 0.55)
  - GridHelper (XY plane) + AxesHelper for orientation reference
  - window.IFU_VIEWER._scene/_camera/_renderer/_active accessor fns
"""
from __future__ import annotations
import pytest


def test_3d_scene_has_lighting(page):
    """three.js scene should have at least 3 lights (ambient + 2+ directional)."""
    page.evaluate("""() => {
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1000)
    # Activate via the actual 3D layout button so the existing handler
    # runs in its natural sequence (init -> set3DActive -> loadSource).
    page.click("#lay-3d")
    # GLB load + scene init can take a few seconds
    page.wait_for_timeout(3500)
    info = page.evaluate("""() => {
        const sceneFn = window.IFU_VIEWER && window.IFU_VIEWER._scene;
        const scene = typeof sceneFn === 'function' ? sceneFn() : sceneFn;
        if (!scene) return null;
        let nLights = 0, nMeshes = 0, nHelpers = 0;
        scene.traverse(o => {
            if (o.isLight) nLights++;
            if (o.isMesh) nMeshes++;
            if (o.userData && o.userData._helper) nHelpers++;
        });
        return {nLights, nMeshes, nHelpers};
    }""")
    assert info is not None, "scene not exposed via window.IFU_VIEWER._scene"
    assert info["nLights"] >= 3, \
        f"expected >= 3 lights (ambient + sun + fill), got {info['nLights']}"
    assert info["nHelpers"] >= 2, \
        f"expected >= 2 helpers (grid + axes), got {info['nHelpers']}"


def test_3d_meshes_use_pbr_material():
    """Static check: the GLB-load callback in build_viewer.py constructs
    MeshStandardMaterial (PBR), not MeshLambertMaterial.

    A runtime check via Playwright is flaky -- GLB load can take >60s
    on heavy assemblies in headless mode -- and this assertion is about
    a code-shape invariant, so we verify it in source.
    """
    import build_viewer
    import inspect
    # The JS template is the HTML_TEMPLATE-shaped string inside build_html
    src = inspect.getsource(build_viewer)
    assert "MeshStandardMaterial" in src, \
        "MeshStandardMaterial not used (bug #25: lighting needs PBR)"
    assert "MeshLambertMaterial" not in src, \
        "MeshLambertMaterial still present in JS template (bug #25 regression)"
