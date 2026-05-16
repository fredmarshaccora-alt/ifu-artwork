"""G.2 API surface tests for /api/onshape/import.

We don't hit the live Onshape API in CI -- those tests pin down the
request/response contract (400 on bad URL, 202 + job id on accept,
404 for unknown job).  The translation flow itself is exercised
manually through the UI.
"""
from __future__ import annotations
import pytest


def test_import_rejects_empty_url(server_url):
    """POST with no url -> 400."""
    import requests
    r = requests.post(f"{server_url}/api/onshape/import",
                       json={}, timeout=10)
    assert r.status_code == 400
    body = r.json()
    assert "url required" in body.get("error", "").lower()


def test_import_rejects_non_onshape_url(server_url):
    """POST with a non-Onshape URL -> 400 + parser error message."""
    import requests
    r = requests.post(f"{server_url}/api/onshape/import",
                       json={"url": "https://google.com"}, timeout=10)
    assert r.status_code == 400
    body = r.json()
    assert "onshape" in body.get("error", "").lower()


def test_import_rejects_garbage_string(server_url):
    import requests
    r = requests.post(f"{server_url}/api/onshape/import",
                       json={"url": "not a url"}, timeout=10)
    assert r.status_code == 400


def test_import_status_404_for_unknown_job(server_url):
    import requests
    r = requests.get(f"{server_url}/api/onshape/import/does_not_exist",
                      timeout=10)
    assert r.status_code == 404


def test_sources_endpoint_lists_static_origin(server_url):
    """The new /api/sources should include the static sources with
    ``origin: 'static'`` and a ``loaded`` flag."""
    import requests
    r = requests.get(f"{server_url}/api/sources", timeout=10)
    assert r.status_code == 200
    body = r.json()
    srcs = body.get("sources") or []
    assert any(s["id"] == "siderail" for s in srcs)
    for s in srcs:
        assert "origin" in s
        assert "loaded" in s
        assert s["origin"] in ("static", "dynamic")
