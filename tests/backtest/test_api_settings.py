"""Integration tests for the F.1 /api/settings endpoints."""
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


def test_get_returns_defaults(server_url):
    r = _req("GET", server_url + "/api/settings")
    assert r.status == 200
    s = json.loads(r.read())
    assert "default_detail" in s
    assert "default_stroke_color" in s


def test_patch_persists_a_change(server_url):
    # Save current, mutate, verify, restore
    r = _req("GET", server_url + "/api/settings")
    original = json.loads(r.read())
    try:
        r = _req("PATCH", server_url + "/api/settings",
                  {"default_detail": "fine"})
        assert r.status == 200
        after = json.loads(r.read())
        assert after["default_detail"] == "fine"

        r = _req("GET", server_url + "/api/settings")
        confirmed = json.loads(r.read())
        assert confirmed["default_detail"] == "fine"
    finally:
        _req("PATCH", server_url + "/api/settings",
              {"default_detail": original.get("default_detail", "normal")})


def test_put_is_partial_too(server_url):
    """PUT behaves the same as PATCH -- a partial update, not a wholesale
    replacement.  Common API ergonomic for single-tenant tools."""
    r = _req("GET", server_url + "/api/settings")
    original = json.loads(r.read())
    try:
        r = _req("PUT", server_url + "/api/settings",
                  {"default_stroke_width_mm": 7.5})
        assert r.status == 200
        after = json.loads(r.read())
        assert after["default_stroke_width_mm"] == 7.5
        # Other keys preserved
        assert after.get("default_stroke_color") == original.get(
            "default_stroke_color")
    finally:
        _req("PATCH", server_url + "/api/settings",
              {"default_stroke_width_mm":
                  original.get("default_stroke_width_mm", 3.0)})


def test_invalid_body_returns_400(server_url):
    """A bare-string body (not an object) must be rejected."""
    req = urllib.request.Request(
        server_url + "/api/settings", method="PATCH",
        data=b'"just-a-string"',
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400
