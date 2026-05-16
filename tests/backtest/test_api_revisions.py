"""Integration tests for the Phase C revision endpoints.

These don't hit Onshape -- the live refresh endpoint is exercised
only when credentials are configured and the source has Onshape ids.
The cached-data endpoints work with any source.
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error


def _req(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    return urllib.request.urlopen(req, timeout=timeout)


def test_sources_list(server_url):
    """The /api/sources endpoint exposes each SOURCES entry, with the
    onshape_ids field present for sources that have it."""
    r = _req("GET", server_url + "/api/sources")
    assert r.status == 200
    data = json.loads(r.read())
    assert "sources" in data
    by_id = {s["id"]: s for s in data["sources"]}
    # Siderail is in SOURCES but has no Onshape ids
    assert "siderail" in by_id
    assert by_id["siderail"]["onshape_ids"] is None
    # Presto has Onshape ids
    if "presto" in by_id:
        assert by_id["presto"]["onshape_ids"] is not None


def test_versions_list_empty_when_never_refreshed(server_url):
    """GET ../versions should not 500 when the cache is empty -- it
    returns an empty list."""
    r = _req("GET", server_url + "/api/sources/siderail/versions")
    assert r.status == 200
    data = json.loads(r.read())
    assert "versions" in data
    # Versions list may be empty; only check the shape
    assert isinstance(data["versions"], list)


def test_versions_refresh_rejects_non_onshape_source(server_url):
    """siderail has no Onshape ids -- refresh must 400, not 500."""
    try:
        _req("POST", server_url + "/api/sources/siderail/versions/refresh")
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_versions_refresh_rejects_unknown_source(server_url):
    try:
        _req("POST", server_url + "/api/sources/not-a-source/versions/refresh")
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_figure_revision_status_unbound(server_url):
    """A figure with no bound_revision returns nulls (not an error)."""
    # Create a figure
    r = _req("POST", server_url + "/api/figures",
              {"name": "phaseC-rev-test", "source_id": "siderail"})
    fid = json.loads(r.read())["id"]
    try:
        r = _req("GET", server_url + f"/api/figures/{fid}/revision_status")
        assert r.status == 200
        data = json.loads(r.read())
        assert data["figure_id"] == fid
        assert data["source_id"] == "siderail"
        assert data["bound_revision"] is None
        assert data["versions_behind"] is None
    finally:
        _req("DELETE", server_url + f"/api/figures/{fid}")
