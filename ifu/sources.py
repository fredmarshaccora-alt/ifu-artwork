"""Dynamic source registry (Phase G.2).

The static ``ifu.config.SOURCES`` tuple is the *bootstrap* set -- three
hand-curated assemblies wired into the build script.  Once the user can
import their own Onshape documents we need a way to add sources at
runtime: their STEP lands in ``out/imports/``, but the source's
identity, label, and Onshape ids need to live somewhere persistent so a
restart picks them back up.

We keep static + dynamic in separate code paths on purpose:
  * static SOURCES drive the *baked* HLR catalogue (build_viewer + the
    ``out/<file>__<view>.svg`` files).  They're heavy, hand-curated, and
    rarely change.
  * dynamic sources are STEP-only and rendered live via /api/render --
    no baked SVGs.  They appear in /api/sources so the UI can list them
    and figures can bind to them.

Persistence: ``out/sources/dynamic.json``, one JSON list of dicts.
Schema matches the tuple-shape of SOURCES translated to keys, plus
``created_at`` / ``imported_from`` for provenance.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional

from .config import OUT, SOURCES as _STATIC_SOURCES

SOURCES_DIR = OUT / "sources"
DYNAMIC_PATH = SOURCES_DIR / "dynamic.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir() -> None:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)


def _load() -> list[dict]:
    if not DYNAMIC_PATH.exists():
        return []
    try:
        data = json.loads(DYNAMIC_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return data["sources"]
    except Exception:
        pass
    return []


def _save(items: list[dict]) -> None:
    _ensure_dir()
    DYNAMIC_PATH.write_text(json.dumps({"sources": items}, indent=2),
                             encoding="utf-8")


def _static_as_dicts() -> list[dict]:
    """Project the static SOURCES tuple list into the dict shape we use
    everywhere else.  Stable order matches config.py order."""
    out = []
    for entry in _STATIC_SOURCES:
        out.append({
            "id": entry[0],
            "label": entry[1],
            "step_path": str(entry[2]),
            "hlr_kwargs": entry[3] if len(entry) > 3 else None,
            "pre_rotation": entry[4] if len(entry) > 4 else None,
            "onshape_ids": entry[5] if len(entry) > 5 else None,
            "origin": "static",
        })
    return out


def list_dynamic() -> list[dict]:
    """Just the dynamic sources, newest first."""
    items = _load()
    items.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    for s in items:
        s.setdefault("origin", "dynamic")
    return items


def all_sources() -> list[dict]:
    """Static (in declaration order) followed by dynamic (newest first)."""
    return _static_as_dicts() + list_dynamic()


def find(source_id: str) -> Optional[dict]:
    """Look up a source by id across static + dynamic.  Returns None
    if no such source exists."""
    for s in all_sources():
        if s["id"] == source_id:
            return s
    return None


def register(*, source_id: str, label: str, step_path: str,
              onshape_ids: Optional[dict] = None,
              hlr_kwargs: Optional[dict] = None,
              imported_from: Optional[str] = None) -> dict:
    """Add (or upsert) a dynamic source.  Idempotent: re-registering
    the same id overwrites the row in place rather than duplicating.
    Returns the stored entry."""
    if not source_id:
        raise ValueError("source_id required")
    # Reasonable defaults for an imported assembly
    hlr_kwargs = hlr_kwargs or {"mesh_defl": 1.5, "sample_defl": 1.0}
    items = _load()
    now = _now_iso()
    entry = {
        "id": source_id,
        "label": label or source_id,
        "step_path": str(step_path),
        "hlr_kwargs": hlr_kwargs,
        "pre_rotation": None,
        "onshape_ids": onshape_ids,
        "origin": "dynamic",
        "created_at": now,
        "updated_at": now,
        "imported_from": imported_from,
    }
    # Upsert by id
    for i, s in enumerate(items):
        if s.get("id") == source_id:
            entry["created_at"] = s.get("created_at", now)
            items[i] = entry
            _save(items)
            return entry
    items.append(entry)
    _save(items)
    return entry


def unregister(source_id: str) -> bool:
    """Remove a dynamic source from the registry.  Returns True if a
    row was deleted.  Does NOT delete the STEP file -- the caller is
    responsible for cleaning up disk if desired."""
    items = _load()
    before = len(items)
    items = [s for s in items if s.get("id") != source_id]
    if len(items) == before:
        return False
    _save(items)
    return True
