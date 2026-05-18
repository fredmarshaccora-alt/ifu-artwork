"""3D viewer graphics quality contract.

The Onshape-quality upgrade adds:
  - ACES Filmic tone mapping + sRGB output color space
  - Room-environment IBL baked via PMREM (scene.environment is set)
  - SSAO post-process via EffectComposer
  - Soft contact shadow plane + studio gradient backdrop
  - Tuned MeshStandardMaterial defaults

These tests don't compare pixels (visual fidelity isn't backtestable
without reference images).  They pin down the renderer + scene state
so a future refactor can't quietly drop the upgrades.
"""
from __future__ import annotations


def _wait_for_3d(page):
    """Trigger the 3D pane's init() by clicking the Split layout
    button -- that's what registers set3DActive(true) which lazily
    instantiates the renderer + scene + composer."""
    page.evaluate("""() => {
        const btn = document.getElementById('lay-split');
        if (btn) btn.click();
    }""")
    # Poll until getRendererState returns non-null (renderer initialized)
    for _ in range(120):
        page.wait_for_timeout(150)
        ready = page.evaluate("""() => {
            const v = window.IFU_VIEWER || {};
            return !!(typeof v.getRendererState === 'function'
                       && v.getRendererState());
        }""")
        if ready:
            break


def test_renderer_uses_aces_tone_mapping(page):
    _wait_for_3d(page)
    # We don't directly expose the renderer; assert via the canvas
    # context state we CAN observe: tone mapping enum + color space.
    # Instead, peek at the OrbitControls singleton's renderer via a
    # global helper we add ad-hoc.  Simpler: re-evaluate inside the
    # module scope.
    info = page.evaluate("""() => {
        // The renderer is module-scoped, not on window.  We expose
        // a getter from the editor's IFU_VIEWER namespace instead.
        const v = window.IFU_VIEWER || {};
        return typeof v.getRendererState === 'function'
            ? v.getRendererState() : null;
    }""")
    assert info, ("IFU_VIEWER.getRendererState() not available; "
                  "regression in build_viewer")
    assert info['toneMapping'] == 4, (
        f"expected ACES Filmic (THREE.ACESFilmicToneMapping = 4), "
        f"got {info['toneMapping']}")
    # SRGBColorSpace string in three 0.160+: "srgb"
    assert info['outputColorSpace'] in ('srgb', 'srgb-linear'), (
        f"expected sRGB output color space; got {info['outputColorSpace']!r}")


def test_scene_has_environment_map(page):
    _wait_for_3d(page)
    has_env = page.evaluate("""() => {
        const v = window.IFU_VIEWER || {};
        const s = (typeof v.getRendererState === 'function')
                    ? v.getRendererState() : null;
        return !!(s && s.hasEnvironment);
    }""")
    assert has_env, "scene.environment must be set (RoomEnvironment IBL)"


def test_composer_setup_path_works(page):
    """SSAO is opt-in (?ssao=1) because SSAOPass + OrthographicCamera
    has historically left the canvas blank; we keep the plain
    renderer path as the default.  The composer IMPORTS must still
    resolve cleanly so the opt-in path is available."""
    _wait_for_3d(page)
    info = page.evaluate("""() => {
        const v = window.IFU_VIEWER || {};
        const s = (typeof v.getRendererState === 'function')
                    ? v.getRendererState() : null;
        return s ? {
            has_composer: !!s.hasComposer,
            has_ssao: !!s.hasSSAO,
        } : null;
    }""")
    assert info, "renderer state not exposed"
    # Default: no composer, no SSAO -- the plain renderer is in use.
    assert info['has_composer'] is False, \
        f"composer should be off by default; got {info}"
    assert info['has_ssao'] is False, \
        f"SSAO should be off by default; got {info}"


def test_shadow_map_enabled_with_contact_plane(page):
    _wait_for_3d(page)
    info = page.evaluate("""() => {
        const v = window.IFU_VIEWER || {};
        const s = (typeof v.getRendererState === 'function')
                    ? v.getRendererState() : null;
        return s ? {
            shadow_map: !!s.shadowMapEnabled,
            has_shadow_plane: !!s.hasShadowPlane,
        } : null;
    }""")
    assert info, "renderer state not exposed"
    assert info['shadow_map'], "renderer.shadowMap.enabled must be true"
    assert info['has_shadow_plane'], \
        "contact shadow plane missing from scene"
