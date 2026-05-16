"""Unit tests for the Phase B projects layer."""
from __future__ import annotations
import pytest

from ifu import projects_store, figures_store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(figures_store, "FIGURES_DIR", tmp_path / "figs")
    monkeypatch.setattr(projects_store, "PROJECTS_DIR", tmp_path / "projs")
    yield


def test_new_project_defaults():
    p = projects_store.new_project(name="Test project", description="d")
    assert p["name"] == "Test project"
    assert p["description"] == "d"
    assert p["figure_ids"] == []
    assert "id" in p


def test_save_load_round_trip():
    p = projects_store.new_project(name="A")
    projects_store.save(p)
    loaded = projects_store.load(p["id"])
    assert loaded["name"] == "A"


def test_add_figure_backlinks_project_id():
    p = projects_store.new_project(name="P")
    projects_store.save(p)
    f = figures_store.new_figure(name="F", source_id="siderail")
    figures_store.save(f)

    assert projects_store.add_figure(p["id"], f["id"]) is True
    proj2 = projects_store.load(p["id"])
    fig2 = figures_store.load(f["id"])
    assert f["id"] in proj2["figure_ids"]
    assert fig2["project_id"] == p["id"]


def test_add_figure_idempotent():
    p = projects_store.new_project(name="P")
    projects_store.save(p)
    f = figures_store.new_figure(name="F", source_id="siderail")
    figures_store.save(f)
    projects_store.add_figure(p["id"], f["id"])
    projects_store.add_figure(p["id"], f["id"])  # again
    proj2 = projects_store.load(p["id"])
    assert proj2["figure_ids"].count(f["id"]) == 1


def test_remove_figure_clears_backlink():
    p = projects_store.new_project(name="P")
    projects_store.save(p)
    f = figures_store.new_figure(name="F", source_id="siderail")
    figures_store.save(f)
    projects_store.add_figure(p["id"], f["id"])
    assert projects_store.remove_figure(p["id"], f["id"]) is True
    fig2 = figures_store.load(f["id"])
    assert "project_id" not in fig2


def test_figures_in_drops_dangling_ids():
    p = projects_store.new_project(name="P")
    projects_store.save(p)
    f = figures_store.new_figure(name="F", source_id="siderail")
    figures_store.save(f)
    projects_store.add_figure(p["id"], f["id"])
    # Delete the figure FILE directly (not via remove_figure)
    figures_store.delete(f["id"])
    # The project still has the dangling id, but figures_in() filters it
    out = projects_store.figures_in(p["id"])
    assert out == []


def test_delete_cascade():
    p = projects_store.new_project(name="P")
    projects_store.save(p)
    f1 = figures_store.new_figure(name="F1", source_id="siderail")
    figures_store.save(f1)
    f2 = figures_store.new_figure(name="F2", source_id="siderail")
    figures_store.save(f2)
    projects_store.add_figure(p["id"], f1["id"])
    projects_store.add_figure(p["id"], f2["id"])
    projects_store.delete(p["id"], cascade=True)
    assert figures_store.load(f1["id"]) is None
    assert figures_store.load(f2["id"]) is None


def test_delete_no_cascade_leaves_orphans():
    p = projects_store.new_project(name="P")
    projects_store.save(p)
    f = figures_store.new_figure(name="F", source_id="siderail")
    figures_store.save(f)
    projects_store.add_figure(p["id"], f["id"])
    projects_store.delete(p["id"], cascade=False)
    # Figure still exists, but its project_id now points to a missing project
    fig2 = figures_store.load(f["id"])
    assert fig2 is not None
    assert fig2.get("project_id") == p["id"]
    # orphan_figures() catches this
    orphans = projects_store.orphan_figures()
    assert any(o["id"] == f["id"] for o in orphans)


def test_orphan_figures_with_no_project_id():
    """A figure that was never attached to a project also counts as orphan."""
    f = figures_store.new_figure(name="Orphan", source_id="siderail")
    figures_store.save(f)
    orphans = projects_store.orphan_figures()
    assert any(o["id"] == f["id"] for o in orphans)
