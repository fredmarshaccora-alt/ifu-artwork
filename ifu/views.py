"""View layer (Phase 3).

A "View" is a camera angle on a project's source -- a saved 2D
rendering position.  Previously each Figure carried its own camera;
now the camera lives on the View, and many Figures (with different
highlighted parts + styles) share the same View.

  Project  --< View  --< Figure

  View {
    id, project_id, source_id, name,
    camera: {eye, target, up_axis},
    configuration: {...},
    figure_ids: [...],
    thumbnail_path, created_at, updated_at,
  }

The Figure schema doesn't change yet -- existing camera + source_id
remain on the figure for backward compat -- but the editor will start
reading them from the View when a figure_id is loaded via the new
/project/<pid>/view/<vid>/figure/<fid> route.

Migration (see ``migrate_existing_figures``): every Figure that has
a project_id but no view_id pointing at a real View spawns a 1:1
View whose camera comes from the Figure.  Run on import / boot so
existing data flows into the new model without user intervention.
"""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import OUT
from . import figures as figures_store
from . import projects as projects_store

VIEWS_DIR = OUT / "views"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir() -> None:
    VIEWS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_id(view_id: str) -> str:
    return "".join(c for c in view_id if c.isalnum() or c in "-_")


def view_path(view_id: str) -> Path:
    _ensure_dir()
    return VIEWS_DIR / f"{_safe_id(view_id)}.json"


def view_thumbnail_path(view_id: str) -> Path:
    _ensure_dir()
    return VIEWS_DIR / f"{_safe_id(view_id)}.png"


def new_view(*, project_id: str, source_id: str, name: str = "",
              camera: Optional[dict] = None,
              configuration: Optional[dict] = None) -> dict:
    vid = uuid.uuid4().hex[:12]
    v = {
        "id": vid,
        "project_id": project_id,
        "source_id": source_id,
        "name": name or "Untitled view",
        "camera": camera,
        "configuration": configuration or None,
        "figure_ids": [],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    return v


def save(v: dict) -> Path:
    if "id" not in v:
        raise ValueError("view missing 'id'")
    v["updated_at"] = _now_iso()
    p = view_path(v["id"])
    p.write_text(json.dumps(v, indent=2), encoding="utf-8")
    return p


def load(view_id: str) -> Optional[dict]:
    p = view_path(view_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete(view_id: str, cascade: bool = False) -> bool:
    """Delete the view.  cascade=True also deletes every figure under
    it; cascade=False leaves figures as orphans (their view_id stays
    set but points nowhere)."""
    v = load(view_id)
    if v is None:
        return False
    if cascade:
        for fid in (v.get("figure_ids") or []):
            figures_store.delete(fid)
    try:
        view_path(view_id).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        tp = view_thumbnail_path(view_id)
        if tp.exists():
            tp.unlink()
    except Exception:
        pass
    return True


def list_all() -> list[dict]:
    _ensure_dir()
    out = []
    for p in VIEWS_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda v: v.get("updated_at", ""), reverse=True)
    return out


def views_in_project(project_id: str) -> list[dict]:
    return [v for v in list_all() if v.get("project_id") == project_id]


def attach_figure(view_id: str, fig_id: str) -> bool:
    """Append fig_id to view.figure_ids (idempotent) AND set the
    figure's view_id backlink.  Returns True if both lookups succeed."""
    v = load(view_id)
    if v is None:
        return False
    fig = figures_store.load(fig_id)
    if fig is None:
        return False
    ids = v.setdefault("figure_ids", [])
    if fig_id not in ids:
        ids.append(fig_id)
        save(v)
    if fig.get("view_id") != view_id:
        fig["view_id"] = view_id
        figures_store.save(fig)
    return True


def detach_figure(view_id: str, fig_id: str) -> bool:
    v = load(view_id)
    if v is None:
        return False
    ids = v.get("figure_ids", [])
    if fig_id not in ids:
        return False
    ids.remove(fig_id)
    save(v)
    fig = figures_store.load(fig_id)
    if fig and fig.get("view_id") == view_id:
        fig.pop("view_id", None)
        figures_store.save(fig)
    return True


def figures_in_view(view_id: str) -> list[dict]:
    """Resolved figure dicts for a view (drops dangling ids)."""
    v = load(view_id)
    if v is None:
        return []
    out = []
    for fid in (v.get("figure_ids") or []):
        fig = figures_store.load(fid)
        if fig is not None:
            out.append(fig)
    return out


# ---- migration -------------------------------------------------------

def migrate_existing_figures() -> dict:
    """Walk every Figure with a project_id; spawn a View per figure
    that doesn't already have one.  Idempotent: figures already
    pointing at a real View are left alone.

    Returns counts: {checked, created, skipped, orphan}.
    """
    counts = {"checked": 0, "created": 0, "skipped": 0, "orphan": 0}
    existing_view_ids = {v["id"] for v in list_all()}
    for fig in figures_store.list_all():
        counts["checked"] += 1
        pid = fig.get("project_id")
        if not pid:
            counts["orphan"] += 1
            continue
        if not projects_store.load(pid):
            counts["orphan"] += 1
            continue
        # If figure already has a valid view_id, skip
        vid = fig.get("view_id")
        if vid and vid in existing_view_ids:
            counts["skipped"] += 1
            continue
        # Otherwise spawn a 1:1 view
        view = new_view(
            project_id=pid,
            source_id=fig.get("source_id") or "",
            name=fig.get("name") or "View",
            camera=fig.get("camera"))
        save(view)
        attach_figure(view["id"], fig["id"])
        existing_view_ids.add(view["id"])
        counts["created"] += 1
    return counts
