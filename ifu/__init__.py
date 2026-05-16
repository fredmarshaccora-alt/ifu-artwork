"""IFU artwork pipeline -- modular successor to the build_viewer.py monolith.

Public API (stable surface for serve.py / rebuild_html / tests):

  config:   SOURCES, VIEWS, SOURCE_VIEW_SUBSET, SOURCE_SKIP_CATEGORIES,
            HERE, OUT
  mesh:     solid_mesh_arrays, slugify
  glb:      export_glb_b64
  step_tree:   fetch_step_tree, count_tree
  onshape_tree: fetch_onshape_tree
  svg_bake: generate_svgs
  catalogue: save_catalogue, load_catalogue

The HTML emitter (build_html) is intentionally NOT moved into this
package yet -- it lives in build_viewer.py and depends on a multi-thousand
line JS template that will be replaced by the React frontend in
Phase 3.  Splitting it now would create churn for negative value.
"""
from __future__ import annotations

from .config import (
    HERE, OUT,
    SOURCES, VIEWS, SOURCE_VIEW_SUBSET, SOURCE_SKIP_CATEGORIES,
)
from .mesh import solid_mesh_arrays, slugify, _solid_mesh_arrays
from .glb import export_glb_b64
from .step_tree import fetch_step_tree, count_tree
from .onshape_tree import fetch_onshape_tree
from .svg_bake import generate_svgs
from .catalogue import save_catalogue, load_catalogue

__all__ = [
    "HERE", "OUT",
    "SOURCES", "VIEWS", "SOURCE_VIEW_SUBSET", "SOURCE_SKIP_CATEGORIES",
    "solid_mesh_arrays", "_solid_mesh_arrays", "slugify",
    "export_glb_b64",
    "fetch_step_tree", "count_tree",
    "fetch_onshape_tree",
    "generate_svgs",
    "save_catalogue", "load_catalogue",
]
