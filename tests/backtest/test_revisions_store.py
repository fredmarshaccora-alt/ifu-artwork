"""Unit tests for the Phase C revisions layer.

These cover everything that DOESN'T need a live Onshape connection:
cache parsing, versions_behind math, find_version, latest_version,
graceful handling of missing caches.

The live ``refresh_versions`` call is tested at the integration tier
when Onshape credentials are present, and gated by the conftest's
server fixture.
"""
from __future__ import annotations
import json
import pytest

from ifu import revisions_store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(revisions_store, "REVS_DIR", tmp_path / "revs")
    yield


def _write_cache(source_id: str, versions: list[dict]):
    """Helper: drop a synthetic cached envelope on disk."""
    p = revisions_store.cache_path(source_id)
    p.write_text(json.dumps({
        "source_id": source_id,
        "last_fetched_at": "2026-05-16T10:00:00Z",
        "versions": versions,
    }, indent=2))


def test_no_cache_returns_none():
    assert revisions_store.cached_versions("never-fetched") is None
    assert revisions_store.latest_version("never-fetched") is None
    assert revisions_store.find_version("never-fetched", "v1") is None
    assert revisions_store.versions_behind("never-fetched", "v1") is None


def test_latest_version_returns_first():
    _write_cache("siderail", [
        {"id": "v3", "name": "R03"},
        {"id": "v2", "name": "R02"},
        {"id": "v1", "name": "R01"},
    ])
    assert revisions_store.latest_version("siderail")["id"] == "v3"


def test_find_version():
    _write_cache("siderail", [
        {"id": "v3", "name": "R03"},
        {"id": "v2", "name": "R02"},
        {"id": "v1", "name": "R01"},
    ])
    assert revisions_store.find_version("siderail", "v2")["name"] == "R02"
    assert revisions_store.find_version("siderail", "missing") is None


def test_versions_behind_zero_when_bound_to_latest():
    _write_cache("siderail", [
        {"id": "v3"}, {"id": "v2"}, {"id": "v1"},
    ])
    assert revisions_store.versions_behind("siderail", "v3") == 0


def test_versions_behind_counts_newer():
    _write_cache("siderail", [
        {"id": "v5"}, {"id": "v4"}, {"id": "v3"}, {"id": "v2"}, {"id": "v1"},
    ])
    assert revisions_store.versions_behind("siderail", "v3") == 2
    assert revisions_store.versions_behind("siderail", "v1") == 4
    assert revisions_store.versions_behind("siderail", "v5") == 0


def test_versions_behind_unknown_version_returns_none():
    _write_cache("siderail", [{"id": "v1"}])
    assert revisions_store.versions_behind("siderail", "nope") is None


def test_refresh_versions_raises_for_unknown_source():
    with pytest.raises(ValueError):
        revisions_store.refresh_versions("not-a-real-source")


def test_refresh_versions_raises_for_non_onshape_source():
    """Siderail in SOURCES has onshape_ids=None -- refresh must error,
    not crash or silently succeed with empty data."""
    with pytest.raises(ValueError):
        revisions_store.refresh_versions("siderail")
