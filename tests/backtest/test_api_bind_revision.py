"""Integration tests for Phase D bind_revision endpoint."""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from pathlib import Path


def _req(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    return urllib.request.urlopen(req, timeout=timeout)


def _seed_versions_cache(source_id, versions):
    """Inject a synthetic cached envelope on disk so we don't need
    Onshape live -- the endpoint only needs to find the version_id."""
    here = Path(__file__).resolve().parents[2]
    p = here / "out" / "revisions" / f"{source_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "source_id": source_id,
        "last_fetched_at": "2026-05-16T12:00:00Z",
        "versions": versions,
    }, indent=2), encoding="utf-8")


def test_bind_revision_writes_metadata_and_audit(server_url):
    _seed_versions_cache("siderail", [
        {"id": "vX9", "name": "R03", "created_at": "2026-05-15",
         "microversion": "mvX9"},
        {"id": "vX8", "name": "R02", "created_at": "2026-04-12",
         "microversion": "mvX8"},
    ])
    # Create figure
    r = _req("POST", server_url + "/api/figures",
              {"name": "bind-test", "source_id": "siderail"})
    fid = json.loads(r.read())["id"]
    try:
        # Bind to R02
        r = _req("POST", server_url + f"/api/figures/{fid}/bind_revision",
                  {"version_id": "vX8"})
        assert r.status == 200
        fig = json.loads(r.read())
        assert fig["bound_revision"]["id"] == "vX8"
        assert fig["bound_revision"]["name"] == "R02"
        assert fig["audit"][-1]["what"] == "bind_revision"
        assert fig["audit"][-1]["version_id"] == "vX8"

        # revision_status now shows "1 behind"
        r = _req("GET", server_url + f"/api/figures/{fid}/revision_status")
        st = json.loads(r.read())
        assert st["versions_behind"] == 1
        assert st["latest_revision"]["id"] == "vX9"
    finally:
        _req("DELETE", server_url + f"/api/figures/{fid}")


def test_bind_revision_requires_version_id(server_url):
    r = _req("POST", server_url + "/api/figures",
              {"name": "no-vid", "source_id": "siderail"})
    fid = json.loads(r.read())["id"]
    try:
        try:
            _req("POST", server_url + f"/api/figures/{fid}/bind_revision",
                  {})
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        _req("DELETE", server_url + f"/api/figures/{fid}")


def test_bind_revision_rejects_unknown_version_id(server_url):
    _seed_versions_cache("siderail", [{"id": "real", "name": "R01"}])
    r = _req("POST", server_url + "/api/figures",
              {"name": "unknown-v", "source_id": "siderail"})
    fid = json.loads(r.read())["id"]
    try:
        try:
            _req("POST", server_url + f"/api/figures/{fid}/bind_revision",
                  {"version_id": "ghost"})
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        _req("DELETE", server_url + f"/api/figures/{fid}")
