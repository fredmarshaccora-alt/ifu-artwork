"""One-shot SVG transformer that merges every <path> inside a per-part
<g class="part part-XXX"> group into a single <path> with combined d
attribute.  Visually identical, but ~200x fewer DOM nodes so the browser
can pan/zoom/select without choking on hundreds of thousands of elements.

Idempotent: groups that already contain exactly one <path> are skipped.

Run AFTER a full build (out/*.svg exist) and BEFORE rebuild_html.py to
re-bundle the slimmer SVGs into viewer.html.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(__file__).parent / "out"

# Match a <g class="part part-XXX" ...> ... </g> block, capturing the
# opening tag and the inner content.  Greedy on inner, but bounded by the
# nearest </g> since our SVGs don't nest parts inside parts.
_PART_GROUP = re.compile(
    r'(<g class="part part-\d+"[^>]*>)(.*?)(</g>)',
    re.DOTALL,
)
_PATH_D = re.compile(r'<path\s+d="([^"]+)"\s*/>')


def merge_one(svg_text: str) -> tuple[str, int, int]:
    """Return (new_text, paths_before, paths_after)."""
    before = 0
    after = 0

    def replace_group(m: re.Match) -> str:
        nonlocal before, after
        open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
        paths = _PATH_D.findall(inner)
        before += len(paths)
        if len(paths) < 2:
            # Already merged or empty -- leave alone
            after += len(paths)
            return m.group(0)
        merged_d = " ".join(p.strip() for p in paths)
        after += 1
        return f'{open_tag}<path d="{merged_d}"/>{close_tag}'

    new_text = _PART_GROUP.sub(replace_group, svg_text)
    return new_text, before, after


def main() -> int:
    svgs = sorted(p for p in OUT.glob("*.svg")
                  if not p.name.startswith("_"))
    if not svgs:
        print("No SVGs in out/, nothing to merge.")
        return 0
    total_before = 0
    total_after = 0
    for svg in svgs:
        text = svg.read_text(encoding="utf-8")
        new_text, b, a = merge_one(text)
        if a == b:
            print(f"  {svg.name:<35s}  paths {b:>6d} -> {a:>6d}  (skip)")
            continue
        size_before = svg.stat().st_size
        svg.write_text(new_text, encoding="utf-8")
        size_after = svg.stat().st_size
        pct = 100 * (size_before - size_after) // max(size_before, 1)
        print(f"  {svg.name:<35s}  paths {b:>6d} -> {a:>6d}  "
              f"({size_before//1024} -> {size_after//1024}KB, "
              f"-{pct}%)")
        total_before += b
        total_after += a
    print(f"\nTotal paths: {total_before} -> {total_after} "
          f"({100*(total_before-total_after)//max(total_before,1)}% fewer)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
