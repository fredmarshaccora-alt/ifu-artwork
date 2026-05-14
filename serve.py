"""Local HTTP server that adds live HLR rendering to the viewer.

Run:
  python serve.py
    # builds viewer.html if missing, then serves on http://localhost:5000
  python serve.py --build
    # forces a full HLR rebuild of viewer.html first (slow; ~5 min)
  python serve.py --html-only
    # just re-bundle viewer.html from the cached catalogue, then serve

Each source's pre-rotated shape is loaded once at startup and held in
memory, so /api/render only pays the HLR + SVG-write cost per request
(~30-100s depending on assembly).

Endpoints:
  GET  /                serves out/viewer.html
  GET  /api/healthz     reports which sources are cached
  POST /api/render      body: {file_id, view_dir: [x,y,z]}
                        returns: SVG bytes (image/svg+xml)

The viewer's "generate 2D" button (3D toolbar) calls /api/render with the
current camera direction and injects the result as a new "Live" view in
the 2D pane.  No clipboard dance, no Python rerun.
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

import cadquery as cq
from flask import Flask, request, Response, send_file, jsonify

from build_viewer import (
    SOURCES, OUT, build_html, save_catalogue, load_catalogue, generate_svgs,
)
from t5_hlr_vector import (
    run_hlr_per_solid, write_svg_parts, rotate_shape,
)

HERE = Path(__file__).parent
app = Flask(__name__)


@app.after_request
def _cors_and_no_cache(resp):
    # Allow viewer.html loaded from file:// (or any other origin) to call
    # /api/*.  Single-user local tool -- no need to be picky about origins.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # Always serve a fresh viewer.html so a rebuild while the server is
    # running is picked up on the next reload.
    if request.path in ("/", "/viewer.html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/<path:_p>", methods=["OPTIONS"])
def _options(_p):
    return ("", 204)

# Shape cache: file_id -> (TopoDS_Shape, hlr_kw)
_SHAPES: dict[str, tuple] = {}


def boot():
    print("Loading sources into memory (one-time cost) ...")
    for entry in SOURCES:
        file_id, label, sp, hlr_kw, pre_rotate = entry[:5]
        if not sp.exists():
            print(f"  skip {file_id}: {sp} missing")
            continue
        print(f"  {file_id:<10s} ", end="", flush=True)
        t0 = time.time()
        shape = cq.importers.importStep(str(sp)).val().wrapped
        if pre_rotate is not None:
            axis, angle = pre_rotate
            shape = rotate_shape(shape, axis, angle)
        _SHAPES[file_id] = (shape, hlr_kw)
        print(f"loaded in {time.time()-t0:.1f}s")
    print(f"Cached {len(_SHAPES)} source(s).\n")


@app.route("/")
def index():
    return send_file(OUT / "viewer.html")


@app.route("/api/healthz")
def healthz():
    return jsonify({"ok": True, "sources": list(_SHAPES.keys())})


@app.route("/api/render", methods=["POST"])
def render():
    body = request.get_json(silent=True) or {}
    file_id = body.get("file_id")
    view_dir = body.get("view_dir") or []
    up_axis = body.get("up_axis")    # {"axis": [x,y,z], "angle": deg} or None
    if file_id not in _SHAPES:
        return jsonify({"error": f"unknown source: {file_id!r}",
                        "known": list(_SHAPES.keys())}), 400
    if not (isinstance(view_dir, list) and len(view_dir) == 3):
        return jsonify({"error": "view_dir must be a 3-element [x, y, z] list"}), 400
    view_dir = tuple(float(x) for x in view_dir)

    shape, hlr_kw = _SHAPES[file_id]
    # Apply the 3D viewer's Up: override to a fresh copy so the SVG matches
    # what the user was looking at when they clicked "generate 2D".  The
    # cache stays in its native pre-rotated state for the next request.
    extra_rot_str = ""
    if up_axis and float(up_axis.get("angle") or 0) != 0:
        try:
            ax = tuple(float(c) for c in up_axis["axis"])
            ang = float(up_axis["angle"])
            shape = rotate_shape(shape, ax, ang)
            extra_rot_str = f"  +rot({ax}, {ang:.0f}deg)"
        except Exception as exc:
            return jsonify({"error": f"bad up_axis: {exc}"}), 400
    t0 = time.time()
    try:
        parts = run_hlr_per_solid(shape, view_dir, **hlr_kw)
    except Exception as exc:
        return jsonify({"error": f"HLR failed: {type(exc).__name__}: {exc}"}), 500
    out_path = OUT / f"_live_{file_id}.svg"
    write_svg_parts(parts, out_path, precision=1)
    svg = out_path.read_text(encoding="utf-8")
    elapsed = time.time() - t0
    print(f"  /api/render {file_id:<10s} dir={tuple(round(x,3) for x in view_dir)}"
          f"{extra_rot_str}  {elapsed:.1f}s  {len(svg)//1024}KB")
    return Response(svg, mimetype="image/svg+xml",
                    headers={"X-Render-Seconds": f"{elapsed:.2f}"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true",
                    help="Force a full HLR rebuild before serving (~5 min)")
    ap.add_argument("--html-only", action="store_true",
                    help="Re-bundle viewer.html from cached catalogue then serve")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    viewer = OUT / "viewer.html"
    if args.build:
        print("Forcing full rebuild ...")
        cat = generate_svgs()
        save_catalogue(cat)
        build_html(cat)
    elif args.html_only:
        print("Re-bundling viewer.html from cached catalogue ...")
        cat = load_catalogue()
        if cat is None:
            print("  no catalogue cached; running full build")
            cat = generate_svgs()
            save_catalogue(cat)
        build_html(cat)
    elif not viewer.exists():
        print(f"viewer.html missing; building first ({viewer}) ...")
        cat = load_catalogue()
        if cat is None:
            cat = generate_svgs()
            save_catalogue(cat)
        build_html(cat)

    boot()
    url = f"http://{args.host}:{args.port}"
    print(f"Serving on {url}")
    print(f"  - open {url} in a browser")
    print("  - click 'generate 2D' in the 3D toolbar to render the current angle")
    print("  - Ctrl+C here to stop the server\n")
    # threaded=False: OCCT internals aren't thread-safe; a single in-flight
    # render is what we want anyway (the box can't HLR two at once).
    app.run(host=args.host, port=args.port, threaded=False, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
