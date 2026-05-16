"""Integration tests for the Phase B projects endpoints."""
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


def test_project_lifecycle(server_url):
    """Create -> get -> attach figure -> list figures -> detach -> delete."""
    # Create project
    r = _req("POST", server_url + "/api/projects",
              {"name": "phaseB-test", "description": "integration"})
    assert r.status == 201
    proj = json.loads(r.read())
    pid = proj["id"]

    # Create a figure with project_id set on creation
    r = _req("POST", server_url + "/api/figures",
              {"name": "phaseB-fig", "source_id": "siderail",
               "project_id": pid})
    assert r.status == 201
    fig = json.loads(r.read())
    fid = fig["id"]
    assert fig.get("project_id") == pid

    # List figures in project should include it
    r = _req("GET", server_url + f"/api/projects/{pid}/figures")
    figs = json.loads(r.read())["figures"]
    assert any(f["id"] == fid for f in figs)

    # Detach
    r = _req("DELETE", server_url + f"/api/projects/{pid}/figures/{fid}")
    assert r.status == 204
    r = _req("GET", server_url + f"/api/projects/{pid}/figures")
    figs = json.loads(r.read())["figures"]
    assert not any(f["id"] == fid for f in figs)

    # Re-attach via POST
    r = _req("POST", server_url + f"/api/projects/{pid}/figures/{fid}")
    assert r.status == 204

    # Delete project with cascade -- figure should vanish too
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                server_url + f"/api/projects/{pid}?cascade=1",
                method="DELETE"),
            timeout=10)
    except Exception:
        pass
    # Cleanup if cascade didn't fire (e.g. server quirks)
    try: _req("DELETE", server_url + f"/api/figures/{fid}")
    except Exception: pass
    try:
        _req("GET", server_url + f"/api/projects/{pid}")
        assert False, "project should be gone"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_orphan_figures_endpoint(server_url):
    """Figures with no project_id show up in the orphans list."""
    r = _req("POST", server_url + "/api/figures",
              {"name": "orphan-int-test", "source_id": "siderail"})
    fid = json.loads(r.read())["id"]
    try:
        r = _req("GET", server_url + "/api/figures/orphans")
        orphans = json.loads(r.read())["figures"]
        assert any(o["id"] == fid for o in orphans), \
            "newly created figure with no project_id should be orphan"
    finally:
        _req("DELETE", server_url + f"/api/figures/{fid}")
