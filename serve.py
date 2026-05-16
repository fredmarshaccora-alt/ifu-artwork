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
    run_part_silhouettes, run_group_silhouette,
    compute_visible_footprints, run_hlr_in_region,
)
from ifu import figures_store, projects_store, revisions_store
from ifu.config import SOURCES
import threading
import functools

# Serialises OCCT-touching endpoints (/api/render, /api/part_silhouettes,
# /api/part_footprints, /api/render_region).  Cheap endpoints
# (/api/figures*, /api/healthz, /) bypass the lock so the UI stays
# responsive even while a Presto raster runs for 2 minutes.
_HLR_LOCK = threading.Lock()


def _occt_serialised(fn):
    """Decorator: take _HLR_LOCK around an endpoint that touches OCCT."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _HLR_LOCK:
            return fn(*args, **kwargs)
    return _wrapper

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

# Per-part silhouette cache: (file_id, vd_key, focal_key, up_axis_key, idx)
# -> list of polylines in (u,v).  Each entry is one PolyAlgo on a single
# solid (cheap-ish: ~0.1-2s depending on part complexity), but if the
# user keeps the same angle and re-selects, the cache means instant.
_SIL_CACHE: dict[tuple, list] = {}
_SIL_CACHE_MAX = 2000

# Visible-footprint cache: (file_id, vd_key, focal_key, up_axis_key, idx)
# -> list of closed polylines tracing the part's visible 2D footprint.
# Cost is dominated by the single rasterize pass (all parts at once);
# results are returned per requested idx so caching is per-idx-per-view.
_FOOT_CACHE: dict[tuple, list] = {}
_FOOT_CACHE_MAX = 2000
# Rasterize-once tracker: bool keyed by (file_id, vd_key, focal_key,
# up_axis_key) so we don't redo the assembly raster within the same view
# when extra parts are requested.
_FOOT_RASTER_DONE: dict[tuple, bool] = {}


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
@_occt_serialised
def render():
    body = request.get_json(silent=True) or {}
    file_id = body.get("file_id")
    # Camera definition.  Two equivalent forms accepted:
    #   - {eye: [x,y,z], target: [x,y,z]}    (preferred -- unambiguous)
    #   - {view_dir: [x,y,z], focal: [x,y,z]} (legacy -- view_dir = eye-target)
    eye = body.get("eye")
    target = body.get("target")
    view_dir = body.get("view_dir")
    focal = body.get("focal")
    up_axis = body.get("up_axis")    # {"axis": [x,y,z], "angle": deg} or None

    if file_id not in _SHAPES:
        return jsonify({"error": f"unknown source: {file_id!r}",
                        "known": list(_SHAPES.keys())}), 400

    # Resolve view_dir + focal from whichever pair we got
    if isinstance(eye, list) and isinstance(target, list) \
            and len(eye) == 3 and len(target) == 3:
        eye = tuple(float(x) for x in eye)
        focal = tuple(float(x) for x in target)
        vd = (eye[0] - focal[0], eye[1] - focal[1], eye[2] - focal[2])
        mag = (vd[0] ** 2 + vd[1] ** 2 + vd[2] ** 2) ** 0.5
        if mag < 1e-9:
            return jsonify({"error": "eye and target coincide"}), 400
        view_dir = (vd[0] / mag, vd[1] / mag, vd[2] / mag)
    elif isinstance(view_dir, list) and len(view_dir) == 3:
        view_dir = tuple(float(x) for x in view_dir)
        if isinstance(focal, list) and len(focal) == 3:
            focal = tuple(float(x) for x in focal)
        else:
            focal = (0.0, 0.0, 0.0)
    else:
        return jsonify({"error":
            "supply either {eye, target} or {view_dir, focal}"}), 400

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

    # X-mirror is gone now.  build_projector was rewritten to build the
    # Ax2 with Z = +view_dir and X = up × view_dir, so OCCT's camera and
    # screen-X axis natively match three.js -- no post-process flip
    # needed.  Previously OCCT's camera was on the OPPOSITE side of the
    # model and the X-flip was a mask that broke for arbitrary view_dirs.
    t_mir = 0.0

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


# ----- Sources + Revisions (Phase C) ---------------------------------

@app.route("/api/sources", methods=["GET"])
def sources_list():
    """List configured sources -- read-only.  Tells the UI which sources
    have Onshape backing (and so support refresh / revision tracking)."""
    out = []
    for entry in SOURCES:
        out.append({
            "id": entry[0],
            "label": entry[1],
            "step_path": str(entry[2]),
            "onshape_ids": entry[5] if len(entry) > 5 else None,
        })
    return jsonify({"sources": out})


@app.route("/api/sources/<source_id>/versions", methods=["GET"])
def versions_list(source_id):
    """Return the cached Versions list for a source.  Empty/missing
    means never refreshed -- the caller should POST to .../refresh."""
    cached = revisions_store.cached_versions(source_id)
    if cached is None:
        return jsonify({"source_id": source_id, "versions": [],
                         "last_fetched_at": None})
    return jsonify(cached)


@app.route("/api/sources/<source_id>/versions/refresh", methods=["POST"])
def versions_refresh(source_id):
    """Hit Onshape to refresh the cached Versions list for this source."""
    try:
        env = revisions_store.refresh_versions(source_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error":
            f"refresh failed: {type(exc).__name__}: {exc}"}), 502
    return jsonify(env)


@app.route("/api/figures/<fig_id>/bind_revision", methods=["POST"])
def figure_bind_revision(fig_id):
    """Bind a figure to a specific cached Version of its source.

    Body: ``{version_id: "..."}``.  The full Version dict is stamped onto
    the figure's ``bound_revision`` field (id, name, description, created_at,
    microversion) so we can detect "N behind latest" without an extra
    cache hit later.

    Phase D-lite: this updates metadata only.  Actually re-rendering the
    figure against the new geometry requires pulling the STEP at the
    target Version from Onshape and rerunning HLR -- that's the
    "Phase D-full" work tracked in PLAN.md.
    """
    body = request.get_json(silent=True) or {}
    version_id = body.get("version_id")
    if not version_id:
        return jsonify({"error": "version_id required"}), 400
    fig = figures_store.load(fig_id)
    if fig is None:
        return jsonify({"error": "figure not found"}), 404
    src = fig.get("source_id")
    ver = revisions_store.find_version(src, version_id) if src else None
    if ver is None:
        return jsonify({"error":
            f"version {version_id!r} not in cache for {src!r}; "
            "refresh first"}), 404
    fig["bound_revision"] = {
        "id": ver["id"],
        "name": ver.get("name"),
        "description": ver.get("description", ""),
        "created_at": ver.get("created_at"),
        "microversion": ver.get("microversion"),
        "bound_at": figures_store._now_iso(),
    }
    # Append to an audit log so the illustrator can see "I bound this
    # figure to R04 on date X" later.
    audit = fig.setdefault("audit", [])
    audit.append({
        "what": "bind_revision",
        "version_id": ver["id"],
        "version_name": ver.get("name"),
        "at": figures_store._now_iso(),
    })
    figures_store.save(fig)
    return jsonify(fig)


@app.route("/api/figures/<fig_id>/revision_status", methods=["GET"])
def figure_revision_status(fig_id):
    """Quick lookup: 'this figure is bound to revision X, latest is Y,
    you're N versions behind'.  None of the fields are required to be
    present -- a figure with no bound_revision is a no-op."""
    fig = figures_store.load(fig_id)
    if fig is None:
        return jsonify({"error": "figure not found"}), 404
    bound = fig.get("bound_revision") or {}
    bound_id = bound.get("id")
    source_id = fig.get("source_id")
    latest = revisions_store.latest_version(source_id) if source_id else None
    behind = (revisions_store.versions_behind(source_id, bound_id)
              if bound_id and source_id else None)
    return jsonify({
        "figure_id": fig_id,
        "source_id": source_id,
        "bound_revision": bound or None,
        "latest_revision": latest,
        "versions_behind": behind,
    })


# ----- Projects CRUD (Phase B) ---------------------------------------

@app.route("/api/projects", methods=["GET"])
def projects_list():
    """List every project, newest first."""
    return jsonify({"projects": projects_store.list_all()})


@app.route("/api/projects", methods=["POST"])
def projects_create():
    body = request.get_json(silent=True) or {}
    name = body.get("name") or "Untitled project"
    description = body.get("description") or ""
    proj = projects_store.new_project(name=name, description=description)
    projects_store.save(proj)
    return jsonify(proj), 201


@app.route("/api/projects/<proj_id>", methods=["GET"])
def projects_get(proj_id):
    proj = projects_store.load(proj_id)
    if proj is None:
        return jsonify({"error": "project not found"}), 404
    return jsonify(proj)


@app.route("/api/projects/<proj_id>", methods=["PUT"])
def projects_update(proj_id):
    body = request.get_json(silent=True) or {}
    existing = projects_store.load(proj_id)
    if existing is None:
        return jsonify({"error": "project not found"}), 404
    body["id"] = existing["id"]
    body["created_at"] = existing.get("created_at",
                                       projects_store._now_iso())
    projects_store.save(body)
    return jsonify(body)


@app.route("/api/projects/<proj_id>", methods=["DELETE"])
def projects_delete(proj_id):
    """Delete project.  Query ``?cascade=1`` to also delete its figures;
    default leaves figures as orphans (their project_id is preserved
    on the figure record so you can later see "this figure used to
    belong to <deleted project>")."""
    cascade = request.args.get("cascade") in ("1", "true", "yes")
    ok = projects_store.delete(proj_id, cascade=cascade)
    if not ok:
        return jsonify({"error": "project not found"}), 404
    return ("", 204)


@app.route("/api/projects/<proj_id>/figures", methods=["GET"])
def projects_figures(proj_id):
    """List figures in this project (resolved dicts, not just ids)."""
    if projects_store.load(proj_id) is None:
        return jsonify({"error": "project not found"}), 404
    return jsonify({"figures": projects_store.figures_in(proj_id)})


@app.route("/api/projects/<proj_id>/figures/<fig_id>", methods=["POST"])
def projects_attach_figure(proj_id, fig_id):
    """Attach an existing figure to this project.  Idempotent."""
    ok = projects_store.add_figure(proj_id, fig_id)
    if not ok:
        return jsonify({"error": "project or figure not found"}), 404
    return ("", 204)


@app.route("/api/projects/<proj_id>/figures/<fig_id>", methods=["DELETE"])
def projects_detach_figure(proj_id, fig_id):
    """Remove the figure from this project (figure record itself stays)."""
    ok = projects_store.remove_figure(proj_id, fig_id)
    if not ok:
        return jsonify({"error": "figure not in project"}), 404
    return ("", 204)


@app.route("/api/figures/orphans", methods=["GET"])
def figures_orphans():
    """Figures with no project (or with a project_id pointing to a
    deleted project).  Shown in the UI's "Unfiled" bucket."""
    return jsonify({"figures": projects_store.orphan_figures()})


# ----- Figures CRUD (Phase A) ----------------------------------------

@app.route("/api/figures", methods=["GET"])
def figures_list():
    """List every figure in the local store, newest first."""
    return jsonify({"figures": figures_store.list_all()})


@app.route("/api/figures", methods=["POST"])
def figures_create():
    """Create a new figure.  Body: any subset of figure fields.  Missing
    fields get defaults from new_figure().  If ``project_id`` is
    supplied, the figure is attached to that project on creation."""
    body = request.get_json(silent=True) or {}
    name = body.get("name") or "Untitled figure"
    source_id = body.get("source_id")
    if not source_id:
        return jsonify({"error": "source_id required"}), 400
    view_id = body.get("view_id") or "iso"
    project_id = body.get("project_id")
    extra = {k: v for k, v in body.items()
             if k in ("camera", "selection", "styles_per_part",
                       "layers_on", "detail", "annotations", "notes")}
    fig = figures_store.new_figure(name=name, source_id=source_id,
                                    view_id=view_id, **extra)
    figures_store.save(fig)
    if project_id:
        # Attach (idempotent; silently no-ops if the project doesn't exist)
        projects_store.add_figure(project_id, fig["id"])
        # Re-load to pick up the project_id backlink stamped by add_figure
        fig = figures_store.load(fig["id"])
    return jsonify(fig), 201


@app.route("/api/figures/<fig_id>", methods=["GET"])
def figures_get(fig_id):
    fig = figures_store.load(fig_id)
    if fig is None:
        return jsonify({"error": "figure not found"}), 404
    return jsonify(fig)


@app.route("/api/figures/<fig_id>", methods=["PUT"])
def figures_update(fig_id):
    """Replace the figure's mutable fields (everything except id /
    created_at).  Body should be the complete figure dict."""
    body = request.get_json(silent=True) or {}
    existing = figures_store.load(fig_id)
    if existing is None:
        return jsonify({"error": "figure not found"}), 404
    # Preserve immutable fields
    body["id"] = existing["id"]
    body["created_at"] = existing.get("created_at", figures_store._now_iso())
    figures_store.save(body)
    return jsonify(body)


@app.route("/api/figures/<fig_id>", methods=["DELETE"])
def figures_delete(fig_id):
    ok = figures_store.delete(fig_id)
    if not ok:
        return jsonify({"error": "figure not found"}), 404
    return ("", 204)


@app.route("/api/render_region", methods=["POST"])
@_occt_serialised
def render_region():
    """Render JUST the solids inside a 2D bounding box at higher detail.

    Used by the "zoom in, give me the fine version" workflow.  Skipping
    the parts outside the viewport means we can crank the mesh + sample
    deflection without paying the full-assembly cost.

    POST JSON:
      {file_id, eye+target or view_dir+focal, up_axis?,
       bbox_uv: [u_min, v_min, u_max, v_max],
       mesh_defl?, sample_defl?, padding_mm?}
    Returns: image/svg+xml
    """
    body = request.get_json(silent=True) or {}
    file_id = body.get("file_id")
    bbox_uv = body.get("bbox_uv")
    mesh_defl = float(body.get("mesh_defl") or 0.4)
    sample_defl = float(body.get("sample_defl") or 0.4)
    padding_mm = float(body.get("padding_mm") or 10.0)

    if file_id not in _SHAPES:
        return jsonify({"error": f"unknown source: {file_id!r}",
                        "known": list(_SHAPES.keys())}), 400
    if not isinstance(bbox_uv, list) or len(bbox_uv) != 4:
        return jsonify({"error":
            "bbox_uv must be [u_min, v_min, u_max, v_max]"}), 400
    try:
        bbox_uv = tuple(float(x) for x in bbox_uv)
    except Exception:
        return jsonify({"error": "bbox_uv must be 4 floats"}), 400
    if bbox_uv[0] >= bbox_uv[2] or bbox_uv[1] >= bbox_uv[3]:
        return jsonify({"error": "bbox_uv must have u_min<u_max and v_min<v_max"}), 400

    # Camera resolution (same as /api/render)
    eye = body.get("eye"); target = body.get("target")
    view_dir = body.get("view_dir"); focal = body.get("focal")
    up_axis = body.get("up_axis")
    if isinstance(eye, list) and isinstance(target, list) \
            and len(eye) == 3 and len(target) == 3:
        eye = tuple(float(x) for x in eye)
        focal = tuple(float(x) for x in target)
        vd = (eye[0] - focal[0], eye[1] - focal[1], eye[2] - focal[2])
        mag = (vd[0]**2 + vd[1]**2 + vd[2]**2) ** 0.5
        if mag < 1e-9:
            return jsonify({"error": "eye and target coincide"}), 400
        view_dir = (vd[0] / mag, vd[1] / mag, vd[2] / mag)
    elif isinstance(view_dir, list) and len(view_dir) == 3:
        view_dir = tuple(float(x) for x in view_dir)
        focal = tuple(float(x) for x in focal) if isinstance(focal, list) else (0.0, 0.0, 0.0)
    else:
        return jsonify({"error":
            "supply either {eye, target} or {view_dir, focal}"}), 400

    shape, _src_hlr_kw = _SHAPES[file_id]
    if up_axis and float(up_axis.get("angle") or 0) != 0:
        try:
            ax = tuple(float(c) for c in up_axis["axis"])
            ang = float(up_axis["angle"])
            shape = rotate_shape(shape, ax, ang)
        except Exception as exc:
            return jsonify({"error": f"bad up_axis: {exc}"}), 400

    t0 = time.time()
    try:
        parts = run_hlr_in_region(
            shape, view_dir, focal=focal,
            bbox_uv=bbox_uv,
            mesh_defl=mesh_defl, sample_defl=sample_defl,
            padding_mm=padding_mm)
    except Exception as exc:
        import traceback; traceback.print_exc()
        return jsonify({"error":
            f"region render failed: {type(exc).__name__}: {exc}"}), 500
    t_hlr = time.time() - t0

    out_path = OUT / f"_region_{file_id}.svg"
    write_svg_parts(parts, out_path, precision=1)
    svg_bytes = out_path.read_bytes()
    print(f"  /api/render_region {file_id:<10s} bbox={bbox_uv} "
          f"defl=({mesh_defl},{sample_defl}) parts={len(parts)} "
          f"hlr={t_hlr:.2f}s size={len(svg_bytes)//1024}KB")
    return Response(svg_bytes, mimetype="image/svg+xml", headers={
        "X-Region-Parts": str(len(parts)),
        "X-Region-Seconds": f"{t_hlr:.2f}",
    })


@app.route("/api/part_footprints", methods=["POST"])
@_occt_serialised
def part_footprints():
    """Visible-footprint boundaries per part.

    For each part_idx in the request, returns the closed polyline(s)
    outlining the part's actually-visible 2D region in the current view
    (occluder cuts drawn along the occluder's boundary).  Same camera
    grammar as /api/render.

    Implementation rasterizes the whole assembly into an ID buffer
    once per view, then extracts contours for every part.  All results
    cached, so subsequent requests in the same view are cache hits.
    """
    body = request.get_json(silent=True) or {}
    file_id = body.get("file_id")
    part_indices = body.get("part_indices") or []
    eye = body.get("eye")
    target = body.get("target")
    view_dir = body.get("view_dir")
    focal = body.get("focal")
    up_axis = body.get("up_axis")

    if file_id not in _SHAPES:
        return jsonify({"error": f"unknown source: {file_id!r}",
                        "known": list(_SHAPES.keys())}), 400
    if not isinstance(part_indices, list) or not part_indices:
        return jsonify({"error": "part_indices must be a non-empty list"}), 400
    try:
        part_indices = [int(i) for i in part_indices]
    except Exception:
        return jsonify({"error": "part_indices must be a list of ints"}), 400

    # Camera resolution (same logic as /api/render)
    if isinstance(eye, list) and isinstance(target, list) \
            and len(eye) == 3 and len(target) == 3:
        eye = tuple(float(x) for x in eye)
        focal = tuple(float(x) for x in target)
        vd = (eye[0] - focal[0], eye[1] - focal[1], eye[2] - focal[2])
        mag = (vd[0] ** 2 + vd[1] ** 2 + vd[2] ** 2) ** 0.5
        if mag < 1e-9:
            return jsonify({"error": "eye and target coincide"}), 400
        view_dir = (vd[0] / mag, vd[1] / mag, vd[2] / mag)
    elif isinstance(view_dir, list) and len(view_dir) == 3:
        view_dir = tuple(float(x) for x in view_dir)
        if isinstance(focal, list) and len(focal) == 3:
            focal = tuple(float(x) for x in focal)
        else:
            focal = (0.0, 0.0, 0.0)
    else:
        return jsonify({"error":
            "supply either {eye, target} or {view_dir, focal}"}), 400

    shape, hlr_kw = _SHAPES[file_id]
    up_axis_key: tuple = ()
    if up_axis and float(up_axis.get("angle") or 0) != 0:
        try:
            ax = tuple(float(c) for c in up_axis["axis"])
            ang = float(up_axis["angle"])
            shape = rotate_shape(shape, ax, ang)
            up_axis_key = (round(ax[0], 3), round(ax[1], 3),
                           round(ax[2], 3), round(ang, 1))
        except Exception as exc:
            return jsonify({"error": f"bad up_axis: {exc}"}), 400

    vd_key = tuple(round(x, 3) for x in view_dir)
    focal_key = tuple(round(x, 1) for x in focal)
    view_key = (file_id, vd_key, focal_key, up_axis_key)

    out_polys: dict[int, list] = {}
    misses: list[int] = []
    for idx in part_indices:
        ck = view_key + (idx,)
        cached = _FOOT_CACHE.get(ck)
        if cached is not None:
            out_polys[idx] = cached
        else:
            misses.append(idx)

    t_raster = 0.0
    if misses and not _FOOT_RASTER_DONE.get(view_key):
        # First request in this view: rasterize the WHOLE assembly so
        # we get every part's footprint in one go.  Subsequent requests
        # in this view hit the cache regardless of which parts are asked.
        all_indices = list(range(len(_count_solids(shape))))
        t0 = time.time()
        try:
            full = compute_visible_footprints(
                shape, all_indices, view_dir, focal=focal,
                mesh_defl=hlr_kw.get("mesh_defl", 0.8),
            )
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"error":
                f"footprint failed: {type(exc).__name__}: {exc}"}), 500
        t_raster = time.time() - t0
        for idx, polys in full.items():
            ck = view_key + (idx,)
            if len(_FOOT_CACHE) >= _FOOT_CACHE_MAX:
                _FOOT_CACHE.pop(next(iter(_FOOT_CACHE)))
            _FOOT_CACHE[ck] = polys
        _FOOT_RASTER_DONE[view_key] = True
        for idx in misses:
            out_polys[idx] = full.get(idx, [])

    payload = {
        "part_indices": part_indices,
        "polylines": {str(i): out_polys.get(i, []) for i in part_indices},
        "stats": {
            "hits": len(part_indices) - len(misses),
            "misses": len(misses),
            "raster_seconds": round(t_raster, 3),
        },
    }
    print(f"  /api/part_footprints {file_id:<10s} "
          f"parts={part_indices[:8]}{'...' if len(part_indices)>8 else ''} "
          f"hits={len(part_indices)-len(misses)} misses={len(misses)} "
          f"raster={t_raster:.2f}s")
    return jsonify(payload)


