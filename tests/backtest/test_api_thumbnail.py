"""G.10 thumbnail roundtrip.

POST a tiny PNG data URL via PUT /api/figures/<fid>/thumbnail, then GET
it back as raw image/png and verify the bytes match.  Plus the safety
rails: 404 on missing figure, 400 on malformed data URLs, 413 when the
payload exceeds the 200 KB cap.
"""
from __future__ import annotations
import base64

# A 1x1 transparent PNG -- smallest valid PNG we can hand-craft.
TINY_PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YA'
    'AAAASUVORK5CYII=')
TINY_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(
    TINY_PNG_BYTES).decode("ascii")


def _make_figure(server_url):
    import requests
    r = requests.post(f"{server_url}/api/figures",
                       json={"name": "thumb-test", "source_id": "siderail"})
    assert r.status_code == 201
    return r.json()["id"]


def _delete_figure(server_url, fid):
    import requests
    requests.delete(f"{server_url}/api/figures/{fid}")


def test_thumbnail_404_for_unknown_figure(server_url):
    import requests
    r = requests.get(f"{server_url}/api/figures/nope_X/thumbnail")
    assert r.status_code == 404


def test_thumbnail_put_404_for_unknown_figure(server_url):
    import requests
    r = requests.put(f"{server_url}/api/figures/nope_X/thumbnail",
                       json={"data_url": TINY_PNG_DATA_URL})
    assert r.status_code == 404


def test_thumbnail_put_400_on_bad_data_url(server_url):
    import requests
    fid = _make_figure(server_url)
    try:
        for bad in ("", "not a data url", "data:text/plain,hello", "data:image/png;,"):
            r = requests.put(f"{server_url}/api/figures/{fid}/thumbnail",
                              json={"data_url": bad})
            assert r.status_code == 400, \
                f"expected 400 for {bad!r}, got {r.status_code}"
    finally:
        _delete_figure(server_url, fid)


def test_thumbnail_roundtrip_bytes_match(server_url):
    """PUT data URL -> GET raw bytes -> bytes must equal the decoded
    payload from the data URL."""
    import requests
    fid = _make_figure(server_url)
    try:
        r = requests.put(f"{server_url}/api/figures/{fid}/thumbnail",
                          json={"data_url": TINY_PNG_DATA_URL})
        assert r.status_code == 200, f"PUT failed: {r.text}"
        body = r.json()
        assert body.get("ok") is True
        assert body.get("bytes") == len(TINY_PNG_BYTES)

        # GET back -- must be image/png and the exact bytes
        g = requests.get(f"{server_url}/api/figures/{fid}/thumbnail")
        assert g.status_code == 200
        assert "image/png" in (g.headers.get("Content-Type") or "")
        assert g.content == TINY_PNG_BYTES
    finally:
        _delete_figure(server_url, fid)


def test_thumbnail_413_on_oversized_payload(server_url):
    """A 250 KB blob must be rejected with 413."""
    import requests
    fid = _make_figure(server_url)
    try:
        # 250 KB of arbitrary bytes encoded as PNG-shaped data URL.
        # The server doesn't decode/validate as a real PNG, just caps
        # bytes -- so any oversized base64 hits the cap.
        big = base64.b64encode(b"x" * (250 * 1024)).decode("ascii")
        durl = "data:image/png;base64," + big
        r = requests.put(f"{server_url}/api/figures/{fid}/thumbnail",
                          json={"data_url": durl})
        assert r.status_code == 413
    finally:
        _delete_figure(server_url, fid)


def test_thumbnail_deleted_when_figure_deleted(server_url):
    """Deleting the figure must also remove the thumbnail file so we
    don't leak PNGs."""
    import requests
    fid = _make_figure(server_url)
    requests.put(f"{server_url}/api/figures/{fid}/thumbnail",
                  json={"data_url": TINY_PNG_DATA_URL})
    # confirm it exists
    assert requests.get(
        f"{server_url}/api/figures/{fid}/thumbnail").status_code == 200
    # delete the figure
    requests.delete(f"{server_url}/api/figures/{fid}")
    # thumbnail is gone
    assert requests.get(
        f"{server_url}/api/figures/{fid}/thumbnail").status_code == 404
