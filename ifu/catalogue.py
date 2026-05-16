"""Catalogue persistence: dump/load the metadata JSON that ``rebuild_html``
needs to re-bundle the viewer without re-running HLR."""
from __future__ import annotations
import json
from pathlib import Path

from .config import OUT


def save_catalogue(catalogue) -> Path:
    """Persist catalogue to disk so we can rebuild HTML without re-running HLR."""
    p = OUT / "_catalogue.json"
    p.write_text(json.dumps(catalogue, indent=2), encoding="utf-8")
    return p


def load_catalogue():
    """Load the cached catalogue.  Returns ``None`` when missing/unreadable."""
    p = OUT / "_catalogue.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
