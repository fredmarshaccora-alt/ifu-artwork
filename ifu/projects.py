"""Project layer (Phase B).

A "project" is the top-level container an illustrator works within:
e.g. "Presto IFU R03".  It groups figures together and, in Phase C+,
will own the per-source revision history.

Phase B keeps the data model deliberately thin:

  Project = {id, name, description, created_at, updated_at,
              figure_ids: [...]}

Figures keep their existing schema; we just add an optional
``project_id`` field.  A figure with no project_id is "orphan"
(legacy / pre-Phase-B) and shows up in a special "Unfiled" bucket
in the UI.

Stored at ``out/projects/{project_id}.json`` -- flat, alongside
the figures folder.  Folder-per-project layout from the DESIGN
doc lands in Phase C when sources also need per-project caching.
"""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import OUT
from . import figures as figures_store

PROJECTS_DIR = OUT / "projects"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def project_path(proj_id: str) -> Path:
    _ensure_dir()
    safe = "".join(c for c in proj_id if c.isalnum() or c in "-_")
    return PROJECTS_DIR / f"{safe}.json"


def new_project(name: str, description: str = "",
                 primary_source_id: Optional[str] = None,
                 onshape_ids: Optional[dict] = None) -> dict:
    """Build a fresh project dict.  ``primary_source_id`` lets the
    project remember which source it was created against (typically
    the result of a G.2 Onshape import) so the figure-creation modal
    can pre-select it.  ``onshape_ids`` records the document the
    project was imported from for provenance."""
    proj = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "description": description,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "figure_ids": [],
    }
    if primary_source_id:
        proj["primary_source_id"] = primary_source_id
    if onshape_ids:
        proj["onshape_ids"] = onshape_ids
    return proj


def save(proj: dict) -> Path:
    if "id" not in proj:
        raise ValueError("project missing 'id'")
    proj["updated_at"] = _now_iso()
    p = project_path(proj["id"])
    p.write_text(json.dumps(proj, indent=2), encoding="utf-8")
    return p


def load(proj_id: str) -> Optional[dict]:
    p = project_path(proj_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete(proj_id: str, cascade: bool = False) -> bool:
    """Delete the project.  If ``cascade``, also delete every figure
    referenced by ``figure_ids`` -- otherwise the figures become
    orphans (project_id no longer points anywhere)."""
    proj = load(proj_id)
    if proj is None:
        return False
    if cascade:
        for fid in (proj.get("figure_ids") or []):
            figures_store.delete(fid)
    project_path(proj_id).unlink(missing_ok=True)
    return True


def list_all() -> list[dict]:
    _ensure_dir()
    out = []
    for p in PROJECTS_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    return out


def add_figure(proj_id: str, fig_id: str) -> bool:
    """Append fig_id to project's figure_ids (idempotent).  Returns
    True on success, False if either doesn't exist."""
    proj = load(proj_id)
    if proj is None:
        return False
    fig = figures_store.load(fig_id)
    if fig is None:
        return False
    ids = proj.setdefault("figure_ids", [])
    if fig_id not in ids:
        ids.append(fig_id)
        save(proj)
    # Backlink: figure remembers its project
    if fig.get("project_id") != proj_id:
        fig["project_id"] = proj_id
        figures_store.save(fig)
    return True


def remove_figure(proj_id: str, fig_id: str) -> bool:
    """Remove fig_id from project.figure_ids and clear the figure's
    project_id.  Returns True if it was in the list."""
    proj = load(proj_id)
    if proj is None:
        return False
    ids = proj.get("figure_ids", [])
    if fig_id not in ids:
        return False
    ids.remove(fig_id)
    save(proj)
    fig = figures_store.load(fig_id)
    if fig and fig.get("project_id") == proj_id:
        fig.pop("project_id", None)
        figures_store.save(fig)
    return True


def figures_in(proj_id: str) -> list[dict]:
    """Resolved list of figure dicts in this project (drops any
    dangling ids whose files have been deleted)."""
    proj = load(proj_id)
    if proj is None:
        return []
    out = []
    for fid in (proj.get("figure_ids") or []):
        f = figures_store.load(fid)
        if f is not None:
            out.append(f)
    return out


def orphan_figures() -> list[dict]:
    """Every figure that has no project_id or whose project_id points
    to a deleted project.  Shown in the "Unfiled" bucket."""
    all_projs = {p["id"] for p in list_all()}
    out = []
    for fig in figures_store.list_all():
        pid = fig.get("project_id")
        if not pid or pid not in all_projs:
            out.append(fig)
    return out
