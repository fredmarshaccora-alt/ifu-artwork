"""Background footprint raster runs as part of /api/render.

Two contracts:
  1. After /api/render returns, _FOOT_RASTER_INFLIGHT or _FOOT_RASTER_DONE
     for that view is True -- the raster has been kicked off.
  2. A subsequent /api/part_footprints call against the same view
     shows ``stats.raster_inflight`` or ``stats.raster_done`` in the
     response so the client can render a "loading" badge.
"""
from __future__ import annotations
import time
import requests


def test_render_kicks_off_background_raster(server_url):
    # Use a non-default camera so we don't reuse a cached raster
    # state from previous tests
    cam = {
        "file_id": "siderail",
        "eye":    [950, -1450, 720],   # arbitrary, unlikely-cached angle
        "target": [0, 0, 0],
    }
    r = requests.post(f"{server_url}/api/render", json=cam, timeout=120)
    assert r.status_code == 200, r.text

    # Within ~3 seconds the part_footprints stats must report either
    # raster_inflight=True (still running) OR raster_done=True
    # (already finished -- possible if siderail mesh is small).
    deadline = time.time() + 10
    saw_inflight_or_done = False
    while time.time() < deadline:
        r2 = requests.post(f"{server_url}/api/part_footprints", json={
            **cam, "part_indices": [0]
        }, timeout=120)
        if r2.status_code == 200:
            stats = r2.json().get("stats") or {}
            if stats.get("raster_inflight") or stats.get("raster_done"):
                saw_inflight_or_done = True
                break
        time.sleep(0.5)
    assert saw_inflight_or_done, (
        "After /api/render, footprint stats never showed raster_inflight "
        "or raster_done -- background prefetch isn't running")


def test_part_footprints_response_exposes_raster_flags(server_url):
    """The shape of the response must include raster_inflight and
    raster_done so the client UI can show 'loading...' vs 'done'."""
    cam = {
        "file_id": "siderail",
        "eye": [880, -1320, 660], "target": [0, 0, 0],
    }
    requests.post(f"{server_url}/api/render", json=cam, timeout=120)
    r = requests.post(f"{server_url}/api/part_footprints", json={
        **cam, "part_indices": [0, 1, 2]
    }, timeout=120)
    assert r.status_code == 200
    stats = r.json().get("stats") or {}
    assert "raster_inflight" in stats, \
        f"raster_inflight missing from stats: {stats}"
    assert "raster_done" in stats, \
        f"raster_done missing from stats: {stats}"
    assert isinstance(stats["raster_inflight"], bool)
    assert isinstance(stats["raster_done"], bool)
