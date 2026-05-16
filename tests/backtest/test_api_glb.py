"""G.6 API surface tests for /api/glb/<source_id>.

The endpoint meshes a source's STEP shape on demand so dynamic
(Onshape-imported) sources can show up in the 3D viewer without a
page rebuild.  Heavy enough that we don't load every source here --
we exercise the contract:

  * static sources return a b64 GLB + summary
  * unknown sources return 404
"""
from __future__ import annotations


def test_glb_for_static_source(server_url):
    """Asking for a baked source returns a base64 GLB with a non-zero
    part + tri count."""
    import requests
    r = requests.get(f"{server_url}/api/glb/siderail", timeout=120)
    assert r.status_code == 200
    body = r.json()
    assert body.get("source_id") == "siderail"
    assert isinstance(body.get("b64"), str) and len(body["b64"]) > 1000
    assert body.get("parts", 0) > 0
    assert body.get("tris", 0) > 0
    assert body.get("kb", 0) > 0


def test_glb_404_for_unknown(server_url):
    import requests
    r = requests.get(f"{server_url}/api/glb/nope_does_not_exist",
                      timeout=10)
    assert r.status_code == 404
    body = r.json()
    assert "unknown" in body.get("error", "").lower()
    # 404 body advertises which sources ARE loaded -- useful for the UI
    assert isinstance(body.get("known"), list)
