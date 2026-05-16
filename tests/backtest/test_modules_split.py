"""Acceptance test for Phase 1a (module split).

The build_viewer.py monolith was split into the `ifu` package.  This
test asserts that:
  - every public name is importable from `ifu` directly (no shim needed)
  - build_viewer's legacy import surface still works (backwards compat)
  - the two paths return THE SAME OBJECT for each name
"""
from __future__ import annotations
import pytest


PUBLIC_NAMES = [
    "SOURCES", "VIEWS", "SOURCE_VIEW_SUBSET", "SOURCE_SKIP_CATEGORIES",
    "HERE", "OUT",
    "solid_mesh_arrays", "_solid_mesh_arrays", "slugify",
    "export_glb_b64",
    "fetch_step_tree", "count_tree",
    "fetch_onshape_tree",
    "generate_svgs",
    "save_catalogue", "load_catalogue",
]


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_importable_from_ifu_package(name):
    """Every public name lives in the ``ifu`` package."""
    import ifu
    assert hasattr(ifu, name), f"ifu has no attribute {name!r}"


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_importable_from_legacy_build_viewer(name):
    """Backwards compat: ``from build_viewer import X`` still works."""
    import build_viewer
    assert hasattr(build_viewer, name), \
        f"build_viewer (shim) lost legacy attr {name!r}"


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_same_object_via_both_paths(name):
    """Verify the shim re-exports the SAME object, not a copy."""
    import ifu
    import build_viewer
    assert getattr(build_viewer, name) is getattr(ifu, name), \
        f"build_viewer.{name} is not ifu.{name} (likely a duplicate definition)"


def test_build_html_still_in_build_viewer():
    """build_html belongs in build_viewer (the JS template needs an
    eventual React rewrite -- splitting now would be churn for no gain)."""
    import build_viewer
    assert callable(build_viewer.build_html), \
        "build_html missing from build_viewer.py"
