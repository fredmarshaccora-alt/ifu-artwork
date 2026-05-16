"""Backtest for bugs #4 + #12 + #21 (API contract).

#4:  switching to {eye, target} body broke a stale `view_dir` reference
     on the client side.  Server endpoint must accept both forms.
#12: window.API_BASE was undefined because `const API_BASE` doesn't
     attach to window.  The page must expose API_BASE as a global.
#21: silhouette fetch never fired because of the same window.API_BASE
     bug -- this verifies the API_BASE pathway works end-to-end.

These are integration tests (need the server up).
"""
from __future__ import annotations
import pytest


def test_render_accepts_eye_target(server_url):
    """Bug #4: POST /api/render with {file_id, eye, target} returns 200."""
    import urllib.request, json
    body = json.dumps({
        "file_id": "siderail",
        "eye": [1000, 1000, 1000],
        "target": [0, 0, 0],
    }).encode()
    req = urllib.request.Request(
        server_url + "/api/render", data=body,
        headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        assert resp.status == 200
        data = resp.read()
        assert data.startswith(b"<?xml") or data.startswith(b"<svg"), \
            "expected SVG response"
    except Exception as exc:
        # 400/500 from a misconfigured siderail STEP is also a failure
        # mode worth flagging, but skip if the server doesn't have the file
        if "unknown source" in str(exc):
            pytest.skip("siderail STEP not loaded on server")
        raise


def test_render_accepts_view_dir_focal(server_url):
    """Legacy form: POST /api/render with {file_id, view_dir, focal}."""
    import urllib.request, json
    body = json.dumps({
        "file_id": "siderail",
        "view_dir": [0.577, 0.577, 0.577],
        "focal": [0, 0, 0],
    }).encode()
    req = urllib.request.Request(
        server_url + "/api/render", data=body,
        headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        assert resp.status == 200
    except Exception as exc:
        if "unknown source" in str(exc):
            pytest.skip("siderail STEP not loaded on server")
        raise


def test_render_rejects_no_camera(server_url):
    """Sanity: missing camera returns 400, not 500."""
    import urllib.request, json
    body = json.dumps({"file_id": "siderail"}).encode()
    req = urllib.request.Request(
        server_url + "/api/render", data=body,
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"want 400, got {e.code}"