def _count_solids(shape):
    """Return list of all solid indices in shape order (matches the
    indexing used everywhere else in this pipeline)."""
    from t5_hlr_vector import split_solids
    return [idx for idx, _label, _solid in split_solids(shape)]


@app.route("/api/part_silhouettes", methods=["POST"])
@_occt_serialised
def part_silhouettes():
    """Per-part TRUE silhouettes for the highlighted parts, computed
    by running HLR on each requested solid IN ISOLATION (no occluders).

    POST JSON: {file_id, part_indices: [int, ...], eye, target, up_axis?}
    Returns:   {part_indices: [...], polylines: {idx: [[[u,v], ...], ...]}}

    Polyline (u,v) is in the same projection frame as /api/render, so
    the client can drop them straight into the SVG layer at the same
    scale(1,-1) coordinate space.
    """
    body = request.get_json(silent=True) or {}
    file_id = body.get("file_id")
    part_indices = body.get("part_indices") or []
    eye = body.get("eye")
    target = body.get("target")
    view_dir = body.get("view_dir")
    focal = body.get("focal")
    up_axis = body.get("up_axis")
    group_mode = bool(body.get("group"))

    if file_id not in _SHAPES:
        return jsonify({"error": f"unknown source: {file_id!r}",
                        "known": list(_SHAPES.keys())}), 400
    if not isinstance(part_indices, list) or not part_indices:
        return jsonify({"error": "part_indices must be a non-empty list"}), 400
    try:
        part_indices = [int(i) for i in part_indices]
    except Exception:
        return jsonify({"error": "part_indices must be a list of ints"}), 400

    # Same camera-resolution logic as /api/render
    if isinstance(eye, list) and isinstance(target, list) \
            and len(eye) == 3 and len(target) == 3:
        eye = tuple(float(x) for x in eye)
        focal = tuple(float(x) for x in target)
        vd = (eye[0] - focal[0], eye[1] - focal[1], eye[2] - focal[2])
        mag = (vd[0] ** 2 + vd[1] ** 2 + vd[2] ** 2) ** 0.5
        if mag < 1e-9:
            return jsonify({"error": "eye and target coincide"}), 400
        view_dir = (vd[0] / mag, vd[1] / mag, vd[2] / mag)
    elif isinstance(view_dir, list) and len(view_dir) == 3:
        view_dir = tuple(float(x) for x in view_dir)
        if isinstance(focal, list) and len(focal) == 3:
            focal = tuple(float(x) for x in focal)
        else:
            focal = (0.0, 0.0, 0.0)
    else:
        return jsonify({"error":
            "supply either {eye, target} or {view_dir, focal}"}), 400

    shape, hlr_kw = _SHAPES[file_id]
    up_axis_key: tuple = ()
    if up_axis and float(up_axis.get("angle") or 0) != 0:
        try:
            ax = tuple(float(c) for c in up_axis["axis"])
            ang = float(up_axis["angle"])
            shape = rotate_shape(shape, ax, ang)
            up_axis_key = (round(ax[0], 3), round(ax[1], 3),
                           round(ax[2], 3), round(ang, 1))
        except Exception as exc:
            return jsonify({"error": f"bad up_axis: {exc}"}), 400

    vd_key = tuple(round(x, 3) for x in view_dir)
    focal_key = tuple(round(x, 1) for x in focal)
    base_key = (file_id, vd_key, focal_key, up_axis_key)

    # Group mode: ONE silhouette around the compound of every selected
    # part.  Cache key is the full sorted index tuple so different group
    # compositions don't share results.
    if group_mode:
        gkey = base_key + ("group", tuple(sorted(part_indices)))
        cached = _SIL_CACHE.get(gkey)
        if cached is not None:
            print(f"  /api/part_silhouettes {file_id:<10s} GROUP "
                  f"parts={part_indices} CACHE HIT")
            return jsonify({
                "part_indices": part_indices,
                "group": True,
                "polylines": {"group": cached},
                "stats": {"hits": 1, "misses": 0, "hlr_seconds": 0.0},
            })
        t0 = time.time()
        try:
            polys = run_group_silhouette(
                shape, part_indices, view_dir, focal=focal,
                mesh_defl=hlr_kw.get("mesh_defl", 0.4),
                sample_defl=hlr_kw.get("sample_defl", 0.3),
            )
        except Exception as exc:
            return jsonify({"error":
                f"group silhouette failed: {type(exc).__name__}: {exc}"}), 500
        t_hlr = time.time() - t0
        if len(_SIL_CACHE) >= _SIL_CACHE_MAX:
            _SIL_CACHE.pop(next(iter(_SIL_CACHE)))
        _SIL_CACHE[gkey] = polys
        print(f"  /api/part_silhouettes {file_id:<10s} GROUP "
              f"parts={part_indices} hlr={t_hlr:.2f}s polys={len(polys)}")
        return jsonify({
            "part_indices": part_indices,
            "group": True,
            "polylines": {"group": polys},
            "stats": {"hits": 0, "misses": 1, "hlr_seconds": round(t_hlr, 3)},
        })

    # Split into cached vs uncached so we only pay HLR for the misses.
    out_polys: dict[int, list] = {}
    misses: list[int] = []
    for idx in part_indices:
        ck = base_key + (idx,)
        cached = _SIL_CACHE.get(ck)
        if cached is not None:
            out_polys[idx] = cached
        else:
            misses.append(idx)

    t_hlr = 0.0
    if misses:
        t0 = time.time()
        try:
            fresh = run_part_silhouettes(
                shape, misses, view_dir, focal=focal,
                mesh_defl=hlr_kw.get("mesh_defl", 0.4),
                sample_defl=hlr_kw.get("sample_defl", 0.3),
            )
        except Exception as exc:
            return jsonify({"error":
                f"silhouette failed: {type(exc).__name__}: {exc}"}), 500
        t_hlr = time.time() - t0
        for idx, polys in fresh.items():
            ck = base_key + (idx,)
            if len(_SIL_CACHE) >= _SIL_CACHE_MAX:
                _SIL_CACHE.pop(next(iter(_SIL_CACHE)))
            _SIL_CACHE[ck] = polys
            out_polys[idx] = polys

    payload = {
        "part_indices": part_indices,
        "polylines": {str(i): out_polys.get(i, []) for i in part_indices},
        "stats": {
            "hits": len(part_indices) - len(misses),
            "misses": len(misses),
            "hlr_seconds": round(t_hlr, 3),
        },
    }
    print(f"  /api/part_silhouettes {file_id:<10s} "
          f"parts={part_indices} hits={len(part_indices)-len(misses)} "
          f"misses={len(misses)} hlr={t_hlr:.2f}s")
    return jsonify(payload)


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
    # threaded=True: but OCCT-critical endpoints take _HLR_LOCK so we
    # never run two HLR / footprint computations at once.  Cheap
    # endpoints (healthz, figures CRUD, /) bypass the lock and respond
    # instantly even while a render is in flight.  Without this the
    # whole server hangs for the 2+ minutes a Presto raster takes.
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
