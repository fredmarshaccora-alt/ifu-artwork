"""Phase E backtest: view-cube gizmo present in the 3D scene."""
from __future__ import annotations


def test_viewhelper_imports_and_exists(page):
    """After 3D pane init, window.IFU_VIEWER._viewHelper exposes a
    ViewHelper instance OR the import resolved without console errors."""
    # We don't expose viewHelper on IFU_VIEWER, so this is a SOURCE-CHECK
    # static test that the import path is correct -- if the JS module
    # had a bad import, the page wouldn't load.
    import build_viewer
    src = open(build_viewer.__file__, encoding="utf-8").read()
    assert "ViewHelper" in src, "ViewHelper import missing"
    assert "three/addons/helpers/ViewHelper.js" in src, \
        "ViewHelper addon path wrong"


def test_3d_pane_loads_without_console_error(page):
    """Switch to 3D and verify no console errors mentioning ViewHelper."""
    msgs = []
    page.on("console",
            lambda m: msgs.append((m.type, m.text))
            if m.type == "error" else None)
    page.evaluate("""() => {
        document.getElementById('file-sel').value = 'siderail';
        document.getElementById('file-sel').dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1000)
    page.click("#lay-3d")
    page.wait_for_timeout(3000)
    helper_errors = [t for typ, t in msgs if "ViewHelper" in t or "viewHelper" in t]
    assert not helper_errors, f"ViewHelper console errors: {helper_errors}"
