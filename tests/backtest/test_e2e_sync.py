"""Backtest for P1.a (3D <-> 2D camera sync).

When the user picks a different 2D view from the dropdown, the 3D
camera should rotate to face that view direction.  No "view in 3D"
button required -- the dropdown IS the cue.
"""
from __future__ import annotations
import math
import pytest


def test_view_change_snaps_3d_camera(page):
    """Switching from iso to front in the view dropdown should swing the
    3D camera so its (position - target) is aligned with the front
    view_dir (0, -1, 0.25)."""
    # Activate 3D so the camera exists
    page.evaluate("""() => {
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(800)
    page.click("#lay-3d")
    page.wait_for_function("""() => {
        const fn = window.IFU_VIEWER && window.IFU_VIEWER._camera;
        return typeof fn === 'function' && !!fn();
    }""", timeout=30_000)
    page.wait_for_timeout(300)
    # Pick the front view
    page.evaluate("""() => {
        const v = document.getElementById('view-sel');
        v.value = 'front';
        v.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(400)
    # Read back the camera direction
    info = page.evaluate("""() => {
        const camFn = window.IFU_VIEWER._camera;
        const cam = camFn();
        if (!cam) return null;
        // controls.target is exposed via the orbit controls; we use
        // (position - lookAt) since lookAt = controls.target.
        // The 3D code stores controls in module scope; we don't expose
        // it, but cam.position - <reasonable lookAt> is fine: we set
        // target=(0,0,0) in snapCameraTo.
        const p = cam.position;
        const len = Math.hypot(p.x, p.y, p.z) || 1;
        return {dx: p.x/len, dy: p.y/len, dz: p.z/len};
    }""")
    assert info is not None
    # front view_dir is (0, -1, 0.25) normalised ~= (0, -0.970, 0.243)
    expected = (0.0, -1.0/math.hypot(1, 0.25), 0.25/math.hypot(1, 0.25))
    for axis, got, exp in zip("xyz", (info["dx"], info["dy"], info["dz"]),
                               expected):
        assert math.isclose(got, exp, abs_tol=0.05), \
            f"camera {axis} dir {got:.3f} != expected {exp:.3f} (P1.a regression)"


def test_iso_view_default_position(page):
    """Initial 3D camera position should match the iso view direction
    (default view on load)."""
    page.evaluate("""() => {
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(800)
    page.click("#lay-3d")
    page.wait_for_function("""() => {
        const fn = window.IFU_VIEWER && window.IFU_VIEWER._camera;
        return typeof fn === 'function' && !!fn();
    }""", timeout=30_000)
    page.wait_for_timeout(300)
    # Force iso (in case load defaulted to something else)
    page.evaluate("""() => {
        const v = document.getElementById('view-sel');
        v.value = 'iso';
        v.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(400)
    info = page.evaluate("""() => {
        const cam = window.IFU_VIEWER._camera();
        const p = cam.position;
        const len = Math.hypot(p.x, p.y, p.z) || 1;
        return {dx: p.x/len, dy: p.y/len, dz: p.z/len};
    }""")
    # iso view_dir is (-0.5, -1, 0.7); normalised
    n = math.hypot(-0.5, -1, 0.7)
    expected = (-0.5/n, -1.0/n, 0.7/n)
    for axis, got, exp in zip("xyz", (info["dx"], info["dy"], info["dz"]),
                               expected):
        assert math.isclose(got, exp, abs_tol=0.05), \
            f"camera {axis} dir {got:.3f} != iso expected {exp:.3f}"
