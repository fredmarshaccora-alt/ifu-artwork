"""Regression: /api/baked_svg/<fid>/<vid> serves the on-disk baked SVG
that build_viewer.py no longer inlines into viewer.html.

The lazy-loading is what gets viewer.html from ~26 MB down to ~385 KB,
so the endpoint needs to:
  - 200 + image/svg+xml for known (fid, vid)
  - 404 with a clear error body for unknown ids
  - sanitise path components to block traversal
  - set Cache-Control: public so the browser caches the response
"""
from __future__ import annotations
from pathlib import Path


def _have_siderail_svg() -> bool:
    return (Path(__file__).parents[2] / "out" / "siderail__iso.svg").exists()


def test_baked_svg_200_with_svg_mime():
    import pytest
    if not _have_siderail_svg():
        pytest.skip("out/siderail__iso.svg not on disk")
    import serve
    client = serve.app.test_client()
    r = client.get("/api/baked_svg/siderail/iso")
    assert r.status_code == 200
    assert r.headers.get("Content-Type", "").startswith("image/svg")
    assert r.data[:5] in (b"<?xml", b"<svg ")
    cache = r.headers.get("Cache-Control", "")
    assert "public" in cache, cache
    assert "max-age" in cache, cache


def test_baked_svg_404_for_unknown_ids():
    import serve
    client = serve.app.test_client()
    r = client.get("/api/baked_svg/does-not-exist/iso")
    assert r.status_code == 404
    body = r.get_json() or {}
    assert "error" in body


def test_baked_svg_sanitises_traversal_attempts():
    """URL-encoded ../ shouldn't escape the out/ directory."""
    import serve
    client = serve.app.test_client()
    # Flask matches "../" as a 405/404 long before our handler runs;
    # any non-200 is acceptable, but the path MUST NOT serve a file
    # from outside out/.  We only assert "did not serve a real file".
    r = client.get("/api/baked_svg/..%2F..%2Fetc/passwd")
    assert r.status_code != 200, (
        "traversal request returned 200 -- handler is unsafe"
    )
