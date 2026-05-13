"""Post-process an SVG produced by t5_hlr_vector to reduce file size.

Operations (each optional):
  --strip-hidden    remove hidden_outline + hidden_sharp layer groups
  --strip-smooth    remove smooth_v layer group
  --precision N     reduce coordinate precision to N decimal places (default 1)

Useful when the original SVG was produced before precision/skip_categories
options existed, or when you want to slim a layer post-hoc.
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path


LAYER_BLOCK_RE = re.compile(
    r'<g class="layer layer-(\w+)"[^>]*>.*?</g>(?:\s*</g>)?',
    re.DOTALL,
)


def strip_layer(svg: str, layer_name: str) -> str:
    """Remove the entire <g class="layer layer-NAME"> block (and matching </g>).

    Our SVGs nest one <g> per part inside each layer <g>.  The structure is:

      <g class="layer layer-NAME" stroke="..." stroke-width="...">
        <g class="part part-XXX" ...>
          <path .../>
          <path .../>
        </g>
        <g class="part part-YYY" ...>
          ...
        </g>
      </g>

    We need to delete from the layer opening tag through its matching closing
    </g> (the LAST </g> in the run, since parts also use </g>).  Match-balance
    by counting.
    """
    open_tag = f'<g class="layer layer-{layer_name}"'
    out = []
    i = 0
    while i < len(svg):
        idx = svg.find(open_tag, i)
        if idx == -1:
            out.append(svg[i:])
            break
        out.append(svg[i:idx])
        # find matching </g> by depth counting
        depth = 0
        j = idx
        while j < len(svg):
            if svg[j:j + 2] == "<g":
                depth += 1
                # advance past tag
                close = svg.find(">", j)
                j = close + 1 if close != -1 else j + 1
            elif svg[j:j + 4] == "</g>":
                depth -= 1
                j += 4
                if depth == 0:
                    break
            else:
                j += 1
        i = j
    return "".join(out)


def combine_paths_in_parts(svg: str) -> str:
    """Combine all <path d="..."/> children of each <g class="part part-NNN">
    into a single <path d="M ... L ... M ... L ..."/>.

    SVG renders multi-subpath strings identically (M starts a new subpath).
    Drops node count from O(polylines) to O(parts), often 30-100x reduction.
    """
    pattern = re.compile(
        r'(<g class="part part-\d+"[^>]*>)(.*?)(</g>)',
        re.DOTALL,
    )

    def reducer(m):
        opening, inner, closing = m.group(1), m.group(2), m.group(3)
        ds = re.findall(r'<path d="([^"]+)"\s*/>', inner)
        if not ds:
            return m.group(0)
        combined = " ".join(ds)
        return f"{opening}<path d=\"{combined}\"/>{closing}"

    return pattern.sub(reducer, svg)


def round_coords(svg: str, precision: int) -> str:
    """Reduce decimal places in all numbers in <path d=...> attributes."""
    fmt = "%." + str(precision) + "f"
    def reduce_path(m):
        d = m.group(1)
        def repl_num(nm):
            try:
                return fmt % float(nm.group(0))
            except ValueError:
                return nm.group(0)
        d_new = re.sub(r"-?\d+\.\d+(?:[eE][+-]?\d+)?", repl_num, d)
        return f'd="{d_new}"'
    return re.sub(r'd="([^"]+)"', reduce_path, svg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--strip-hidden", action="store_true")
    ap.add_argument("--strip-smooth", action="store_true")
    ap.add_argument("--precision", type=int, default=1)
    args = ap.parse_args()

    svg = args.input.read_text(encoding="utf-8")
    n0 = len(svg)
    if args.strip_hidden:
        svg = strip_layer(svg, "hidden_outline")
        svg = strip_layer(svg, "hidden_sharp")
    if args.strip_smooth:
        svg = strip_layer(svg, "smooth_v")
    svg = round_coords(svg, args.precision)
    n1 = len(svg)
    args.output.write_text(svg, encoding="utf-8")
    print(f"{args.input.name}: {n0/1024:.0f}KB  ->  "
          f"{args.output.name}: {n1/1024:.0f}KB  "
          f"({100*(n0-n1)/n0:.0f}% smaller)")


if __name__ == "__main__":
    main()
