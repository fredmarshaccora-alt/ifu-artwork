"""Rebuild the viewer HTML from existing SVG files on disk.

Doesn't run HLR. Useful after slim_svg.py reduces sizes, or while
iterating on the viewer's JS/CSS.

Catalogue (file list, part list per file) is reconstructed by:
  - reading each SOURCES entry's STEP path for part identity, OR
  - if --no-step is passed, parsing data-part / data-label attrs out of the SVG

The --no-step path avoids re-loading 61MB STEP files just to rebuild HTML.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

from build_viewer import (SOURCES, VIEWS, SOURCE_VIEW_SUBSET, OUT,
                           build_html, save_catalogue)


def parts_from_svg(svg_path: Path):
    """Scrape data-part / data-label off the existing SVG."""
    if not svg_path.exists():
        return []
    text = svg_path.read_text(encoding="utf-8")
    seen = {}
    for m in re.finditer(
        r'<g class="part part-(\d+)" data-part="(\d+)" data-label="([^"]+)"',
        text,
    ):
        idx = int(m.group(2))
        if idx not in seen:
            seen[idx] = {"idx": idx, "label": m.group(3)}
    return [seen[k] for k in sorted(seen)]


def main():
    catalogue = []
    for entry in SOURCES:
        file_id, file_label, sp = entry[0], entry[1], entry[2]
        del entry
        view_filter = SOURCE_VIEW_SUBSET.get(file_id)
        # use the first available view to scrape parts
        parts = []
        views_meta = []
        for view_id, view_label, vd in VIEWS:
            if view_filter and view_id not in view_filter:
                continue
            svg_name = f"{file_id}__{view_id}.svg"
            svg_path = OUT / svg_name
            if not svg_path.exists():
                continue
            if not parts:
                parts = parts_from_svg(svg_path)
            views_meta.append({
                "view_id": view_id,
                "view_label": view_label,
                "view_dir": list(vd),
                "svg_file": svg_name,
                "bbox": None,
            })
        if views_meta:
            catalogue.append({
                "file_id": file_id,
                "file_label": file_label,
                "parts": parts,
                "views": views_meta,
            })
            print(f"  {file_id}: {len(parts)} parts, {len(views_meta)} views")
    save_catalogue(catalogue)
    build_html(catalogue)


if __name__ == "__main__":
    main()
