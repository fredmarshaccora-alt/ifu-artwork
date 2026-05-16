"""Backtest for P2.a (region render endpoint).

/api/render_region must:
  - accept either {eye,target} or {view_dir, focal} (same grammar as /api/render)
  - return SVG with FEWER parts than the full-assembly render
  - actually be FASTER than a full render at the same detail (sanity)
  - return SVG containing path data inside the requested bbox_uv
"""
from __future__ import annotations
import json
import re
import time
import urllib.request
import pytest


def _post_json(url, body, timeout=180):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def test_region_render_returns_svg(server_url):
    """Smoke test: render a generous region of the siderail iso view.

    The bbox is intentionally huge so it captures something regardless
    of where the source's solids project in (u,v) space.  The endpoint's
    filter logic is checked in test_region_render_is_smaller_than_full.
    """
    body = {
        "file_id": "siderail",
        "view_dir": [0.577, -0.577, 0.577],
        "focal": [0, 0, 0],
        "bbox_uv": [-10000, -10000, 10000, 10000],
        "mesh_defl": 0.8, "sample_defl": 1.0,
    }
    try:
        resp = _post_json(server_url + "/api/render_region", body)
    except urllib.error.HTTPError as e:
        if "unknown source" in e.read().decode():
            pytest.skip("siderail not loaded on server")
        raise
    assert resp.status == 200
    data = resp.read()
    assert data.startswith(b"<?xml") or data.startswith(b"<svg")
    # Header reports part count -- should be > 0 for a reasonable region
    n_parts = int(resp.headers.get("X-Region-Parts", "0"))
    assert n_parts > 0, "region render returned 0 parts"


def test_region_render_is_smaller_than_full(server_url):
    """A region render should produce strictly fewer paths than a full
    render of the same view (otherwise the region filter isn't working)."""
    cam = {
        "view_dir": [0.577, -0.577, 0.577],
        "focal": [0, 0, 0],
    }
    try:
        full = _post_json(server_url + "/api/render",
                           {"file_id": "siderail", **cam}).read()
        # Use a deliberately TIGHT bbox to force the filter to keep
        # strictly fewer parts.  Coordinates are in projector u,v
        # space; for siderail iso the assembly spans roughly +/- 2000.
        region = _post_json(server_url + "/api/render_region",
                             {"file_id": "siderail", **cam,
                              "bbox_uv": [0, 0, 500, 500],
                              "mesh_defl": 0.8, "sample_defl": 1.0}).read()
    except urllib.error.HTTPError as e:
        if "unknown source" in e.read().decode():
            pytest.skip("siderail not loaded on server")
        raise
    full_paths = full.count(b"<path")
    region_paths = region.count(b"<path")
    assert region_paths < full_paths, \
        f"region ({region_paths} paths) should be smaller than " \
        f"full ({full_paths} paths) -- bbox filter broken?"


def test_region_render_rejects_bad_bbox(server_url):
    """Server-side validation on bbox_uv shape + ordering."""
    bad = [
        {"file_id": "siderail", "view_dir": [1, 0, 0],
         "bbox_uv": "not a list"},
        {"file_id": "siderail", "view_dir": [1, 0, 0],
         "bbox_uv": [1, 2, 3]},                       # wrong length
        {"file_id": "siderail", "view_dir": [1, 0, 0],
         "bbox_uv": [100, 100, 50, 50]},              # min > max
    ]
    for body in bad:
        try:
            _post_json(server_url + "/api/render_region", body, timeout=10)
            assert False, f"expected 400 for {body}"
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"want 400 for {body}, got {e.code}"
