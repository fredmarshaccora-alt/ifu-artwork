"""Line-style presets ("shading") for the 2D vector output.

The HLR line-art draws five edge categories, each with a stroke colour, a
width (mm at 1:1) and an optional SVG dash pattern (see ``t5_hlr_vector
.DEFAULT_STYLES``).  A *preset* is a named bundle of those per-category styles
so a user can flip a whole figure between, e.g., a crisp technical look and a
soft pencil look -- and define their own.

Storage mirrors ``ifu.settings``: a single JSON document at
``out/presets.json`` holding the user-defined presets.  A set of built-in
presets is merged in at read time and is read-only (``builtin: true``); user
presets win on id collision so a user can shadow a builtin if they really want.

A preset's ``styles`` only needs to override the categories it cares about;
``resolve_styles`` fills the rest from ``DEFAULT_STYLES`` so the dict handed to
``write_svg_parts`` is always complete.
"""
from __future__ import annotations
import json
import time
from typing import Optional

from .config import OUT

PRESETS_PATH = OUT / "presets.json"

# The five edge categories a preset may style (back-to-front draw order).
CATEGORIES = ("hidden_outline", "hidden_sharp", "smooth_v",
              "sharp_v", "outline_v")


# --- built-in presets --------------------------------------------------------
# Each: id, name, builtin, styles{category: {stroke, width, dash|None}}.
# These are deliberately nicer than the single hard-coded default: a clear
# weight hierarchy (heavy silhouette, medium creases, light tangents) reads far
# better in printed IFUs than uniform lines.
BUILTIN_PRESETS = [
    {
        "id": "crisp_technical",
        "name": "Crisp Technical",
        "builtin": True,
        "styles": {
            "outline_v":      {"stroke": "#111111", "width": 0.80, "dash": None},
            "sharp_v":        {"stroke": "#111111", "width": 0.34, "dash": None},
            "smooth_v":       {"stroke": "#8a8a8a", "width": 0.18, "dash": None},
            "hidden_sharp":   {"stroke": "#9aa0a6", "width": 0.20, "dash": "2 1.5"},
            "hidden_outline": {"stroke": "#9aa0a6", "width": 0.28, "dash": "3 2"},
        },
    },
    {
        "id": "bold_outline",
        "name": "Bold Outline",
        "builtin": True,
        "styles": {
            "outline_v":      {"stroke": "#000000", "width": 1.20, "dash": None},
            "sharp_v":        {"stroke": "#000000", "width": 0.40, "dash": None},
            "smooth_v":       {"stroke": "#9a9a9a", "width": 0.18, "dash": None},
            "hidden_sharp":   {"stroke": "#b0b0b0", "width": 0.22, "dash": "2 2"},
            "hidden_outline": {"stroke": "#b0b0b0", "width": 0.30, "dash": "3 2"},
        },
    },
    {
        "id": "soft_pencil",
        "name": "Soft Pencil",
        "builtin": True,
        "styles": {
            "outline_v":      {"stroke": "#3a3a3a", "width": 0.55, "dash": None},
            "sharp_v":        {"stroke": "#5a5a5a", "width": 0.28, "dash": None},
            "smooth_v":       {"stroke": "#9b9b9b", "width": 0.20, "dash": None},
            "hidden_sharp":   {"stroke": "#bcbcbc", "width": 0.18, "dash": "1.5 1.5"},
            "hidden_outline": {"stroke": "#bcbcbc", "width": 0.24, "dash": "2 2"},
        },
    },
    {
        "id": "blueprint",
        "name": "Blueprint",
        "builtin": True,
        "styles": {
            "outline_v":      {"stroke": "#0b3d91", "width": 0.80, "dash": None},
            "sharp_v":        {"stroke": "#0b3d91", "width": 0.34, "dash": None},
            "smooth_v":       {"stroke": "#6f8fc4", "width": 0.18, "dash": None},
            "hidden_sharp":   {"stroke": "#6f8fc4", "width": 0.20, "dash": "2 1.5"},
            "hidden_outline": {"stroke": "#6f8fc4", "width": 0.28, "dash": "3 2"},
        },
    },
]

# Default preset applied when a figure has none selected.
DEFAULT_PRESET_ID = "crisp_technical"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir() -> None:
    PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_user() -> list:
    if not PRESETS_PATH.exists():
        return []
    try:
        doc = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(doc.get("presets") or []) if isinstance(doc, dict) else []


def _save_user(user_presets: list) -> None:
    _ensure_dir()
    PRESETS_PATH.write_text(
        json.dumps({"version": 1, "presets": user_presets,
                    "saved_at": _now_iso()}, indent=2),
        encoding="utf-8")


def list_all() -> list:
    """Return built-ins followed by user presets.  On id collision the user
    preset replaces the builtin (so editing a builtin = saving a shadow)."""
    user = _load_user()
    user_ids = {p.get("id") for p in user}
    merged = [p for p in BUILTIN_PRESETS if p["id"] not in user_ids]
    merged.extend(user)
    return merged


def get(preset_id: str) -> Optional[dict]:
    if not preset_id:
        return None
    for p in list_all():
        if p.get("id") == preset_id:
            return p
    return None


