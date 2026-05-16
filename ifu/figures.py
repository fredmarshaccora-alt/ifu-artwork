"""Figure persistence layer.

A "figure" is the unit a technical illustrator works with: a saved
camera + selection + per-part styles + layer toggles + annotations,
bound to a specific source revision.  Stored as one JSON file per
figure in ``out/figures/``.

Phase A intentionally keeps a flat layout (one folder, no projects).
Phase B promotes figures into per-project folders without changing
the JSON schema -- the layout migrates, the data shape doesn't.

Schema is intentionally permissive: extra fields round-trip
untouched, so we can add (annotations, revision bindings, audit log)
in Phase B+ without breaking Phase A files.
"""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import OUT

FIGURES_DIR = OUT / "figures"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir() -> None:
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)


def figure_path(fig_id: str) -> Path:
    """Path to the JSON file backing a figure id.  Caller is responsible
    for checking ``.exists()`` before reading."""
    _ensure_dir()
    # Defensive: figure ids come from URL paths, never let them escape
    safe = "".join(c for c in fig_id if c.isalnum() or c in "-_")
    return FIGURES_DIR / f"{safe}.json"


def new_figure(name: str, source_id: str,
                view_id: str = "iso",
                **extra) -> dict:
    """Construct an in-memory figure dict.  Does NOT save -- caller must
    call ``save(fig)`` to persist."""
    fig = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "source_id": source_id,
        "view_id": view_id,
        # All optional; safe defaults for a brand-new figure
        "camera": None,                # None = use the view_id's preset
        "selection": [],
        "styles_per_part": {},
        "layers_on": {
            "outline_v": True, "sharp_v": True, "smooth_v": False,
            "hidden_outline": False, "hidden_sharp": False,
        },
        "detail": "normal",
        "annotations": [],
        "notes": "",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    fig.update(extra)
    return fig


def save(fig: dict) -> Path:
    """Write figure JSON to disk, bumping ``updated_at``.  Returns the path."""
    if "id" not in fig:
        raise ValueError("figure missing 'id'")
    fig["updated_at"] = _now_iso()
    p = figure_path(fig["id"])
    p.write_text(json.dumps(fig, indent=2), encoding="utf-8")
    return p


def load(fig_id: str) -> Optional[dict]:
    """Load figure by id.  Returns None if missing or unreadable."""
    p = figure_path(fig_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete(fig_id: str) -> bool:
    """Remove the figure's JSON file.  Returns True if it existed."""
    p = figure_path(fig_id)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except Exception:
        return False


def list_all() -> list[dict]:
    """Return all figures as a list of dicts, sorted by updated_at desc.
    Skips files that fail to parse rather than blowing up the index."""
    _ensure_dir()
    out = []
    for p in FIGURES_DIR.glob("*.json"):
        try:
            fig = json.loads(p.read_text(encoding="utf-8"))
            out.append(fig)
        except Exception:
            continue
    out.sort(key=lambda f: f.get("updated_at", ""), reverse=True)
    return out
