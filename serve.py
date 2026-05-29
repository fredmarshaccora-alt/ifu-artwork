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
import os
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
    split_solids,
)
from ifu import (figures_store, projects_store, revisions_store,
                  settings_store, sources_store, onshape_fetch,
                  views_store)
from ifu.config import SOURCES
import threading
import functools
from collections import deque

# Serialises OCCT-touching endpoints (/api/render, /api/part_silhouettes,
# /api/part_footprints, /api/render_region).  Cheap endpoints
# (/api/figures*, /api/healthz, /) bypass the lock so the UI stays
# responsive even while a Presto raster runs for 2 minutes.
_HLR_LOCK = threading.Lock()

# Serialises lazy source imports so two concurrent requests for the same
# not-yet-loaded source don't both import the STEP.  Held only inside
# _ensure_source_loaded (never nested with _HLR_LOCK), so no deadlock.
_LOAD_LOCK = threading.Lock()

# Track who holds _HLR_LOCK so we can diagnose hangs without strace.
# Updated under the lock; readers may see stale values but that's fine
# for diagnostic purposes.
_HLR_LOCK_HOLDER = {"endpoint": None, "since": None, "thread": None}


def _occt_serialised(fn):
    """Decorator: take _HLR_LOCK around an endpoint that touches OCCT.

    Logs lock-acquire and release so the debug overlay can show which
    endpoint is currently inside OCCT.  If a render hangs in OCCT C++
    land (Python can't preempt it), the log shows exactly which one
    grabbed the lock and never gave it back.
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        ep = fn.__name__
        t_wait0 = time.time()
        # Try to acquire with a quick non-block check so we can log
        # if anything is blocking us.
        if not _HLR_LOCK.acquire(blocking=False):
            holder = dict(_HLR_LOCK_HOLDER)
            _log_event(
                level="warn", op="occt.wait",
                endpoint=ep,
                blocking=holder.get("endpoint") or "?",
                held_for_sec=round(
                    time.time() - (holder.get("since") or time.time()), 1),
            )
            _HLR_LOCK.acquire()
        try:
            _HLR_LOCK_HOLDER.update(
                endpoint=ep, since=time.time(),
                thread=threading.current_thread().name)
            t_wait = time.time() - t_wait0
            _log_event(level="info", op="occt.enter", endpoint=ep,
                        wait_sec=round(t_wait, 2))
            t0 = time.time()
            try:
                return fn(*args, **kwargs)
            finally:
                _log_event(level="ok", op="occt.exit", endpoint=ep,
                            held_sec=round(time.time() - t0, 2))
        finally:
            _HLR_LOCK_HOLDER.update(endpoint=None, since=None,
                                     thread=None)
            _HLR_LOCK.release()
    return _wrapper


# ---- Rolling request log ----------------------------------------------
# Captures every API call (path, method, status, duration, source_id if
# extractable, error message) into an in-memory ring buffer.  /api/debug/log
# returns it as JSON; the editor's "Server log" overlay polls and prints
# it so the user can see at a glance which endpoint ran and how it ended.
_LOG_BUFFER: "deque[dict]" = deque(maxlen=500)
_LOG_LOCK = threading.Lock()
_LOG_SEQ = [0]

# On-disk mirror of the structured log so events survive a server
# restart.  Rotate at _LOG_DISK_MAX_BYTES; keep one .1 backup; drop the
# rest.  No external logger -- single-user tool, append-only is fine.
_LOG_DISK_PATH = Path(__file__).parent / "out" / "debug.log"
_LOG_DISK_PREV = Path(__file__).parent / "out" / "debug.log.1"
_LOG_DISK_MAX_BYTES = 5 * 1024 * 1024  # 5 MB before rotation

# ---- Interaction-capture store (closed-loop debugging) ----------------
# When the user hits "capture" in the editor, the client POSTs the live
# SVG snapshot + tracker entries + current selection here.  We write each
# capture to debug_captures/ so a headless harness can render it, crop a
# zoomed screenshot around the clicked parts, and analyse exactly what
# the silhouette layer drew.  This is how we look at PIXELS, not DOM
# counts, when debugging "I clicked one part and a bunch lit up".
_CAPTURE_DIR = Path(__file__).parent / "debug_captures"
_CAPTURE_SEQ = [0]
_CAPTURE_LOCK = threading.Lock()


def _log_disk_append(evt: dict) -> None:
    """Append one event as a JSON line to debug.log.  Rotates to
    debug.log.1 (single previous-generation file) once the current log
    exceeds 5 MB.  Failures are swallowed -- the in-memory ring buffer
    is the source of truth; disk is convenience."""
    try:
        import json as _json
        _LOG_DISK_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Rotate before append so the line lands in the fresh file.
        try:
            sz = _LOG_DISK_PATH.stat().st_size
        except OSError:
            sz = 0
        if sz >= _LOG_DISK_MAX_BYTES:
            try:
                if _LOG_DISK_PREV.exists():
                    _LOG_DISK_PREV.unlink()
                _LOG_DISK_PATH.rename(_LOG_DISK_PREV)
            except OSError:
                # If rename failed (Windows file lock?) just truncate
                # rather than letting the log grow unbounded.
                try:
                    _LOG_DISK_PATH.write_text("", encoding="utf-8")
                except OSError:
                    pass
        with open(_LOG_DISK_PATH, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(evt, default=str))
            fh.write("\n")
    except Exception:
        # Logging must never break the server.
        pass


def _log_event(**fields) -> None:
    """Push one structured event onto the rolling buffer + stdout."""
    with _LOG_LOCK:
        _LOG_SEQ[0] += 1
        evt = {"seq": _LOG_SEQ[0],
               "t": time.strftime("%H:%M:%S", time.gmtime()),
               **fields}
        _LOG_BUFFER.append(evt)
    # Mirror to disk (best-effort, never raises)
    _log_disk_append(evt)
    # Mirror to stdout so a running terminal shows it too
    parts = [f"{k}={v}" for k, v in fields.items()
             if k not in ("level",)]
    print(f"[{evt['t']}] {fields.get('level','info'):<5} {' '.join(parts)}",
          flush=True)

HERE = Path(__file__).parent
app = Flask(__name__)


@app.before_request
def _log_start():
    """Stamp the start time + log a request-start event.  Stored on
    request.environ so the corresponding after_request can compute
    duration."""
    request.environ["_t_start"] = time.time()
    # Don't log the boring stuff
    p = request.path
    if (p == "/" or p == "/api/healthz"
            or p == "/api/debug/log"
            or p.startswith("/static")):
        return
    # Try to extract a source_id from common patterns -- body for
    # /api/render etc, path for /api/sources/<sid>/...
    sid = None
    try:
        if request.method in ("POST", "PUT") and request.is_json:
            body = request.get_json(silent=True) or {}
            sid = body.get("file_id") or body.get("source_id")
        if not sid:
            parts = p.strip("/").split("/")
            # /api/sources/<sid>/... -> parts[2]
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "sources":
                sid = parts[2]
            elif len(parts) >= 3 and parts[0] == "api" and parts[1] == "glb":
                sid = parts[2]
    except Exception:
        pass
    _log_event(level="req", method=request.method, path=p, source_id=sid)


@app.after_request
def _cors_and_no_cache(resp):
    # CORS.  Local dev keeps the permissive "*" (viewer.html from file://
    # or same-origin).  A split deploy (front-end on Vercel, this API on
    # Render) sets IFU_ALLOWED_ORIGIN to the Vercel origin so the browser
    # permits cross-origin calls.  Methods MUST include PUT + DELETE --
    # the editor autosaves figures with PUT and deletes with DELETE, and
    # cross-origin preflight would otherwise reject them.
    resp.headers["Access-Control-Allow-Origin"] = \
        os.environ.get("IFU_ALLOWED_ORIGIN", "*")
    resp.headers["Access-Control-Allow-Methods"] = \
        "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Vary"] = "Origin"
    # Always serve a fresh viewer.html so a rebuild while the server is
    # running is picked up on the next reload.  Full no-cache combo so
    # Chrome's memory cache also revalidates (the index() handler sets
    # the same values, but they get overwritten when after_request runs
    # last -- keep both in sync).
    if request.path in ("/", "/viewer.html"):
        resp.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate"
        )
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"

    # Log the response (skip the noisy ones)
    p = request.path
    if not (p == "/" or p == "/api/healthz"
            or p == "/api/debug/log"
            or p.startswith("/static")):
        t0 = request.environ.get("_t_start") or time.time()
        ms = int((time.time() - t0) * 1000)
        level = "err" if resp.status_code >= 400 else "ok"
        _log_event(level=level, method=request.method, path=p,
                    status=resp.status_code, ms=ms)
    return resp


@app.route("/api/debug/client_log", methods=["POST"])
def debug_client_log():
    """Receive structured browser-side errors so we can debug without
    F12.  Body: {level, op, msg, source, line, ...}.  Anything the
    client posts lands in the same rolling log as server events so
    `/api/debug/log` returns them together."""
    body = request.get_json(silent=True) or {}
    fields = {"level": body.get("level") or "err",
              "op":    body.get("op")    or "client",
              "src":   "browser"}
    for k, v in body.items():
        if k in ("level", "op"):
            continue
        if v is None:
            continue
        fields[k] = v
    _log_event(**fields)
    return jsonify({"ok": True})


@app.route("/api/debug/log", methods=["GET"])
def debug_log():
    """Return the recent server log buffer.  Query ``?since=<seq>`` to
    only fetch entries newer than the given sequence number (cheap
    polling for the editor's debug overlay).  Query ``?disk=1`` to
    include the on-disk tail (events from before the last restart)
    parsed back into the same shape."""
    since = 0
    try:
        since = int(request.args.get("since") or 0)
    except (TypeError, ValueError):
        pass
    want_disk = request.args.get("disk") in ("1", "true", "yes")
    with _LOG_LOCK:
        if since:
            out = [e for e in _LOG_BUFFER if e["seq"] > since]
        else:
            # Default: tail the last ~80 entries
            out = list(_LOG_BUFFER)[-80:]
        latest_seq = _LOG_SEQ[0]

    disk_tail: list = []
    if want_disk:
        # Read up to the last 1 MB of the rotated log so we don't
        # answer slowly for users who ask for ?disk=1.  json-line
        # parse; skip lines that don't parse rather than 500.
        import json as _json
        try:
            sz = _LOG_DISK_PATH.stat().st_size
            with open(_LOG_DISK_PATH, "rb") as fh:
                if sz > 1024 * 1024:
                    fh.seek(-1024 * 1024, 2)
                    fh.readline()  # discard partial first line
                data = fh.read().decode("utf-8", errors="replace")
            for ln in data.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    disk_tail.append(_json.loads(ln))
                except _json.JSONDecodeError:
                    continue
        except OSError:
            pass

    payload = {"events": out, "latest_seq": latest_seq}
    if want_disk:
        payload["disk_tail"] = disk_tail
        payload["disk_path"] = str(_LOG_DISK_PATH)
    return jsonify(payload)


def _next_capture_seq() -> int:
    """Monotonic capture id.  Seeds from existing files on first call so
    ids keep climbing across restarts (don't clobber prior captures)."""
    with _CAPTURE_LOCK:
        if _CAPTURE_SEQ[0] == 0:
            mx = 0
            try:
                for p in _CAPTURE_DIR.glob("cap_*.json"):
                    try:
                        mx = max(mx, int(p.stem.split("_")[1]))
                    except (IndexError, ValueError):
                        pass
            except OSError:
                pass
            _CAPTURE_SEQ[0] = mx
        _CAPTURE_SEQ[0] += 1
        return _CAPTURE_SEQ[0]


@app.route("/api/debug/capture", methods=["POST"])
def debug_capture():
    """Persist a live interaction snapshot for closed-loop debugging.

    Body: {svg, tracker, selection, clicks, note, viewport, fid, vid,
           styles}.  ``svg`` is the live ``.svg-pane svg`` outerHTML
           (includes the layer-silhouette overlay exactly as drawn).
    Writes debug_captures/cap_<seq>.svg + cap_<seq>.json and returns the
    seq + paths so the harness can pick it up."""
    import json as _json
    body = request.get_json(silent=True) or {}
    seq = _next_capture_seq()
    _CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    svg = body.get("svg") or ""
    svg_path = _CAPTURE_DIR / f"cap_{seq:04d}.svg"
    json_path = _CAPTURE_DIR / f"cap_{seq:04d}.json"

    meta = {
        "seq":       seq,
        "t":         time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "note":      body.get("note") or "",
        "fid":       body.get("fid"),
        "vid":       body.get("vid"),
        "selection": body.get("selection") or [],
        "clicks":    body.get("clicks") or [],
        "tracker":   body.get("tracker") or [],
        "viewport":  body.get("viewport") or {},
        "styles":    body.get("styles") or {},
        "meshes3d":  body.get("meshes3d"),
        "svg_bytes": len(svg),
        "svg_file":  svg_path.name,
    }
    try:
        svg_path.write_text(svg, encoding="utf-8")
        json_path.write_text(_json.dumps(meta, indent=2, default=str),
                             encoding="utf-8")
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    _log_event(level="ok", op="debug.capture", seq=seq,
               sel=len(meta["selection"]), svg_kb=len(svg) // 1024,
               note=meta["note"][:60])
    return jsonify({"ok": True, "seq": seq,
                    "svg_path": str(svg_path),
                    "json_path": str(json_path)})


@app.route("/api/debug/captures", methods=["GET"])
def debug_captures_list():
    """List stored captures (newest first), summary only."""
    import json as _json
    out = []
    try:
        for p in sorted(_CAPTURE_DIR.glob("cap_*.json"), reverse=True):
            try:
                m = _json.loads(p.read_text(encoding="utf-8"))
                out.append({k: m.get(k) for k in
                            ("seq", "t", "note", "fid", "vid",
                             "selection", "svg_bytes", "svg_file")})
            except (OSError, _json.JSONDecodeError):
                continue
    except OSError:
        pass
    return jsonify({"captures": out, "dir": str(_CAPTURE_DIR)})


@app.route("/api/debug/captures/<int:seq>", methods=["GET"])
def debug_capture_get(seq):
    """Return one capture's full metadata (not the SVG body)."""
    import json as _json
    p = _CAPTURE_DIR / f"cap_{seq:04d}.json"
    if not p.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        return jsonify(_json.loads(p.read_text(encoding="utf-8")))
    except (OSError, _json.JSONDecodeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/<path:_p>", methods=["OPTIONS"])
def _options(_p):
    return ("", 204)

# Shape cache: file_id -> (TopoDS_Shape, hlr_kw)
_SHAPES: dict[str, tuple] = {}

# Render cache: maps (file_id, rounded view_dir, rounded focal, up_axis)
# to the SVG bytes for that exact render.  Exact HLR is the killer cost
# (~80s for Presto), so memoising lets the user revisit an angle for free.
#
# LRU semantics via OrderedDict: every read move_to_end()'s the key, so
# eviction targets the genuinely-coldest entry rather than the oldest
# *inserted* one.  Matters when the user toggles between a couple of
# camera angles -- old FIFO would evict the popular one first.
from collections import OrderedDict

_RENDER_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_RENDER_CACHE_MAX = 20    # rough cap on memory


def _cache_get(cache: "OrderedDict", key):
    """Read + mark recently-used for LRU eviction."""
    val = cache.get(key)
    if val is not None:
        cache.move_to_end(key)
    return val


def _cache_put(cache: "OrderedDict", key, value, max_size: int) -> None:
    """Insert + evict the least-recently-used entries until the cache
    is back within ``max_size``."""
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > max_size:
        # popitem(last=False) drops the oldest *touched* entry == LRU.
        cache.popitem(last=False)


# View-direction quantisation tolerance.  OrbitControls + mouse-pixel
# quantisation leaves view_dir floats drifting by a few 1e-4 between
# frames; rounding to 3 decimal places (~0.057deg) was tight enough that
# tiny drift crossed the cache-key boundary and we missed the cache on
# what the user perceived as "the same angle".  Bumped to 2 decimal
# places (~0.57deg) -- still finer than human-perceptible camera
# changes, but absorbs the drift.
_VD_PRECISION = 2
_FOCAL_PRECISION = 1


def _view_keys(view_dir, focal):
    """Single source of truth for the per-view cache keys used by
    _RENDER_CACHE, _SIL_CACHE, _FOOT_CACHE, _FOOT_RASTER_*.  Returns
    ``(vd_key, focal_key)`` as tuples of rounded floats.

    Centralising this means a precision change (see _VD_PRECISION)
    flows everywhere consistently.
    """
    vd_key = tuple(round(x, _VD_PRECISION) for x in view_dir)
    focal_key = tuple(round(x, _FOCAL_PRECISION) for x in focal)
    return vd_key, focal_key

# Per-part silhouette cache: (file_id, vd_key, focal_key, up_axis_key, idx)
# -> list of polylines in (u,v).  Each entry is one PolyAlgo on a single
# solid (cheap-ish: ~0.1-2s depending on part complexity), but if the
# user keeps the same angle and re-selects, the cache means instant.
_SIL_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_SIL_CACHE_MAX = 2000

# Visible-footprint cache: (file_id, vd_key, focal_key, up_axis_key, idx)
# -> list of closed polylines tracing the part's visible 2D footprint.
# Cost is dominated by the single rasterize pass (all parts at once);
# results are returned per requested idx so caching is per-idx-per-view.
_FOOT_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_FOOT_CACHE_MAX = 2000

# Raster handle cache: maps view_key -> (id_buf, px_per_mm, u_min, v_min).
# Lets /api/part_footprints serve assembly + group silhouettes on demand
# WITHOUT re-running the meshing + tri projection (the slow part).
# Memory cost is the id_buf (~9 MB for a 3000x3000 raster, ~2 MB at
# 1500x1500); cap is small.
_RASTER_HANDLE_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_RASTER_HANDLE_CACHE_MAX = 8

# Per-view assembly silhouette cache: view_key -> [polylines].  Cheap
# to compute once we have the raster handle; cached so the UI doesn't
# pay the contour trace per request.
_ASSEMBLY_SILHOUETTE_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_ASSEMBLY_SILHOUETTE_CACHE_MAX = 32

# GLB cache: maps (source_id, config_str) -> (b64, summary_dict).
# Reconfigure replaces _SHAPES[source_id] with the new geometry but
# the previous config's GLB bytes stay valid as bytes; flipping back
# to a config the user has touched in this session is instant rather
# than paying another ~5 s mesh + export.
_GLB_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_GLB_CACHE_MAX = 16  # small -- each entry is ~50-200 KB

# Latest configuration string applied to a source, indexed by source_id.
# Reconfigure updates this; /api/glb keys its lookup by it so renaming
# / reconfiguring picks the right cached blob (or recomputes if absent).
_SOURCE_CONFIG: dict[str, str] = {}

# Per-config shape cache: (source_id, config_str) -> (shape, hlr_kw).
# Lets reconfigure flip back to a previously-translated configuration
# without re-running the ~12-30 s Onshape translation + STEP load.
# Capped to bound memory (each entry holds a TopoDS_Compound which can
# be tens of MB for a big assembly).
_CFG_SHAPES: "OrderedDict[tuple, tuple]" = OrderedDict()
_CFG_SHAPES_MAX = 8
# Rasterize-once tracker: bool keyed by (file_id, vd_key, focal_key,
# up_axis_key) so we don't redo the assembly raster within the same view
# when extra parts are requested.
_FOOT_RASTER_DONE: dict[tuple, bool] = {}
# In-flight raster tracker so /api/render doesn't spawn duplicate
# background threads for the same view, and /api/part_footprints can
# tell the client "still computing" instead of starting another one.
_FOOT_RASTER_INFLIGHT: dict[tuple, bool] = {}
_FOOT_INFLIGHT_LOCK = threading.Lock()


def _kick_footprint_raster(file_id: str, shape, view_dir,
                             focal, up_axis_key,
                             vd_key, focal_key, mesh_defl: float) -> None:
    """Run the assembly footprint raster in a background thread so the
    cache is warm by the time the user clicks a part.  Idempotent --
    if a raster for the same view is already in flight or already done,
    this is a no-op."""
    view_key = (file_id, vd_key, focal_key, up_axis_key)
    with _FOOT_INFLIGHT_LOCK:
        if _FOOT_RASTER_DONE.get(view_key):
            return
        if _FOOT_RASTER_INFLIGHT.get(view_key):
            return
        _FOOT_RASTER_INFLIGHT[view_key] = True

    def _run():
        from t5_hlr_vector import (
            _extract_projected_triangles,
            _rasterise_visible_footprints,
            compute_assembly_silhouette_from_raster,
            split_solids,
        )
        t0 = time.time()
        # The cache-warming raster runs at a deliberately coarser
        # mesh_defl + lower resolution than the default
        # /api/part_footprints call.  Reason: the closed-loop outline
        # is for visualisation only -- a 1mm coarser mesh / 1500x
        # raster looks fine on screen but cuts the raster cost from
        # ~5 min to ~1 min for a 138-part assembly.  When a user
        # explicitly asks for a higher-fidelity outline later we can
        # re-raster at full quality on that single click.
        coarse_defl = max(1.0, mesh_defl * 1.5)
        _log_event(level="info", op="footprint.prefetch.start",
                    source_id=file_id, view=str(vd_key),
                    mesh_defl=round(coarse_defl, 2))
        try:
            # Phase 1 (OCCT-bound): mesh + project triangles.  Hold the
            # HLR lock only for this phase -- ~5-15 s for a 138-part
            # assembly -- so /api/render isn't blocked for the full
            # raster duration.
            t_extract0 = time.time()
            with _HLR_LOCK:
                all_indices = list(
                    range(len(list(split_solids(shape)))))
                tri_data = _extract_projected_triangles(
                    shape, view_dir, focal, coarse_defl)
            t_extract = time.time() - t_extract0

            # Phase 2 (pure numpy / cv2): rasterise + contour-trace.
            # No OCCT calls -> the lock is released, so an interactive
            # /api/render request landing now runs in parallel.
            t_raster0 = time.time()
            full = _rasterise_visible_footprints(
                tri_data, all_indices, resolution=1500)
            t_raster = time.time() - t_raster0

            # Pull the raster handle BEFORE writing into _FOOT_CACHE
            # (we strip it off so /api/part_footprints sees a clean
            # {idx: polylines} shape).
            raster_handle = full.pop(("__id_buf__",), None)
            if raster_handle is not None:
                _cache_put(_RASTER_HANDLE_CACHE, view_key, raster_handle,
                            _RASTER_HANDLE_CACHE_MAX)
                # Compute + cache the assembly silhouette so the
                # client can fetch it instantly without a contour
                # re-trace.
                try:
                    asm = compute_assembly_silhouette_from_raster(
                        raster_handle)
                    _cache_put(_ASSEMBLY_SILHOUETTE_CACHE, view_key, asm,
                                _ASSEMBLY_SILHOUETTE_CACHE_MAX)
                except Exception as exc:
                    _log_event(level="warn", op="footprint.assembly",
                                source_id=file_id,
                                error=f"{type(exc).__name__}: {exc}")

            for idx, polys in (full or {}).items():
                ck = view_key + (idx,)
                _cache_put(_FOOT_CACHE, ck, polys, _FOOT_CACHE_MAX)
            _FOOT_RASTER_DONE[view_key] = True
            _log_event(level="ok", op="footprint.prefetch.done",
                        source_id=file_id,
                        seconds=round(time.time() - t0, 2),
                        extract_sec=round(t_extract, 2),
                        raster_sec=round(t_raster, 2),
                        parts=len(all_indices))
        except Exception as exc:
            _log_event(level="err", op="footprint.prefetch",
                        source_id=file_id,
                        error=f"{type(exc).__name__}: {exc}")
        finally:
            with _FOOT_INFLIGHT_LOCK:
                _FOOT_RASTER_INFLIGHT.pop(view_key, None)

    t = threading.Thread(target=_run, daemon=True,
                          name=f"footprint-prefetch-{file_id}")
    t.start()


def _load_step_as_compound(step_path: Path):
    """Read a STEP and return a single OCCT shape that contains every
    top-level solid.

    cadquery's importStep returns a Workplane whose ``vals()`` lists
    every top-level item in the file.  When the STEP itself wraps its
    parts in a top-level COMPOUND (Onshape's assembly export, the
    hand-curated Presto STEP) ``vals()`` is length 1 and ``val()``
    already covers everything.  When the STEP lists each part as a
    sibling top-level entity (Onshape's part-studio export via
    /partstudios/.../translations), ``vals()`` has one entry per part
    and ``val()`` quietly drops everything except the first solid --
    which is why the user only saw 1 rivet.

    To handle both shapes uniformly we always build a compound here.
    """
    # Reject obviously broken files BEFORE letting cadquery raise an
    # opaque OCP exception 30 s later.  Truncated downloads from Onshape
    # have produced 0-byte / 100-byte STEPs that read as valid but
    # contain no solids -- catch them early.
    try:
        size = step_path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"STEP {step_path} unreadable: {exc}") from exc
    if size < 200:
        # A minimal STEP header alone is ~150 bytes; anything below that
        # cannot possibly carry geometry.
        raise RuntimeError(
            f"STEP {step_path} is too small ({size} bytes) -- "
            f"likely a truncated download"
        )

    imp = cq.importers.importStep(str(step_path))
    vals = imp.vals()
    if not vals:
        raise RuntimeError(f"STEP {step_path} contained no shapes")
    if len(vals) == 1:
        shape = vals[0].wrapped
    else:
        # Multi-solid file: assemble all of them under a single compound
        # so downstream code (split_solids, HLR, GLB) sees everything.
        from OCP.TopoDS import TopoDS_Compound
        from OCP.BRep import BRep_Builder
        comp = TopoDS_Compound()
        bb = BRep_Builder()
        bb.MakeCompound(comp)
        for v in vals:
            bb.Add(comp, v.wrapped)
        shape = comp

    # Final guard: walk the compound and confirm at least one solid
    # survived.  Some Onshape exports of empty part-studios produce a
    # syntactically valid STEP whose top level is an empty compound;
    # callers will silently return 0 polylines unless we fail here.
    solids = split_solids(shape)
    if not solids:
        raise RuntimeError(
            f"STEP {step_path} parsed but contained 0 solids -- "
            f"empty part studio or corrupt export?"
        )
    return shape


def _load_source_into_memory(*, file_id: str, step_path: Path,
                                hlr_kw: dict,
                                pre_rotate=None) -> bool:
    """Import a STEP into the shape cache.  Returns True on success.
    Used by boot() for static sources AND the Onshape-import worker
    for dynamic sources.  Skips silently if the STEP is missing."""
    if not step_path.exists():
        print(f"  skip {file_id}: {step_path} missing")
        return False
    print(f"  {file_id:<28s} ", end="", flush=True)
    t0 = time.time()
    try:
        shape = _load_step_as_compound(step_path)
        if pre_rotate is not None:
            axis, angle = pre_rotate
            shape = rotate_shape(shape, axis, angle)
        _SHAPES[file_id] = (shape, hlr_kw or {"mesh_defl": 1.5,
                                                 "sample_defl": 1.0})
        print(f"loaded in {time.time()-t0:.1f}s")
        return True
    except Exception as exc:
        print(f"FAILED: {exc}")
        return False


def _ensure_source_loaded(file_id: str) -> bool:
    """Lazily import a source's shape into _SHAPES on first use.  Returns
    True once the shape is available.

    Replaces the old boot-time preload: startup is now instant and the
    server only pays a STEP-import cost for sources actually opened
    (essential for a multi-user service -- you can't preload everyone's
    models, and most requests hit an already-cached source).

    THREAD-SAFETY: this performs OCCT work (STEP import + rotate), so it
    must run with the OCCT serialised.  All callers invoke it either
    inside an @_occt_serialised endpoint body or inside a `with
    _HLR_LOCK` block, so OCCT is never touched from two threads at once.
    """
    if file_id in _SHAPES:
        return True
    with _LOAD_LOCK:
        if file_id in _SHAPES:        # another thread loaded it meanwhile
            return True
        # Static (config) source?
        for entry in SOURCES:
            if entry[0] == file_id:
                fid, label, sp, hlr_kw, pre_rotate = entry[:5]
                return _load_source_into_memory(
                    file_id=fid, step_path=sp, hlr_kw=hlr_kw,
                    pre_rotate=pre_rotate)
        # Dynamic (Onshape-imported) source?
        try:
            for s in sources_store.list_dynamic():
                if s.get("id") == file_id:
                    pre_rot = s.get("pre_rotation")
                    return _load_source_into_memory(
                        file_id=s["id"], step_path=Path(s["step_path"]),
                        hlr_kw=s.get("hlr_kwargs") or {},
                        pre_rotate=pre_rot if pre_rot else None)
        except Exception as exc:
            print(f"  _ensure_source_loaded({file_id}) dynamic lookup: {exc}")
        return False


def boot():
    # Sources are NO LONGER preloaded -- they import lazily on first use
    # (see _ensure_source_loaded).  Startup is instant; the import cost is
    # paid per-source, once, when someone first opens it.
    n_static = len(SOURCES)
    n_dyn = 0
    try:
        n_dyn = len(sources_store.list_dynamic())
    except Exception:
        pass
    print(f"Sources load on demand: {n_static} static + {n_dyn} dynamic "
          f"known (none preloaded).")
    # Migrate any existing figures that don't have a View yet -- the
    # operation is idempotent, so this is safe on every boot.  Pre-Phase-3
    # figures spawn a 1:1 View with their stored camera.
    try:
        m = views_store.migrate_existing_figures()
        if m.get("created"):
            print(f"Views migration: created {m['created']} view(s); "
                  f"{m['skipped']} already linked; {m['orphan']} orphan.")
    except Exception as exc:
        print(f"Views migration skipped: {exc}")
    print()


@app.route("/api/baked_svg/<file_id>/<view_id>")
def baked_svg(file_id, view_id):
    """Serve a single baked SVG.  Previously these were inlined into
    viewer.html (3 sources x 3 views = ~26 MB).  Lazy-loading via this
    endpoint drops the bundled HTML to ~500 KB; the browser can cache
    each baked SVG independently and revalidate by mtime.

    File names follow the ``<file_id>__<view_id>.svg`` convention set
    by svg_bake.generate_svgs.  Path components are sanitised before
    construction to prevent traversal -- only [A-Za-z0-9_-] survives.
    """
    import re
    safe_fid = re.sub(r"[^A-Za-z0-9_-]+", "_", file_id)
    safe_vid = re.sub(r"[^A-Za-z0-9_-]+", "_", view_id)
    if not safe_fid or not safe_vid:
        return jsonify({"error": "bad ids"}), 400
    path = OUT / f"{safe_fid}__{safe_vid}.svg"
    if not path.exists() or not path.is_file():
        return jsonify({"error": "not found",
                        "wanted": str(path.name)}), 404
    resp = send_file(path, mimetype="image/svg+xml",
                      conditional=True)
    # Allow the browser to cache the SVG; the mtime-based ETag
    # send_file emits will invalidate on rebuild.
    resp.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    return resp


@app.route("/favicon.ico")
def favicon():
    """1x1 transparent PNG so the browser stops 404-ing for the favicon
    on every page load.  Tiny inline blob; no need for a static file."""
    import base64
    blob = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAS"
        "cMnwAAAABJRU5ErkJggg==")
    resp = Response(blob, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/")
def index():
    """Serve the bundled viewer.  After a rebuild Chrome's memory cache
    used to hold the old 26 MB parsed result even though our after-
    request set Cache-Control: no-store -- the user kept seeing stale
    JS until they hard-reloaded.  Belt-and-braces:

      Cache-Control: no-store, no-cache, must-revalidate
      Pragma: no-cache
      Expires: 0
      ETag: <build_id>

    The ETag is the on-disk mtime so a rebuild also invalidates 304s.
    Combined with no-store, refresh always re-fetches.
    """
    # viewer.html is app code committed to the repo — always lives at
    # <app_dir>/out/viewer.html.  IFU_DATA_DIR (/data on Render) holds
    # user data (figures/views/sources), not the built viewer.
    _app_viewer = HERE / "out" / "viewer.html"
    viewer = _app_viewer if _app_viewer.exists() else OUT / "viewer.html"
    if not viewer.exists():
        return Response(
            "IFU compute API — viewer.html not found. "
            "Run build_viewer.py to generate it. Health: /api/healthz",
            mimetype="text/plain")
    resp = send_file(viewer, conditional=False)
    try:
        mtime = viewer.stat().st_mtime_ns
        resp.set_etag(f"build-{mtime}")
    except OSError:
        pass
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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

    if not _ensure_source_loaded(file_id):
        _log_event(level="err", op="render", source_id=file_id,
                    reason="source not loaded",
                    known=",".join(_SHAPES.keys()))
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
            _log_event(level="err", op="render", source_id=file_id,
                        reason="eye==target")
            return jsonify({"error": "eye and target coincide"}), 400
        view_dir = (vd[0] / mag, vd[1] / mag, vd[2] / mag)
    elif isinstance(view_dir, list) and len(view_dir) == 3:
        view_dir = tuple(float(x) for x in view_dir)
        if isinstance(focal, list) and len(focal) == 3:
            focal = tuple(float(x) for x in focal)
        else:
            focal = (0.0, 0.0, 0.0)
    else:
        _log_event(level="err", op="render", source_id=file_id,
                    reason="no camera in body",
                    body_keys=",".join((body or {}).keys()))
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
    vd_key, focal_key = _view_keys(view_dir, focal)
    cache_key = (file_id, vd_key, focal_key, up_axis_key)
    cached = _cache_get(_RENDER_CACHE, cache_key)
    if cached is not None:
        print(f"  /api/render {file_id:<10s} dir={vd_key}{extra_rot_str}  "
              f"CACHE HIT  {len(cached)//1024}KB")
        _log_event(level="ok", op="render.cache_hit",
                    source_id=file_id, kb=len(cached)//1024)
        return Response(cached, mimetype="image/svg+xml", headers={
            "X-Render-Seconds": "0.0",
            "X-Render-Breakdown": "cache-hit",
        })
    _log_event(level="info", op="render.start", source_id=file_id,
                vd=f"({vd_key[0]},{vd_key[1]},{vd_key[2]})",
                focal=f"({focal_key[0]},{focal_key[1]},{focal_key[2]})",
                mesh_defl=hlr_kw.get("mesh_defl"))
    t_hlr0 = time.time()
    try:
        parts = run_hlr_per_solid(shape, view_dir, focal=focal, **hlr_kw)
    except Exception as exc:
        _log_event(level="err", op="render.hlr",
                    source_id=file_id,
                    error=f"{type(exc).__name__}: {exc}")
        return jsonify({"error": f"HLR failed: {type(exc).__name__}: {exc}"}), 500
    t_hlr = time.time() - t_hlr0
    # Count what came out -- if everything's zero the user sees a blank
    # SVG, which is the "I clicked Generate and nothing happened" symptom.
    # run_hlr_per_solid returns dicts with key "polys" (not "categories"
    # or "polylines"); each value is {category: [polyline, ...]} where a
    # polyline is a list of (x, y) tuples.
    n_polys = 0
    n_parts_with_geom = 0
    for pe in parts or []:
        cats = pe.get("polys") or pe.get("categories") or {}
        any_seg = False
        for _cat_name, polylines in cats.items():
            for poly in polylines or []:
                if poly and len(poly) >= 2:
                    n_polys += 1
                    any_seg = True
        if any_seg:
            n_parts_with_geom += 1
    _log_event(level="info" if n_polys else "warn",
                op="render.hlr_done",
                source_id=file_id, hlr_sec=round(t_hlr, 2),
                parts=len(parts or []),
                parts_with_geom=n_parts_with_geom,
                polylines=n_polys)

    # X-mirror is gone now.  build_projector was rewritten to build the
    # Ax2 with Z = +view_dir and X = up × view_dir, so OCCT's camera and
    # screen-X axis natively match three.js -- no post-process flip
    # needed.  Previously OCCT's camera was on the OPPOSITE side of the
    # model and the X-flip was a mask that broke for arbitrary view_dirs.
    t_mir = 0.0

    t_svg0 = time.time()
    # Persist the scratch SVG only when the request opts in via
    # ?save=1 (or IFU_PERSIST_LIVE_SVG=1) -- otherwise write to a
    # temp file and unlink after reading.  Used to leave one
    # _live_<sid>.svg per source on disk forever, which became
    # ~3 MB per source after a long session.
    persist_disk = (
        request.args.get("save") in ("1", "true", "yes")
        or os.environ.get("IFU_PERSIST_LIVE_SVG") in ("1", "true", "yes")
    )
    if persist_disk:
        out_path = OUT / f"_live_{file_id}.svg"
        write_svg_parts(parts, out_path, precision=1)
        svg = out_path.read_text(encoding="utf-8")
    else:
        import tempfile
        with tempfile.NamedTemporaryFile(
                "w", suffix=".svg", delete=False, dir=str(OUT),
                encoding="utf-8") as _tmp:
            tmp_path = Path(_tmp.name)
        try:
            write_svg_parts(parts, tmp_path, precision=1)
            svg = tmp_path.read_text(encoding="utf-8")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    t_svg = time.time() - t_svg0
    elapsed = t_hlr + t_mir + t_svg
    breakdown = f"hlr={t_hlr:.1f}s mirror={t_mir:.1f}s svg-write={t_svg:.1f}s"
    print(f"  /api/render {file_id:<10s} dir={tuple(round(x,3) for x in view_dir)}"
          f"{extra_rot_str}  total={elapsed:.1f}s ({breakdown})  "
          f"{len(svg)//1024}KB")
    _log_event(level="ok", op="render.done",
                source_id=file_id, total_sec=round(elapsed, 2),
                hlr_sec=round(t_hlr, 2), svg_sec=round(t_svg, 2),
                svg_kb=len(svg)//1024, polylines=n_polys)
    # Insert into render cache (LRU eviction)
    svg_bytes = svg.encode("utf-8")
    _cache_put(_RENDER_CACHE, cache_key, svg_bytes, _RENDER_CACHE_MAX)

    # Background: kick off the visible-footprint raster for this view
    # so by the time the user has finished scanning the SVG and clicks
    # a part, the closed-loop outline cache is already warm.  Without
    # this, the first click eats a ~46s wait for a 77-part assembly.
    # The thread takes _HLR_LOCK so it doesn't fight any concurrent
    # /api/render; the user's request has already returned by then.
    _kick_footprint_raster(
        file_id=file_id, shape=shape, view_dir=view_dir,
        focal=focal, up_axis_key=up_axis_key,
        vd_key=vd_key, focal_key=focal_key,
        mesh_defl=hlr_kw.get("mesh_defl", 0.8))

    return Response(svg_bytes, mimetype="image/svg+xml", headers={
        "X-Render-Seconds": f"{elapsed:.2f}",
        "X-Render-Breakdown": breakdown,
        "X-Render-Polylines": str(n_polys),
    })


# ----- Settings (F.1) ------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def settings_get():
    """Return the merged app-level settings document."""
    return jsonify(settings_store.load())


@app.route("/api/settings", methods=["PUT", "PATCH"])
def settings_update():
    """Merge the request body onto current settings and persist.

    PUT and PATCH are semantically identical here -- both partial.  We
    accept both verbs because PATCH is the strictly-correct REST verb
    but PUT-as-partial is widespread in single-tenant tools.
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be an object"}), 400
    return jsonify(settings_store.save(body))


@app.route("/api/settings/reset", methods=["POST"])
def settings_reset():
    """Replace settings.json with DEFAULT_SETTINGS.  Returns fresh dict."""
    return jsonify(settings_store.reset())


# ----- Sources + Revisions (Phase C) ---------------------------------

@app.route("/api/sources", methods=["GET"])
def sources_list():
    """List configured sources -- static (baked) and dynamic (imported
    from Onshape).  Tells the UI which sources have Onshape backing (and
    so support refresh / revision tracking) and which are loaded in
    memory (i.e. /api/render will work without a server restart)."""
    out = []
    for s in sources_store.all_sources():
        out.append({
            "id": s["id"],
            "label": s["label"],
            "step_path": s["step_path"],
            "onshape_ids": s.get("onshape_ids"),
            "origin": s.get("origin", "static"),
            "loaded": s["id"] in _SHAPES,
        })
    return jsonify({"sources": out})


# ----- Onshape import (Phase G.2) ------------------------------------

@app.route("/api/onshape/probe", methods=["POST"])
def onshape_probe():
    """Lightweight URL inspection -- no translation, no STEP download.

    Used by the new-project wizard to pre-populate the project name
    from the Onshape document title as soon as the user pastes the URL.

    Body: ``{url: "<doc URL>"}``.  Returns ``{document_name,
    element_name, element_type, onshape_ids}`` on success, or
    ``{error: "..."}`` with an appropriate HTTP code on failure.
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        ids = onshape_fetch.parse_onshape_url(url)
    except onshape_fetch.OnshapeURLError as exc:
        return jsonify({"error": str(exc)}), 400
    did, wv, wvid, eid = ids["did"], ids["wv"], ids["wvid"], ids["eid"]
    if not eid:
        return jsonify({
            "error": "URL must include /e/<eid> element segment"
        }), 400
    try:
        doc = onshape_fetch.get_document_info(did)
        elem = onshape_fetch.get_element_info(did, wv, wvid, eid)
    except RuntimeError as exc:
        # Client missing creds, etc.
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({
            "error": f"probe failed: {type(exc).__name__}: {exc}"
        }), 502
    return jsonify({
        "document_name": doc.get("name"),
        "element_name": elem.get("name"),
        "element_type": elem.get("type"),
        "onshape_ids": {"did": did,
                         "wid": wvid if wv == "w" else None,
                         "vid": wvid if wv == "v" else None,
                         "mid": wvid if wv == "m" else None,
                         "eid": eid, "wv": wv},
    })


@app.route("/api/onshape/import", methods=["POST"])
def onshape_import_start():
    """Kick off an Onshape STEP import.  Body: ``{url: "<doc URL>"}``.
    Returns ``{job_id, status, ...}`` immediately; the actual work
    runs on a daemon thread.  Poll /api/onshape/import/<job_id>."""
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        # Eager URL-parse so we can reject obviously bad input
        # before spawning a thread
        onshape_fetch.parse_onshape_url(url)
    except onshape_fetch.OnshapeURLError as exc:
        return jsonify({"error": str(exc)}), 400
    job = onshape_fetch.start_import(url)
    return jsonify(job), 202


@app.route("/api/onshape/import/<job_id>", methods=["DELETE"])
def onshape_import_cancel(job_id):
    """Request cancellation of an in-flight import job.  The worker
    checks its cancel flag at each progress checkpoint and bails out
    with status='cancelled'.  Returns the (possibly-updated) job dict,
    or 404 if the job id is unknown."""
    job = onshape_fetch.cancel_import(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    _log_event(level="info", op="onshape.import.cancel", job_id=job_id,
                status=job.get("status"))
    return jsonify(job)


@app.route("/api/onshape/import/<job_id>", methods=["GET"])
def onshape_import_status(job_id):
    """Poll an in-flight import.  Returns the job dict, or 404 if no
    such job is being tracked (server restart -> jobs are wiped)."""
    job = onshape_fetch.get_job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    # Once a job reports "ready" but the source isn't in _SHAPES yet,
    # load it now -- this is the moment between download completing
    # and the next render call.  Done synchronously so the UI sees a
    # consistent 'loaded' flag.
    if job.get("status") == "ready":
        sid = job.get("source_id")
        if sid and sid not in _SHAPES:
            existing = sources_store.find(sid)
            if existing is None:
                # First time -- register the dynamic source
                sources_store.register(
                    source_id=sid,
                    label=job.get("document_name") or sid,
                    step_path=job["step_path"],
                    onshape_ids=job.get("onshape_ids"),
                    imported_from=job.get("url"))
                existing = sources_store.find(sid)
            # Load into the in-memory cache so /api/render works
            _load_source_into_memory(
                file_id=sid, step_path=Path(job["step_path"]),
                hlr_kw=(existing or {}).get("hlr_kwargs") or {},
                pre_rotate=None)
    return jsonify(job)


@app.route("/api/glb/<source_id>", methods=["GET"])
@_occt_serialised
def glb_for_source(source_id):
    """Generate a GLB for a source on demand.

    The baked GLB_B64 catalogue only knows about static sources -- after
    an Onshape import lands at runtime, the editor needs to be able to
    pull a fresh mesh.  We mesh against the same in-memory shape used
    by /api/render with the source's hlr_kwargs mesh_defl, base64-encode
    it, and return ``{b64, parts, tris, kb}``.
    """
    if not _ensure_source_loaded(source_id):
        return jsonify({"error": f"unknown or unloaded source: {source_id!r}",
                         "known": list(_SHAPES.keys())}), 404
    shape, hlr_kw = _SHAPES[source_id]
    mesh_defl = (hlr_kw or {}).get("mesh_defl", 1.5)

    cache_key = (source_id, _SOURCE_CONFIG.get(source_id, ""))
    cached = _cache_get(_GLB_CACHE, cache_key)
    if cached is not None:
        b64, summary = cached
        return jsonify({
            "source_id": source_id,
            "b64": b64,
            "from_cache": True,
            **(summary or {}),
        })

    try:
        from ifu.glb import export_glb_b64
        b64, summary = export_glb_b64(shape, mesh_defl)
    except Exception as exc:
        return jsonify({"error":
            f"GLB export failed: {type(exc).__name__}: {exc}"}), 500
    if not b64:
        return jsonify({"error": "no meshable solids"}), 422
    _cache_put(_GLB_CACHE, cache_key, (b64, summary), _GLB_CACHE_MAX)
    return jsonify({
        "source_id": source_id,
        "b64": b64,
        **(summary or {}),
    })


@app.route("/api/sources/<source_id>/reconfigure", methods=["POST"])
@_occt_serialised
def source_reconfigure(source_id):
    """Re-translate a dynamic source with a new configuration and swap
    the in-memory shape.

    Body: ``{configuration: {parameter_id: value, ...}}``.

    On success returns ``{ok: true, source_id, configuration,
    step_path}`` and evicts every cache keyed by source_id so the next
    /api/render / /api/glb call recomputes.
    """
    src = sources_store.find(source_id)
    if src is None:
        return jsonify({"error": "unknown source"}), 404
    ids = src.get("onshape_ids") or {}
    did, eid = ids.get("did"), ids.get("eid")
    wv = ids.get("wv") or "w"
    wvid = ids.get("wid") or ids.get("vid") or ids.get("mid")
    if not (did and wvid and eid):
        return jsonify({
            "error": "source has no onshape_ids -- can't reconfigure"
        }), 400

    body = request.get_json(silent=True) or {}
    cfg_values = body.get("configuration") or {}
    cfg_str = onshape_fetch.encode_configuration(cfg_values)
    _log_event(level="info", op="reconfigure.start",
                source_id=source_id, cfg=cfg_str or "(default)")

    # Fast path: have we already translated + loaded this exact
    # (source_id, config_str) combination in this server session?  If
    # so, swap the in-memory shape WITHOUT another Onshape round trip.
    cfg_key = (source_id, cfg_str)
    cached_shape = _cache_get(_CFG_SHAPES, cfg_key)
    if cached_shape is not None:
        _SHAPES[source_id] = cached_shape
        _SOURCE_CONFIG[source_id] = cfg_str
        # Evict per-source caches keyed by current view, EXCEPT _GLB_CACHE
        # which is keyed by (source_id, config_str) and stays warm.
        for cache in (_RENDER_CACHE, _SIL_CACHE, _FOOT_CACHE,
                       _FOOT_RASTER_DONE,
                       _RASTER_HANDLE_CACHE,
                       _ASSEMBLY_SILHOUETTE_CACHE):
            for key in list(cache.keys()):
                if isinstance(key, tuple) and key and key[0] == source_id:
                    del cache[key]
        _log_event(level="ok", op="reconfigure.cache_hit",
                    source_id=source_id, cfg=cfg_str or "(default)")
        return jsonify({
            "ok": True,
            "source_id": source_id,
            "configuration": cfg_values,
            "step_path": src.get("step_path"),
            "from_cache": True,
        })

    # Need element type for the translation endpoint family.  Probe it.
    try:
        elem = onshape_fetch.get_element_info(did, wv, wvid, eid)
    except Exception as exc:
        _log_event(level="err", op="reconfigure.elem",
                    source_id=source_id,
                    error=f"{type(exc).__name__}: {exc}")
        return jsonify({
            "error": f"element probe failed: {exc}"
        }), 502
    element_type = elem.get("type") or "ASSEMBLY"

    dest = Path(src["step_path"])
    try:
        result = onshape_fetch.translate_and_download(
            did=did, wv=wv, wvid=wvid, eid=eid,
            element_type=element_type,
            configuration=cfg_str,
            dest=dest)
    except Exception as exc:
        _log_event(level="err", op="reconfigure.translate",
                    source_id=source_id,
                    error=f"{type(exc).__name__}: {exc}")
        return jsonify({
            "error": f"translation failed: {exc}"
        }), 502

    # Swap the in-memory shape.  Errors here are recoverable (we still
    # have the new STEP on disk), but we need to surface them.
    try:
        ok = _load_source_into_memory(
            file_id=source_id, step_path=dest,
            hlr_kw=src.get("hlr_kwargs") or {},
            pre_rotate=None)
        if not ok:
            raise RuntimeError("failed to reload STEP into shape cache")
    except Exception as exc:
        _log_event(level="err", op="reconfigure.reload",
                    source_id=source_id,
                    error=f"{type(exc).__name__}: {exc}")
        return jsonify({
            "error": f"reload failed: {exc}",
            "step_path": str(dest),
        }), 500

    # Cache the freshly-translated shape against (source_id, config_str)
    # so future flips back to this configuration skip the translation.
    _cache_put(_CFG_SHAPES, cfg_key, _SHAPES[source_id], _CFG_SHAPES_MAX)

    # Evict caches keyed by this source so the next render recomputes.
    # NB: the GLB cache is keyed by (source_id, config_str) so we
    # deliberately KEEP its entries -- flipping back to a prior
    # configuration in this session must hit the cached blob, not
    # re-mesh + export.
    for cache in (_RENDER_CACHE, _SIL_CACHE, _FOOT_CACHE,
                   _FOOT_RASTER_DONE,
                   _RASTER_HANDLE_CACHE,
                   _ASSEMBLY_SILHOUETTE_CACHE):
        for key in list(cache.keys()):
            if isinstance(key, tuple) and key and key[0] == source_id:
                del cache[key]
    # Mark the source's current configuration so the next /api/glb
    # call cache-hits on this config rather than the previous one.
    _SOURCE_CONFIG[source_id] = cfg_str

    # Update the dynamic-source record so the configuration persists
    # across a server restart.
    sources_store.register(
        source_id=source_id,
        label=src.get("label") or source_id,
        step_path=str(dest),
        onshape_ids=src.get("onshape_ids"),
        hlr_kwargs=src.get("hlr_kwargs"),
        imported_from=src.get("imported_from"))

    _log_event(level="ok", op="reconfigure.done",
                source_id=source_id, cfg=cfg_str or "(default)",
                step_kb=dest.stat().st_size // 1024)
    return jsonify({
        "ok": True,
        "source_id": source_id,
        "configuration": cfg_values,
        "step_path": str(dest),
        **(result or {}),
    })


@app.route("/api/sources/<source_id>", methods=["DELETE"])
def source_delete(source_id):
    """Delete a dynamic (Onshape-imported) source.  Static demo sources
    (from ifu.config.SOURCES) cannot be deleted -- returns 400.  Also
    removes the STEP file from disk, evicts every cache keyed by the
    source, and drops the in-memory shape.

    Use case: Settings -> Imported sources -> Delete button.
    """
    src = sources_store.find(source_id)
    if src is None:
        return jsonify({"error": "unknown source"}), 404
    if (src.get("origin") or "static") != "dynamic":
        return jsonify({
            "error": "only dynamic (Onshape-imported) sources can be "
                     "deleted; static demo sources are part of the build"
        }), 400

    # Drop the on-disk STEP (best-effort).
    try:
        step_path = src.get("step_path")
        if step_path:
            p = Path(step_path)
            if p.exists() and p.is_file():
                p.unlink()
    except OSError as exc:
        _log_event(level="warn", op="source.delete.unlink",
                    source_id=source_id,
                    error=f"{type(exc).__name__}: {exc}")

    # Evict every cache keyed by this source.
    for cache in (_RENDER_CACHE, _SIL_CACHE, _FOOT_CACHE,
                   _FOOT_RASTER_DONE, _GLB_CACHE, _CFG_SHAPES,
                   _RASTER_HANDLE_CACHE, _ASSEMBLY_SILHOUETTE_CACHE):
        for key in list(cache.keys()):
            if isinstance(key, tuple) and key and key[0] == source_id:
                del cache[key]

    _SHAPES.pop(source_id, None)
    _SOURCE_CONFIG.pop(source_id, None)

    removed = sources_store.unregister(source_id)
    _log_event(level="ok", op="source.delete",
                source_id=source_id, removed=removed)
    return jsonify({"ok": True, "source_id": source_id,
                    "removed": removed})


@app.route("/api/sources/<source_id>/configuration", methods=["GET"])
def source_configuration(source_id):
    """List the Onshape configuration parameters for a source.  Only
    meaningful for sources with onshape_ids; returns
    ``{has_config: False, parameters: []}`` otherwise.
    """
    src = sources_store.find(source_id)
    if src is None:
        return jsonify({"error": "unknown source"}), 404
    ids = src.get("onshape_ids") or {}
    did, eid = ids.get("did"), ids.get("eid")
    wv = ids.get("wv") or "w"
    wvid = ids.get("wid") or ids.get("vid") or ids.get("mid")
    if not (did and wvid and eid):
        return jsonify({"has_config": False, "parameters": []})
    try:
        return jsonify(onshape_fetch.get_element_configuration(
            did, wv, wvid, eid))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error":
            f"configuration fetch failed: {type(exc).__name__}: {exc}"
        }), 502


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
    primary_source_id = body.get("primary_source_id")
    onshape_ids = body.get("onshape_ids")
    proj = projects_store.new_project(
        name=name, description=description,
        primary_source_id=primary_source_id,
        onshape_ids=onshape_ids)
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
                       "layers_on", "detail", "annotations", "notes",
                       "configuration")}
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


# ---- Views (Phase 3) -------------------------------------------------

@app.route("/api/projects/<proj_id>/views", methods=["GET"])
def views_list_in_project(proj_id):
    """All views belonging to a project, newest-first.  Includes a
    resolved ``figures`` list so the UI can render counts + thumbnails
    without round-tripping each figure."""
    proj = projects_store.load(proj_id)
    if proj is None:
        return jsonify({"error": "project not found"}), 404
    views = views_store.views_in_project(proj_id)
    # Include figure count so the workspace card can show "3 figures"
    for v in views:
        v["figure_count"] = len(v.get("figure_ids") or [])
    return jsonify({"project_id": proj_id, "views": views})


@app.route("/api/views", methods=["GET"])
def views_list_all():
    """Bulk endpoint: every view, optionally grouped by project_id.

    HomeScreen previously called ``/api/projects/<pid>/views`` once per
    project; with 50 projects + 200 views that meant 50 disk-walks of
    the same 200 view files (~1.4 s on Windows).  This endpoint does
    one disk walk and groups in-memory, so the home page hot path
    drops from O(N x M) JSON reads to O(M).

    Query ``?group_by_project=1`` returns
    ``{by_project: {<pid>: [view, ...]}}``; otherwise ``{views: [...]}``.
    """
    group = request.args.get("group_by_project") in ("1", "true", "yes")
    all_views = views_store.list_all()
    for v in all_views:
        v["figure_count"] = len(v.get("figure_ids") or [])
    if group:
        by_project: dict[str, list] = {}
        for v in all_views:
            by_project.setdefault(v.get("project_id") or "", []).append(v)
        return jsonify({"by_project": by_project,
                        "count": len(all_views)})
    return jsonify({"views": all_views, "count": len(all_views)})


@app.route("/api/views", methods=["POST"])
def views_create():
    body = request.get_json(silent=True) or {}
    pid = body.get("project_id")
    sid = body.get("source_id")
    name = body.get("name") or "Untitled view"
    camera = body.get("camera")
    configuration = body.get("configuration")
    if not pid:
        return jsonify({"error": "project_id required"}), 400
    if projects_store.load(pid) is None:
        return jsonify({"error": "project not found"}), 404
    if not sid:
        # If the project has a primary source, default to it
        proj = projects_store.load(pid) or {}
        sid = proj.get("primary_source_id")
    if not sid:
        return jsonify({"error":
            "source_id required (project has no primary_source_id)"}), 400
    v = views_store.new_view(project_id=pid, source_id=sid,
                              name=name, camera=camera,
                              configuration=configuration)
    views_store.save(v)
    return jsonify(v), 201


@app.route("/api/views/<view_id>", methods=["GET"])
def views_get(view_id):
    v = views_store.load(view_id)
    if v is None:
        return jsonify({"error": "view not found"}), 404
    return jsonify(v)


@app.route("/api/views/<view_id>", methods=["PUT"])
def views_update(view_id):
    body = request.get_json(silent=True) or {}
    existing = views_store.load(view_id)
    if existing is None:
        return jsonify({"error": "view not found"}), 404
    body["id"] = existing["id"]
    body["created_at"] = existing.get("created_at", views_store._now_iso())
    views_store.save(body)
    return jsonify(body)


@app.route("/api/views/<view_id>", methods=["DELETE"])
def views_delete(view_id):
    cascade = request.args.get("cascade") in ("1", "true", "yes")
    ok = views_store.delete(view_id, cascade=cascade)
    if not ok:
        return jsonify({"error": "view not found"}), 404
    return ("", 204)


@app.route("/api/views/<view_id>/figures", methods=["GET"])
def views_figures(view_id):
    v = views_store.load(view_id)
    if v is None:
        return jsonify({"error": "view not found"}), 404
    figs = views_store.figures_in_view(view_id)
    return jsonify({"view_id": view_id, "figures": figs})


@app.route("/api/views/<view_id>/figures/<fig_id>", methods=["POST"])
def views_attach_figure(view_id, fig_id):
    ok = views_store.attach_figure(view_id, fig_id)
    if not ok:
        return jsonify({"error": "view or figure not found"}), 404
    return jsonify(views_store.load(view_id))


@app.route("/api/views/<view_id>/figures/<fig_id>", methods=["DELETE"])
def views_detach_figure(view_id, fig_id):
    ok = views_store.detach_figure(view_id, fig_id)
    if not ok:
        return jsonify({"error": "view or figure not found"}), 404
    return jsonify(views_store.load(view_id))


@app.route("/api/views/<view_id>/thumbnail", methods=["GET"])
def views_thumbnail_get(view_id):
    p = views_store.view_thumbnail_path(view_id)
    if not p.exists():
        return ("", 404)
    return send_file(p, mimetype="image/png", max_age=0)


@app.route("/api/views/<view_id>/thumbnail", methods=["PUT", "POST"])
def views_thumbnail_put(view_id):
    v = views_store.load(view_id)
    if v is None:
        return jsonify({"error": "view not found"}), 404
    body = request.get_json(silent=True) or {}
    durl = body.get("data_url") or ""
    if not isinstance(durl, str) or not durl.startswith("data:image/"):
        return jsonify({"error": "data_url must start with 'data:image/'"}), 400
    try:
        header, b64 = durl.split(",", 1)
    except ValueError:
        return jsonify({"error": "malformed data URL"}), 400
    import base64
    try:
        png = base64.b64decode(b64, validate=True)
    except Exception:
        return jsonify({"error": "data URL payload is not valid base64"}), 400
    if len(png) < 16:
        return jsonify({"error": "thumbnail payload is empty"}), 400
    if len(png) > 200 * 1024:
        return jsonify({"error":
            f"thumbnail too large ({len(png)//1024}KB); cap is 200KB"}), 413
    p = views_store.view_thumbnail_path(view_id)
    try:
        p.write_bytes(png)
    except Exception as exc:
        return jsonify({"error": f"write failed: {exc}"}), 500
    return jsonify({"ok": True, "bytes": len(png)})


@app.route("/api/views/migrate", methods=["POST"])
def views_migrate():
    """One-shot helper: walk every Figure with a project_id and spawn
    a 1:1 View for any figure not already pointing at one.  Idempotent
    -- re-running it is safe."""
    counts = views_store.migrate_existing_figures()
    return jsonify(counts)


@app.route("/api/figures/<fig_id>/thumbnail", methods=["GET"])
def figures_thumbnail_get(fig_id):
    """Serve the PNG thumbnail captured at save-time.  404 when the
    figure has never been saved with one (older figures, or capture
    failed client-side)."""
    p = figures_store.figure_thumbnail_path(fig_id)
    if not p.exists():
        return ("", 404)
    return send_file(p, mimetype="image/png", max_age=0)


@app.route("/api/figures/<fig_id>/thumbnail", methods=["PUT", "POST"])
def figures_thumbnail_put(fig_id):
    """Store a PNG thumbnail for this figure.  Body: ``{data_url:
    "data:image/png;base64,..."}`` -- the data URL produced by
    canvas.toDataURL on the client.

    Caps the stored thumbnail at 200 KB to keep the figures folder
    light; larger blobs are rejected so a misbehaving client can't
    fill the disk.
    """
    fig = figures_store.load(fig_id)
    if fig is None:
        return jsonify({"error": "figure not found"}), 404
    body = request.get_json(silent=True) or {}
    durl = body.get("data_url") or ""
    if not isinstance(durl, str) or not durl.startswith("data:image/"):
        return jsonify({"error": "data_url must start with 'data:image/'"}), 400
    try:
        header, b64 = durl.split(",", 1)
    except ValueError:
        return jsonify({"error": "malformed data URL"}), 400
    import base64
    try:
        png = base64.b64decode(b64, validate=True)
    except Exception:
        return jsonify({"error": "data URL payload is not valid base64"}), 400
    if len(png) < 16:
        return jsonify({"error": "thumbnail payload is empty"}), 400
    if len(png) > 200 * 1024:
        return jsonify({"error":
            f"thumbnail too large ({len(png)//1024}KB); cap is 200KB"}), 413
    p = figures_store.figure_thumbnail_path(fig_id)
    try:
        p.write_bytes(png)
    except Exception as exc:
        return jsonify({"error": f"write failed: {exc}"}), 500
    return jsonify({"ok": True, "bytes": len(png),
                    "url": f"/api/figures/{fig_id}/thumbnail"})


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

    if not _ensure_source_loaded(file_id):
        return jsonify({"error": f"unknown or unloaded source: {file_id!r}",
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

    # Region renders are not persisted by default -- they're a UI helper.
    # See /api/render's notes on IFU_PERSIST_LIVE_SVG.
    persist_disk = (
        request.args.get("save") in ("1", "true", "yes")
        or os.environ.get("IFU_PERSIST_LIVE_SVG") in ("1", "true", "yes")
    )
    if persist_disk:
        out_path = OUT / f"_region_{file_id}.svg"
        write_svg_parts(parts, out_path, precision=1)
        svg_bytes = out_path.read_bytes()
    else:
        import tempfile
        with tempfile.NamedTemporaryFile(
                "wb", suffix=".svg", delete=False, dir=str(OUT)) as _tmp:
            tmp_path = Path(_tmp.name)
        try:
            write_svg_parts(parts, tmp_path, precision=1)
            svg_bytes = tmp_path.read_bytes()
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    print(f"  /api/render_region {file_id:<10s} bbox={bbox_uv} "
          f"defl=({mesh_defl},{sample_defl}) parts={len(parts)} "
          f"hlr={t_hlr:.2f}s size={len(svg_bytes)//1024}KB")
    return Response(svg_bytes, mimetype="image/svg+xml", headers={
        "X-Region-Parts": str(len(parts)),
        "X-Region-Seconds": f"{t_hlr:.2f}",
    })


@app.route("/api/part_footprints", methods=["POST"])
def part_footprints():
    """Visible-footprint boundaries per part.

    For each part_idx in the request, returns the closed polyline(s)
    outlining the part's actually-visible 2D region in the current view
    (occluder cuts drawn along the occluder's boundary).  Same camera
    grammar as /api/render.

    NB: this endpoint is intentionally NOT @_occt_serialised.  The body
    holds _HLR_LOCK in a `with` block only for the OCCT-bound extract
    phase (~5-15 s); the longer numpy/cv2 rasterise runs lock-free.
    Wrapping in @_occt_serialised would re-acquire the same
    non-reentrant lock and self-deadlock the calling thread instantly.

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

    if not _ensure_source_loaded(file_id):
        return jsonify({"error": f"unknown or unloaded source: {file_id!r}",
                        "known": list(_SHAPES.keys())}), 400
    # Empty part_indices is allowed when the caller only wants the
    # assembly silhouette or per-group union outlines (no per-part
    # outlines).  Reject only if ALL three are missing.
    groups_body = body.get("groups") or {}
    want_assembly_body = bool(body.get("want_assembly"))
    if not isinstance(part_indices, list):
        return jsonify({"error": "part_indices must be a list"}), 400
    if not part_indices and not groups_body and not want_assembly_body:
        return jsonify({"error":
            "supply part_indices, groups, or want_assembly"}), 400
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

    vd_key, focal_key = _view_keys(view_dir, focal)
    view_key = (file_id, vd_key, focal_key, up_axis_key)

    out_polys: dict[int, list] = {}
    misses: list[int] = []
    for idx in part_indices:
        ck = view_key + (idx,)
        cached = _cache_get(_FOOT_CACHE, ck)
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
            from t5_hlr_vector import (
                _extract_projected_triangles,
                _rasterise_visible_footprints,
                compute_assembly_silhouette_from_raster,
            )
            # Phase 1 (OCCT-bound): hold the lock for mesh + project only.
            with _HLR_LOCK:
                tri_data = _extract_projected_triangles(
                    shape, view_dir, focal,
                    hlr_kw.get("mesh_defl", 0.8))
            # Phase 2 (numpy/cv2 only): rasterise without the lock so
            # /api/render can run in parallel.
            full = _rasterise_visible_footprints(
                tri_data, all_indices, resolution=3000)
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"error":
                f"footprint failed: {type(exc).__name__}: {exc}"}), 500
        t_raster = time.time() - t0
        # Cache the raster handle so /api/part_footprints can also
        # serve assembly + group silhouettes without re-rasterising.
        raster_handle = full.pop(("__id_buf__",), None)
        if raster_handle is not None:
            _cache_put(_RASTER_HANDLE_CACHE, view_key, raster_handle,
                        _RASTER_HANDLE_CACHE_MAX)
            try:
                asm = compute_assembly_silhouette_from_raster(raster_handle)
                _cache_put(_ASSEMBLY_SILHOUETTE_CACHE, view_key, asm,
                            _ASSEMBLY_SILHOUETTE_CACHE_MAX)
            except Exception as exc:
                _log_event(level="warn", op="footprint.assembly",
                            error=f"{type(exc).__name__}: {exc}")
        for idx, polys in full.items():
            ck = view_key + (idx,)
            _cache_put(_FOOT_CACHE, ck, polys, _FOOT_CACHE_MAX)
        _FOOT_RASTER_DONE[view_key] = True
        for idx in misses:
            out_polys[idx] = full.get(idx, [])

    # Optional: client may ask for the assembly silhouette and/or
    # per-group union silhouettes alongside the per-part outlines.
    want_assembly = bool(body.get("want_assembly"))
    groups = body.get("groups") or {}
    assembly_polys = None
    group_polys: dict[str, list] = {}
    if want_assembly or groups:
        from t5_hlr_vector import (
            compute_assembly_silhouette_from_raster,
            compute_group_silhouettes_from_raster,
        )
        if want_assembly:
            cached_asm = _cache_get(_ASSEMBLY_SILHOUETTE_CACHE, view_key)
            if cached_asm is not None:
                assembly_polys = cached_asm
            else:
                raster_handle = _cache_get(_RASTER_HANDLE_CACHE, view_key)
                if raster_handle is not None:
                    try:
                        assembly_polys = (
                            compute_assembly_silhouette_from_raster(
                                raster_handle))
                        _cache_put(_ASSEMBLY_SILHOUETTE_CACHE,
                                    view_key, assembly_polys,
                                    _ASSEMBLY_SILHOUETTE_CACHE_MAX)
                    except Exception as exc:
                        _log_event(level="warn", op="footprint.assembly",
                                    error=f"{type(exc).__name__}: {exc}")
        if groups:
            raster_handle = _cache_get(_RASTER_HANDLE_CACHE, view_key)
            if raster_handle is not None:
                try:
                    # Sanitise: groups must be {str_key: [int_idx, ...]}
                    clean_groups = {}
                    for k, idxs in groups.items():
                        clean_groups[str(k)] = [
                            int(i) for i in (idxs or [])]
                    group_polys = (
                        compute_group_silhouettes_from_raster(
                            raster_handle, clean_groups))
                except Exception as exc:
                    _log_event(level="warn", op="footprint.groups",
                                error=f"{type(exc).__name__}: {exc}")

    raster_inflight = bool(_FOOT_RASTER_INFLIGHT.get(view_key))
    raster_done = bool(_FOOT_RASTER_DONE.get(view_key))
    payload = {
        "part_indices": part_indices,
        "polylines": {str(i): out_polys.get(i, []) for i in part_indices},
        "stats": {
            "hits": len(part_indices) - len(misses),
            "misses": len(misses),
            "raster_seconds": round(t_raster, 3),
            "raster_inflight": raster_inflight,
            "raster_done": raster_done,
        },
    }
    if assembly_polys is not None:
        payload["assembly"] = assembly_polys
    if group_polys:
        payload["groups"] = group_polys
    print(f"  /api/part_footprints {file_id:<10s} "
          f"parts={part_indices[:8]}{'...' if len(part_indices)>8 else ''} "
          f"hits={len(part_indices)-len(misses)} misses={len(misses)} "
          f"raster={t_raster:.2f}s")
    # Surface a breakdown in the debug log so the user can see, per
    # selection: how many parts came back with closed loops, how many
    # were empty (= fully occluded or off-screen), and the total
    # number of polylines drawn.
    n_with_polys = sum(1 for v in out_polys.values() if v)
    n_polylines = sum(len(v) for v in out_polys.values())
    n_points = sum(len(pl) for v in out_polys.values() for pl in v)
    _log_event(level="info" if n_with_polys else "warn",
                op="part_footprints",
                source_id=file_id,
                parts=len(part_indices),
                with_polys=n_with_polys,
                empty=len(part_indices) - n_with_polys,
                polylines=n_polylines,
                points=n_points,
                raster_sec=round(t_raster, 2))
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

    if not _ensure_source_loaded(file_id):
        return jsonify({"error": f"unknown or unloaded source: {file_id!r}",
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

    vd_key, focal_key = _view_keys(view_dir, focal)
    base_key = (file_id, vd_key, focal_key, up_axis_key)

    # Group mode: ONE silhouette around the compound of every selected
    # part.  Cache key is the full sorted index tuple so different group
    # compositions don't share results.
    if group_mode:
        gkey = base_key + ("group", tuple(sorted(part_indices)))
        cached = _cache_get(_SIL_CACHE, gkey)
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
        _cache_put(_SIL_CACHE, gkey, polys, _SIL_CACHE_MAX)
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
        cached = _cache_get(_SIL_CACHE, ck)
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
            _cache_put(_SIL_CACHE, ck, polys, _SIL_CACHE_MAX)
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

    # IFU_DEV=1 turns on Werkzeug's reloader so edits to serve.py /
    # ifu/*.py re-run the server automatically.  The reloader re-imports
    # the module on each change, which DOES re-run boot() — STEPs reload
    # (~30 s for siderail, ~3 min for contesa).  In dev that's tolerable
    # because edits are usually JS via rebuild_html.py (no restart).
    # In prod we keep auto-reload off; restart is explicit.
    dev_mode = os.environ.get("IFU_DEV", "").strip() in ("1", "true", "yes", "on")

    # boot() must run in the worker process.  When the reloader spawns
    # the child, WERKZEUG_RUN_MAIN=true; the parent (watcher) doesn't
    # need OCCT loaded.  Saves us re-loading STEPs in the watcher.
    is_reloader_watcher = (
        dev_mode and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    )
    if not is_reloader_watcher:
        boot()

    url = f"http://{args.host}:{args.port}"
    print(f"Serving on {url}")
    print(f"  - open {url} in a browser")
    print("  - click 'generate 2D' in the 3D toolbar to render the current angle")
    print("  - Ctrl+C here to stop the server")
    if dev_mode:
        print("  - IFU_DEV=1: auto-reload on edits to serve.py / ifu/*.py")
    print()
    # threaded=True: but OCCT-critical endpoints take _HLR_LOCK so we
    # never run two HLR / footprint computations at once.  Cheap
    # endpoints (healthz, figures CRUD, /) bypass the lock and respond
    # instantly even while a render is in flight.  Without this the
    # whole server hangs for the 2+ minutes a Presto raster takes.
    app.run(
        host=args.host,
        port=args.port,
        threaded=True,
        debug=dev_mode,
        use_reloader=dev_mode,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