def resolve_styles(preset_or_id) -> Optional[dict]:
    """Return a complete {category: {stroke,width,dash}} dict for a preset
    (or preset id), or None if not found / not given.  Only the categories the
    preset specifies are returned; ``write_svg_parts`` merges these onto its
    own DEFAULT_STYLES so partial presets are fine."""
    preset = preset_or_id if isinstance(preset_or_id, dict) else get(preset_or_id)
    if not preset:
        return None
    styles = preset.get("styles") or {}
    # Keep only known categories + valid keys.
    out = {}
    for cat, st in styles.items():
        if cat not in CATEGORIES or not isinstance(st, dict):
            continue
        out[cat] = {
            "stroke": st.get("stroke", "#000000"),
            "width": float(st.get("width", 0.3)),
            "dash": st.get("dash") or None,
        }
    return out or None


def create(payload: dict) -> dict:
    """Create a user preset.  Generates an id from the name if absent."""
    user = _load_user()
    name = (payload.get("name") or "Untitled").strip()
    pid = (payload.get("id") or "").strip()
    if not pid:
        base = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
        pid = base or "preset"
        existing = {p.get("id") for p in list_all()}
        if pid in existing:
            n = 2
            while f"{pid}_{n}" in existing:
                n += 1
            pid = f"{pid}_{n}"
    preset = {
        "id": pid,
        "name": name,
        "builtin": False,
        "styles": payload.get("styles") or {},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    user = [p for p in user if p.get("id") != pid]
    user.append(preset)
    _save_user(user)
    return preset


def update(preset_id: str, patch: dict) -> Optional[dict]:
    """Update (or shadow a builtin as) a user preset."""
    user = _load_user()
    existing = next((p for p in user if p.get("id") == preset_id), None)
    if existing is None:
        builtin = next((p for p in BUILTIN_PRESETS if p["id"] == preset_id), None)
        if builtin is None:
            return None
        existing = dict(builtin)
        existing["builtin"] = False
        existing["created_at"] = _now_iso()
        user.append(existing)
    if "name" in patch:
        existing["name"] = patch["name"]
    if "styles" in patch and isinstance(patch["styles"], dict):
        existing["styles"] = {**(existing.get("styles") or {}), **patch["styles"]}
    existing["updated_at"] = _now_iso()
    _save_user(user)
    return existing


def delete(preset_id: str) -> bool:
    """Delete a user preset.  Built-ins can't be deleted (returns False)."""
    user = _load_user()
    new = [p for p in user if p.get("id") != preset_id]
    if len(new) == len(user):
        return False
    _save_user(new)
    return True


# --- preview swatch ----------------------------------------------------------
# A canned iso-cube line drawing exercising every category, so the preview
# reflects the real look (weight hierarchy, colours, dashes) instantly without
# meshing a real model.  Coordinates are a ~120x96 viewBox.
def _iso_cube_polys():
    import math
    cx, cy, s = 60.0, 50.0, 30.0
    c30 = math.cos(math.radians(30))
    top = (cx, cy - s)
    ur = (cx + c30 * s, cy - 0.5 * s)
    lr = (cx + c30 * s, cy + 0.5 * s)
    bot = (cx, cy + s)
    ll = (cx - c30 * s, cy + 0.5 * s)
    ul = (cx - c30 * s, cy - 0.5 * s)
    ctr = (cx, cy)                      # front corner (3 visible edges meet)
    back = (cx, cy + 0.30 * s)          # hidden corner (projected, offset down)
    # silhouette hexagon
    outline = [[top, ur, lr, bot, ll, ul, top]]
    # three visible internal edges
    sharp = [[ctr, top], [ctr, lr], [ctr, ll]]
    # a small fillet hint on the top face (smooth/tangent line)
    fa = (cx - 0.18 * s, cy - 0.62 * s)
    fb = (cx + 0.18 * s, cy - 0.62 * s)
    smooth = [[fa, (cx, cy - 0.50 * s), fb]]
    # three hidden back edges (dashed)
    hidden = [[back, ur], [back, ul], [back, bot]]
    return {
        "outline_v": outline,
        "sharp_v": sharp,
        "smooth_v": smooth,
        "hidden_sharp": hidden,
        "hidden_outline": [],
    }


def preview_svg(preset_or_id) -> str:
    """Return a small standalone SVG swatch drawn with a preset's styles."""
    from t5_hlr_vector import DEFAULT_STYLES
    resolved = resolve_styles(preset_or_id) or {}
    styles = {**DEFAULT_STYLES, **resolved}
    polys = _iso_cube_polys()
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 96" '
        'width="100%" height="100%" preserveAspectRatio="xMidYMid meet">',
        '<rect x="0" y="0" width="120" height="96" fill="#ffffff"/>',
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">',
    ]
    for cat in CATEGORIES:
        pls = polys.get(cat) or []
        if not pls:
            continue
        st = styles[cat]
        dash = f' stroke-dasharray="{st["dash"]}"' if st.get("dash") else ""
        # scale widths up a touch so thin mm strokes are visible in the swatch
        w = max(0.4, float(st["width"]) * 1.6)
        parts.append(f'<g stroke="{st["stroke"]}" stroke-width="{w:.2f}"{dash}>')
        for pl in pls:
            d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in pl)
            parts.append(f'<path d="{d}"/>')
        parts.append('</g>')
    parts.append('</g></svg>')
    return "\n".join(parts)
