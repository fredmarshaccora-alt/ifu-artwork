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


# ----- /api/onshape/probe (G.4) -------------------------------------

def test_probe_rejects_empty_url(server_url):
    import requests
    r = requests.post(f"{server_url}/api/onshape/probe", json={},
                       timeout=10)
    assert r.status_code == 400
    assert "url required" in r.json().get("error", "").lower()


def test_probe_rejects_non_onshape_url(server_url):
    import requests
    r = requests.post(f"{server_url}/api/onshape/probe",
                       json={"url": "https://google.com"}, timeout=10)
    assert r.status_code == 400


def test_probe_rejects_url_without_element_segment(server_url):
    """A URL that parses but has no /e/<eid> should 400 with a
    specific message."""
    import requests
    url = ("https://cad.onshape.com/documents/abc1234567890123/"
           "w/def4567890123456")
    r = requests.post(f"{server_url}/api/onshape/probe",
                       json={"url": url}, timeout=10)
    assert r.status_code == 400
    assert "element" in r.json().get("error", "").lower()


# ----- /api/sources/<id>/configuration (G.3) ------------------------

def test_configuration_404_for_unknown_source(server_url):
    import requests
    r = requests.get(
        f"{server_url}/api/sources/nope_does_not_exist/configuration",
        timeout=10)
    assert r.status_code == 404


def test_configuration_empty_for_local_source(server_url):
    """A static source without onshape_ids (e.g. siderail) returns
    has_config: false / empty parameters -- no Onshape call made."""
    import requests
    r = requests.get(
        f"{server_url}/api/sources/siderail/configuration",
        timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body.get("has_config") is False
    assert body.get("parameters") == []
