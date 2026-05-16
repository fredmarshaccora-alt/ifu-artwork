"""Unit tests for the Phase A figures persistence layer.

CRUD round-trips, default fields, schema permissiveness (unknown
fields survive a save/load), file-system safety (id traversal).
"""
from __future__ import annotations
import pytest

from ifu import figures_store


@pytest.fixture(autouse=True)
def _isolate_figures(tmp_path, monkeypatch):
    """Point the module's FIGURES_DIR at a tmp directory for each test."""
    monkeypatch.setattr(figures_store, "FIGURES_DIR", tmp_path / "figs")
    yield


def test_new_figure_defaults():
    fig = figures_store.new_figure(name="Hello", source_id="siderail")
    assert fig["name"] == "Hello"
    assert fig["source_id"] == "siderail"
    assert fig["view_id"] == "iso"
    assert fig["selection"] == []
    assert fig["styles_per_part"] == {}
    assert isinstance(fig["id"], str) and len(fig["id"]) >= 8
    assert fig["layers_on"]["outline_v"] is True


def test_save_load_round_trip():
    fig = figures_store.new_figure(name="A", source_id="presto")
    figures_store.save(fig)
    loaded = figures_store.load(fig["id"])
    assert loaded is not None
    assert loaded["name"] == "A"
    assert loaded["id"] == fig["id"]


def test_unknown_fields_round_trip():
    """The schema is intentionally permissive -- extra fields the client
    might add (Phase B's revision bindings, audits, etc.) must survive."""
    fig = figures_store.new_figure(name="A", source_id="presto")
    fig["future_field"] = {"nested": [1, 2, 3]}
    figures_store.save(fig)
    loaded = figures_store.load(fig["id"])
    assert loaded["future_field"] == {"nested": [1, 2, 3]}


def test_delete():
    fig = figures_store.new_figure(name="bye", source_id="siderail")
    figures_store.save(fig)
    assert figures_store.delete(fig["id"]) is True
    assert figures_store.load(fig["id"]) is None
    assert figures_store.delete(fig["id"]) is False    # already gone


def test_list_all_sorted_newest_first():
    import time
    fig1 = figures_store.new_figure(name="older", source_id="siderail")
    figures_store.save(fig1)
    time.sleep(1)   # so updated_at differs by >= 1 second
    fig2 = figures_store.new_figure(name="newer", source_id="siderail")
    figures_store.save(fig2)
    out = figures_store.list_all()
    assert len(out) == 2
    assert out[0]["name"] == "newer"
    assert out[1]["name"] == "older"


def test_load_missing_returns_none():
    assert figures_store.load("does-not-exist") is None


def test_id_path_traversal_blocked():
    """A malicious id like '../../etc/passwd' must not escape the figures dir."""
    p = figures_store.figure_path("../../etc/passwd")
    # The sanitiser strips '.' and '/'; result lives inside FIGURES_DIR
    assert "etc" in p.name      # the safe chars survive
    assert ".." not in p.parts  # no parent traversal
    assert "/" not in p.name


def test_save_bumps_updated_at():
    import time
    fig = figures_store.new_figure(name="bump", source_id="siderail")
    figures_store.save(fig)
    first = figures_store.load(fig["id"])
    time.sleep(1.1)
    figures_store.save(fig)
    second = figures_store.load(fig["id"])
    assert second["updated_at"] > first["updated_at"]
    assert second["created_at"] == first["created_at"]
