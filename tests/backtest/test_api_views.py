"""Phase-3 Views API.

Pin the contract before the UI starts depending on it:
  - POST /api/views requires project_id, defaults source_id from
    project's primary_source_id
  - GET /api/projects/<pid>/views returns the project's views with
    figure_count populated
  - POST /api/views/<vid>/figures/<fid> attaches both directions
    (view.figure_ids += fid AND figure.view_id = vid)
  - DELETE /api/views/<vid>?cascade=1 removes attached figures
  - Migration is idempotent
"""
from __future__ import annotations
import time
import requests


def _new_project(server_url, name, **extra):
    body = {"name": name, "primary_source_id": "siderail", **extra}
    r = requests.post(f"{server_url}/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _new_figure(server_url, project_id, name):
    r = requests.post(f"{server_url}/api/figures", json={
        "name": name, "source_id": "siderail", "project_id": project_id})
    assert r.status_code == 201, r.text
    return r.json()


def _cleanup(server_url, pid):
    requests.delete(f"{server_url}/api/projects/{pid}?cascade=1")


def test_create_view_defaults_source_from_project(server_url):
    proj = _new_project(server_url, "v-test-1")
    try:
        r = requests.post(f"{server_url}/api/views",
                          json={"project_id": proj["id"],
                                "name": "Iso top",
                                "camera": {"eye": [1, 1, 1], "target": [0, 0, 0]}})
        assert r.status_code == 201, r.text
        v = r.json()
        assert v["project_id"] == proj["id"]
        assert v["source_id"] == "siderail"
        assert v["name"] == "Iso top"
        assert v["camera"]["eye"] == [1, 1, 1]
        assert v["figure_ids"] == []
    finally:
        _cleanup(server_url, proj["id"])


def test_views_in_project_returns_figure_count(server_url):
    proj = _new_project(server_url, "v-count")
    pid = proj["id"]
    try:
        v1 = requests.post(f"{server_url}/api/views",
                            json={"project_id": pid, "name": "V1"}).json()
        v2 = requests.post(f"{server_url}/api/views",
                            json={"project_id": pid, "name": "V2"}).json()
        # Attach one figure to v1
        f = _new_figure(server_url, pid, "f1")
        requests.post(f"{server_url}/api/views/{v1['id']}/figures/{f['id']}")

        r = requests.get(f"{server_url}/api/projects/{pid}/views")
        assert r.status_code == 200
        views = r.json()["views"]
        assert len(views) == 2
        by_id = {v["id"]: v for v in views}
        assert by_id[v1["id"]]["figure_count"] == 1
        assert by_id[v2["id"]]["figure_count"] == 0
    finally:
        _cleanup(server_url, pid)


def test_attach_figure_sets_both_directions(server_url):
    proj = _new_project(server_url, "v-attach")
    pid = proj["id"]
    try:
        v = requests.post(f"{server_url}/api/views",
                          json={"project_id": pid, "name": "V"}).json()
        fig = _new_figure(server_url, pid, "F")

        r = requests.post(
            f"{server_url}/api/views/{v['id']}/figures/{fig['id']}")
        assert r.status_code == 200
        # View now lists the figure
        updated = requests.get(f"{server_url}/api/views/{v['id']}").json()
        assert fig["id"] in (updated.get("figure_ids") or [])
        # Figure now points back at the view
        f2 = requests.get(f"{server_url}/api/figures/{fig['id']}").json()
        assert f2.get("view_id") == v["id"]
    finally:
        _cleanup(server_url, pid)


def test_cascade_delete_removes_figures(server_url):
    proj = _new_project(server_url, "v-cascade")
    pid = proj["id"]
    try:
        v = requests.post(f"{server_url}/api/views",
                          json={"project_id": pid, "name": "V"}).json()
        fig = _new_figure(server_url, pid, "F-doomed")
        requests.post(f"{server_url}/api/views/{v['id']}/figures/{fig['id']}")
        # Cascade delete the view
        d = requests.delete(
            f"{server_url}/api/views/{v['id']}?cascade=1")
        assert d.status_code == 204
        # Figure should be gone
        g = requests.get(f"{server_url}/api/figures/{fig['id']}")
        assert g.status_code == 404
    finally:
        _cleanup(server_url, pid)


def test_migrate_is_idempotent(server_url):
    """Running the migration twice creates no extra views."""
    proj = _new_project(server_url, "v-mig")
    pid = proj["id"]
    try:
        _new_figure(server_url, pid, "F-orphan")
        a = requests.post(f"{server_url}/api/views/migrate").json()
        b = requests.post(f"{server_url}/api/views/migrate").json()
        assert a.get("created") >= 1, a
        assert b.get("created") == 0, b
        # After both runs the figure should have a view attached
        views = requests.get(
            f"{server_url}/api/projects/{pid}/views").json()["views"]
        assert any(v.get("figure_count", 0) >= 1 for v in views), views
    finally:
        _cleanup(server_url, pid)


def test_view_post_400_without_project(server_url):
    r = requests.post(f"{server_url}/api/views",
                      json={"name": "stray"})
    assert r.status_code == 400


def test_view_get_404_for_unknown(server_url):
    r = requests.get(f"{server_url}/api/views/does_not_exist")
    assert r.status_code == 404
