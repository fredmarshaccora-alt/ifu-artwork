"""Regression test: after the ViewHelper renders, the renderer's
viewport must be restored to the full canvas.

If it's not, subsequent main-scene renders draw into the gizmo's
tiny corner viewport and the model becomes invisible -- the bug
reported right after Phase E shipped.
"""
from __future__ import annotations


def test_renderer_viewport_restores_after_gizmo(page):
    """Activate 3D, wait for a few frames, then read back the renderer's
    current viewport.  Must fill the canvas, not the corner."""
    page.evaluate("""() => {
        document.getElementById('file-sel').value = 'siderail';
        document.getElementById('file-sel').dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(800)
    page.click("#lay-3d")
    # Give it a few frames to render through one main + viewHelper cycle
    page.wait_for_timeout(2500)
    info = page.evaluate("""() => {
        const fn = window.IFU_VIEWER && window.IFU_VIEWER._renderer;
        const r = typeof fn === 'function' ? fn() : null;
        if (!r) return null;
        // three.js stores last viewport in a Vector4 on the renderer
        const vp = new THREE.Vector4();
        r.getViewport(vp);
        const c = r.domElement;
        return {
            vpx: vp.x, vpy: vp.y, vpw: vp.z, vph: vp.w,
            canvasW: c.width, canvasH: c.height,
        };
    }""")
    assert info is not None, "renderer not accessible"
    # Viewport width should be close to the canvas width (>= 80%).
    # ViewHelper's corner is ~128 px; a regression would leave vpw==128.
    assert info["vpw"] >= info["canvasW"] * 0.8, \
        f"renderer viewport {info['vpw']} much smaller than canvas " \
        f"{info['canvasW']} -- gizmo viewport leaked (regression)"
    assert info["vph"] >= info["canvasH"] * 0.8, \
        f"renderer viewport height {info['vph']} much smaller than canvas " \
        f"{info['canvasH']}"
