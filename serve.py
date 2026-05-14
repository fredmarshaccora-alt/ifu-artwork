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

# Render cache: maps (file_id, rounded view_dir, rounded focal, up_axis)
# to the SVG bytes for that exact render.  Exact HLR is the killer cost
# (~80s for Presto), so memoising lets the user revisit an angle for free.
_RENDER_CACHE: dict[tuple, bytes] = {}
_RENDER_CACHE_MAX = 20    # rough cap on memory


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
    focal = body.get("focal")        # [x, y, z] world-space point the camera looks at
    up_axis = body.get("up_axis")    # {"axis": [x,y,z], "angle": deg} or None
    if file_id not in _SHAPES:
        return jsonify({"error": f"unknown source: {file_id!r}",
                        "known": list(_SHAPES.keys())}), 400
    if not (isinstance(view_dir, list) and len(view_dir) == 3):
        return jsonify({"error": "view_dir must be a 3-element [x, y, z] list"}), 400
    view_dir = tuple(float(x) for x in view_dir)
    if isinstance(focal, list) and len(focal) == 3:
        focal = tuple(float(x) for x in focal)
    else:
        focal = (0.0, 0.0, 0.0)

    shape, hlr_kw = _SHAPES[file_id]
    # Apply the 3D viewer's Up: override to a fresh copy so the SVG matches
    # what the user was looking at when they clicked "generate 2D".  The
    # cache stays in its native pre-rotated state for the next request.
    extra_rot_str = ""
    up_axis_key: tuple = ()
    if up_axis and float(up_axis.get("angle") or 0) != 0:
        try:
            ax = tuple(float(c) for c in up_axis["axis"])
            ang = float(up_axis["angle"])
            shape = rotate_shape(shape, ax, ang)
            extra_rot_str = f"  +rot({ax}, {ang:.0f}deg)"
            up_axis_key = (round(ax[0], 3), round(ax[1], 3), round(ax[2], 3),
                           round(ang, 1))
        except Exception as exc:
            return jsonify({"error": f"bad up_axis: {exc}"}), 400

    # Cache check: identical (source, view, focal, up_axis) -> instant.
    vd_key = tuple(round(x, 3) for x in view_dir)
    focal_key = tuple(round(x, 1) for x in focal)
    cache_key = (file_id, vd_key, focal_key, up_axis_key)
    cached = _RENDER_CACHE.get(cache_key)
    if cached is not None:
        print(f"  /api/render {file_id:<10s} dir={vd_key}{extra_rot_str}  "
              f"CACHE HIT  {len(cached)//1024}KB")
        return Response(cached, mimetype="image/svg+xml", headers={
            "X-Render-Seconds": "0.0",
            "X-Render-Breakdown": "cache-hit",
        })
    t_hlr0 = time.time()
    try:
        parts = run_hlr_per_solid(shape, view_dir, focal=focal, **hlr_kw)
    except Exception as exc:
        return jsonify({"error": f"HLR failed: {type(exc).__name__}: {exc}"}), 500
    t_hlr = time.time() - t_hlr0

    # three.js camera-right = up × view_dir; OCCT screen-X = view_dir × up.
    # The two are EXACT negatives of each other for any view_dir (with
    # up=world Z), so the OCCT SVG comes back as the horizontal mirror of
    # what the user saw in 3D.  Negate polyline X to compensate.
    # Verified empirically by side-by-side render with mirror off: the
    # 2D bed appears reversed left-to-right relative to the 3D pane.
    t_mir0 = time.time()
    for part in parts:
        polys = part.get("polys", {})
        for cat in list(polys.keys()):
            polys[cat] = [[(-x, y) for (x, y) in pl] for pl in polys[cat]]
    t_mir = time.time() - t_mir0

    t_svg0 = time.time()
    out_path = OUT / f"_live_{file_id}.svg"
    write_svg_parts(parts, out_path, precision=1)
    svg = out_path.read_text(encoding="utf-8")
    t_svg = time.time() - t_svg0
    elapsed = t_hlr + t_mir + t_svg
    breakdown = f"hlr={t_hlr:.1f}s mirror={t_mir:.1f}s svg-write={t_svg:.1f}s"
    print(f"  /api/render {file_id:<10s} dir={tuple(round(x,3) for x in view_dir)}"
          f"{extra_rot_str}  total={elapsed:.1f}s ({breakdown})  "
          f"{len(svg)//1024}KB")
    # Insert into render cache (evict oldest if over cap)
    svg_bytes = svg.encode("utf-8")
    if len(_RENDER_CACHE) >= _RENDER_CACHE_MAX:
        _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
    _RENDER_CACHE[cache_key] = svg_bytes

    return Response(svg_bytes, mimetype="image/svg+xml", headers={
        "X-Render-Seconds": f"{elapsed:.2f}",
        "X-Render-Breakdown": breakdown,
    })


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
