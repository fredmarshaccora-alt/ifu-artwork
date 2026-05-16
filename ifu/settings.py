"""App-level settings (F.1).

A single JSON document at ``out/settings.json`` holding everything
that's NOT per-project or per-figure: default render detail, default
styling, storage path, etc.  Loaded once at startup, persisted on
every write.

Schema is intentionally permissive (unknown keys round-trip) so new
features can add defaults without a migration step.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional

from .config import OUT

SETTINGS_PATH = OUT / "settings.json"


# Centralised default-document.  When the on-disk file is missing or
# corrupt, we materialise this; on write we MERGE the caller's payload
# onto this so a partial update doesn't blow away other keys.
DEFAULT_SETTINGS = {
    "version": 1,
    # Render defaults applied when a new figure is created
    "default_detail": "normal",            # coarse | normal | fine
    "default_stroke_color": "#00836a",
    "default_stroke_width_mm": 3.0,
    "default_fill_color": "#cce6e0",
    "default_fill_alpha": 0.3,
    "default_fill_on": False,
    # UI prefs
    "ui_dark_mode": False,
    "ui_grid_visible": True,
    # Storage
    "projects_dir": str(OUT),              # disk location of project JSON
    # Onshape (read-only mirror -- credentials live in env / dotenv)
    "onshape_connected": False,
    "onshape_account": None,
    # Last-launch metadata
    "last_loaded_at": None,
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    """Return the merged settings dict.  Always returns a usable doc --
    falls back to DEFAULT_SETTINGS if the file is missing / unreadable.
    Unknown keys on disk are preserved through the merge."""
    _ensure_dir()
    if not SETTINGS_PATH.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        on_disk = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_SETTINGS)
    # Merge: defaults provide missing keys, on-disk wins.
    out = dict(DEFAULT_SETTINGS)
    out.update(on_disk or {})
    return out


def save(patch: dict) -> dict:
    """Merge ``patch`` onto the existing settings and persist.

    Returns the post-merge dict.  ``version`` is preserved; ``patch``
    can't downgrade it.
    """
    current = load()
    cur_version = current.get("version", 1)
    current.update(patch or {})
    current["version"] = max(cur_version, current.get("version", 1))
    current["last_loaded_at"] = _now_iso()
    _ensure_dir()
    SETTINGS_PATH.write_text(json.dumps(current, indent=2),
                              encoding="utf-8")
    return current


def reset() -> dict:
    """Wipe to DEFAULT_SETTINGS.  Used by the settings screen's
    "reset to defaults" button.  Returns the fresh dict."""
    _ensure_dir()
    fresh = dict(DEFAULT_SETTINGS)
    fresh["last_loaded_at"] = _now_iso()
    SETTINGS_PATH.write_text(json.dumps(fresh, indent=2),
                              encoding="utf-8")
    return fresh


def get(key: str, default=None):
    """Convenience accessor for a single key."""
    return load().get(key, default)
