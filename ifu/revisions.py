"""Source revision tracking (Phase C).

For each source with Onshape ids, we can query the document's Versions
list -- explicit publish markers (e.g. "R01", "R02") created by
engineering -- and track which Version a figure is bound to.

A figure's ``bound_revision`` field captures the Version id + name +
microversion at the moment of binding.  Comparing that to the latest
Version tells us "your figure is N revisions behind the latest".

The Versions list is cached in ``out/revisions/{source_id}.json`` so
we don't hit Onshape on every page load.  Refresh is manual via the
``refresh_versions`` endpoint.

Phase C SCOPE: track metadata only.  Re-rendering against a new
revision (i.e. pulling the STEP at that Version, baking SVG, rerunning
HLR with the figure's camera + selection) is Phase D work.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional

from .config import OUT, SOURCES
from .onshape_client import OnshapeClient

REVS_DIR = OUT / "revisions"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir() -> None:
    REVS_DIR.mkdir(parents=True, exist_ok=True)


def _source_meta(source_id: str) -> Optional[dict]:
    """Look up the SOURCES entry for ``source_id`` as a dict."""
    for entry in SOURCES:
        if entry[0] == source_id:
            return {
                "id": entry[0],
                "label": entry[1],
                "step_path": str(entry[2]),
                "onshape_ids": entry[5] if len(entry) > 5 else None,
            }
    return None


def cache_path(source_id: str) -> Path:
    _ensure_dir()
    safe = "".join(c for c in source_id if c.isalnum() or c in "-_")
    return REVS_DIR / f"{safe}.json"


def cached_versions(source_id: str) -> Optional[dict]:
    """Load the cached versions list for a source.  Returns None if not
    yet cached.  Shape: ``{source_id, last_fetched_at, versions: [{...}]}``."""
    p = cache_path(source_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def refresh_versions(source_id: str) -> dict:
    """Hit Onshape's /documents/{did}/versions endpoint and cache the result.

    Returns the cached envelope ``{source_id, last_fetched_at, versions}``.
    Versions are sorted newest-first.

    Raises:
      ValueError if source_id is unknown or has no onshape_ids
      RuntimeError if the Onshape client is unavailable
      Whatever the client raises on network/auth errors
    """
    src = _source_meta(source_id)
    if src is None:
        raise ValueError(f"unknown source: {source_id!r}")
    ids = src.get("onshape_ids")
    if ids is None:
        raise ValueError(
            f"source {source_id!r} has no onshape_ids (local STEP only)")
    if OnshapeClient is None:
        raise RuntimeError(
            "Onshape client unavailable; cannot refresh versions")

    c = OnshapeClient()
    did = ids["did"]
    resp = c.get(f"/documents/d/{did}/versions")
    # Onshape returns an ARRAY of version objects.  Normalise the keys
    # we care about; round-trip the raw response too so we can extract
    # more later if needed.
    versions = []
    for v in resp or []:
        versions.append({
            "id": v.get("id"),
            "name": v.get("name"),
            "description": v.get("description") or "",
            "created_at": v.get("createdAt"),
            "microversion": v.get("microversion"),
            "parent": v.get("parent"),
        })
    # Onshape returns newest first already, but sort defensively
    versions.sort(key=lambda v: v.get("created_at") or "", reverse=True)

    envelope = {
        "source_id": source_id,
        "last_fetched_at": _now_iso(),
        "versions": versions,
    }
    cache_path(source_id).write_text(
        json.dumps(envelope, indent=2), encoding="utf-8")
    return envelope


def latest_version(source_id: str) -> Optional[dict]:
    """Return the latest cached Version dict for the source, or None
    if there's nothing cached."""
    cached = cached_versions(source_id)
    if not cached or not cached.get("versions"):
        return None
    return cached["versions"][0]


def find_version(source_id: str, version_id: str) -> Optional[dict]:
    """Locate a specific Version in the cached list."""
    cached = cached_versions(source_id)
    if not cached:
        return None
    for v in cached.get("versions") or []:
        if v.get("id") == version_id:
            return v
    return None


def versions_behind(source_id: str, bound_version_id: str) -> Optional[int]:
    """How many Versions are NEWER than ``bound_version_id``?

    Returns None when we can't compute (no cache, bound id not found).
    0 means the figure is up to date.
    """
    cached = cached_versions(source_id)
    if not cached:
        return None
    vs = cached.get("versions") or []
    # vs is newest-first; count entries before the bound one
    for i, v in enumerate(vs):
        if v.get("id") == bound_version_id:
            return i
    return None
