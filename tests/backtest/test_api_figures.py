"""Integration tests for the Phase A figures CRUD endpoints."""
from __future__ import annotations
import json
import urllib.request
import urllib.error
import pytest


def _req(method, url, body=None, timeout=10):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    return urllib.request.urlopen(req, timeout=timeout)


def test_create_get_update_delete(server_url):
    """Full lifecycle: POST -> GET -> PUT -> GET -> DELETE -> GET 404."""
    # Create
    r = _req("POST", server_url + "/api/figures",
              {"name": "API smoke", "source_id": "siderail",
               "view_id": "front", "selection": [3, 7],
               "notes": "from the integration test"})
    assert r.status == 201
    fig = json.loads(r.read())
    fid = fig["id"]
    assert fig["name"] == "API smoke"
    assert fig["selection"] == [3, 7]

    # Get
    r = _req("GET", server_url + f"/api/figures/{fid}")
    assert r.status == 200
    fetched = json.loads(r.read())
    assert fetched["id"] == fid

    # Update
    fetched["notes"] = "edited"
    fetched["selection"] = [3, 7, 12]
    r = _req("PUT", server_url + f"/api/figures/{fid}", fetched)
    assert r.status == 200
    updated = json.loads(r.read())
    assert updated["notes"] == "edited"
    assert updated["selection"] == [3, 7, 12]

    # Delete
    r = _req("DELETE", server_url + f"/api/figures/{fid}")
    assert r.status == 204

    # Get -> 404
    try:
        _req("GET", server_url + f"/api/figures/{fid}")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_create_requires_source_id(server_url):
    try:
        _req("POST", server_url + "/api/figures", {"name": "no source"})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_list_includes_newly_created(server_url):
    r = _req("POST", server_url + "/api/figures",
              {"name": "list-me", "source_id": "siderail"})
    fid = json.loads(r.read())["id"]
    try:
        r = _req("GET", server_url + "/api/figures")
        figs = json.loads(r.read())["figures"]
        assert any(f["id"] == fid for f in figs), \
            f"new figure {fid} missing from list response"
    finally:
        _req("DELETE", server_url + f"/api/figures/{fid}")
