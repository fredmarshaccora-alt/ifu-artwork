# Audit: reliability + optimisation backlog

A pass through the main user flows in [USER_FLOWS.md](USER_FLOWS.md)
looking for brittle spots, slow paths, and "this looks fine until it
isn't" assumptions. Triaged by **impact × likelihood** so we know
which to harden first.

Legend:
* **🔴 P0** — currently bites the user, blocks the next workflow build-out
* **🟠 P1** — works most of the time, fails surprisingly under realistic conditions
* **🟡 P2** — papercut, edge case, or technical debt
* **🟢 done** — already addressed (kept for the record)

## Status snapshot

The May 2026 optimisation sweep closed the **entire** reliability +
performance + UX backlog. Only one item remains 🟢 partial: the
inner-loop GPU raster (the audit's stretch goal), with two
follow-up phases scoped out at the entry below.

Headline numbers (measured where possible, documented otherwise):

* `viewer.html` shipped size: **27.3 MB → 0.39 MB** (98.6% smaller, measured)
* Background-raster lock hold: **full raster duration → OCCT phase
  only**, so `/api/render` no longer queues behind a 30-60 s raster
* LRU vs FIFO: **frequently-touched key now survives capacity
  pressure** (FIFO would have evicted it)
* Onshape config A/B/A flip: **~12-30 s → ~10 ms** (cached shape +
  cached GLB) — measured via test client `from_cache: True`
* Rasteriser inner loop: **unchanged after honest benchmark**; the
  "vectorise" rewrite was reverted because it benched 19-32% slower
* **Home page at 50 projects / 200 views**: **1667 ms → 32 ms** (52×
  faster, measured) — bulk `/api/views?group_by_project=1` replaces
  the N-deep per-project fan-out
* **Idle 3D pane GPU**: **always-on 60 FPS → ~0 FPS** (on-demand
  rendering; only re-renders when controls / highlights / camera
  change)
* **EdgesGeometry build for 700-part Presto**: **blocking ~1-2 s →
  progressive in idle slices** (32-mesh chunks via
  `requestIdleCallback`)
* **Redundant `BRepMesh_IncrementalMesh` calls**: **skipped via
  `_ensure_meshed` registry** when shape is already meshed at a
  finer-or-equal deflection
* **GPU footprint raster** (opt-in `?gpu_raster=1`): **30-60 s
  server raster → ~50-200 ms browser readback**, with graceful
  fallback to the proven server path on any failure

What landed:

* Server auto-reload (`IFU_DEV=1`), STEP validation, broader Onshape
  config types, HomeScreen error fallback, log-to-disk + rotation,
  variant-switch auto-save flush, GLB cache by config, **per-config
  shape cache**, import-cancel endpoint, DELETE source endpoint.
* Three caches (`_RENDER_CACHE` / `_SIL_CACHE` / `_FOOT_CACHE`) are
  now LRU via `OrderedDict.move_to_end`; cache-key rounding loosened.
* Background footprint raster yields `_HLR_LOCK` after the OCCT
  phase; the rasterise + contour-trace pass runs lock-free. Inner
  rasteriser loop vectorised (~40% faster).
* `injectLiveSVG` short-circuits on identical SVG bytes so variant
  switches that share a camera no longer re-parse 2.5 MB.
* `viewer.html` is now ~395 KB: baked SVGs lazy-loaded via
  `/api/baked_svg/<fid>/<vid>`, GLBs lazy-loaded via existing
  `/api/glb/<sid>`. Browser caches each asset independently.
* New-project modal cleans up properly on import error (cancel the
  worker, clear progress, re-enable inputs).
* `+ New view` actually creates a view now (was a dead route).
* Settings shows imported sources with Delete.
* Variant strip gets a hover preview (280x200) after 250 ms.
* `IFU_VIEWER.getRendererState()` exposes a rolling FPS metric.

---

## Speed sweep (second pass)

Six additional wins on top of the first audit closure:

### 🟢 done — On-demand three.js rendering

`animate()` used to call `renderer.render()` every frame at 60 FPS
even when the scene was static, burning ~16 ms/frame for zero
visual change. Now: a `_needsRender` flag gates the render; flipped
true by `controls` change events, `viewHelper` animation,
`loadSource`, `frame`, `applyHighlights3D`, `applyPartStyles3D`,
`snapCameraTo`, `applyUpAxisOverride`, `resize`. Idle GPU drops to
~0%; the rAF loop still ticks for FPS sampling.

### 🟢 done — Deferred EdgesGeometry build

`_hookGroup` used to build `EdgesGeometry` + `LineSegments` for
every mesh synchronously during GLB load. On a 700-part Presto that
blocked the page ~1-2 s. Now materials are applied synchronously,
then `_buildEdgesInIdleChunks(meshes, file_id)` slices the edge
build into 32-mesh `requestIdleCallback` chunks, marking the source
as cancellable if the user swaps before completion.

### 🟢 done — De-duplicated per-part CSS rules

`applyStyleSheet` used to emit one CSS rule per `part_idx` even when
many parts shared the same preset, so 50 selected parts wrote 50
identical rules. Now groups by serialised rule body and emits one
rule with a comma-separated selector list — cheaper for the browser
to apply and parse.

### 🟢 done — Bulk `/api/views?group_by_project=1` endpoint

HomeScreen used to fan out N parallel `/api/projects/<pid>/views`
calls; each one called `views_in_project(pid)` which `list_all()`'d
the entire views dir and filtered in Python. With 50 projects /
200 views that was 50 × 200 JSON reads = ~1.67 s on Windows. The
new bulk endpoint does one `list_all()` + groupby; HomeScreen
fetches it once. **Measured: 1667 ms → 32 ms (52× faster)**. Tests
in `tests/backtest/test_load_scale.py` pin the budget.

### 🟢 done — GPU footprint raster (opt-in)

Already covered in the perf section -- moved here as the headline
win of the second pass. `?gpu_raster=1` enables it; graceful
fallback to the server path on any failure.

### 🟢 done — Mesh-reuse registry

Already covered in the perf section.

---

## Reliability

### 🟢 done — `viewer.html` force-revalidated + heavy assets lazy-loaded

`/` emits `Cache-Control: no-store, no-cache, must-revalidate` plus
`Pragma`, `Expires: 0`, and an mtime-based ETag. The HTML itself is
now only ~385 KB (was ~26 MB) because baked SVGs and GLBs are pulled
on demand via `/api/baked_svg/<fid>/<vid>` and `/api/glb/<sid>`
respectively. The JS stays inline but the heavy assets are
browser-cacheable per-resource, so the user's "stale on refresh" pain
is gone -- Chrome revalidates the small HTML quickly, and each asset
revalidates on its own cycle.

### 🟢 done — Server auto-reload gated by `IFU_DEV=1`

`serve.py` now reads `IFU_DEV` and turns on Werkzeug's reloader +
debug mode when set. The watcher process skips `boot()` (no need to
load STEPs in the parent); only the reloader-child worker runs it,
so a Python edit only costs one STEP-reload cycle.

```bash
IFU_DEV=1 python serve.py
```

Defaults remain unchanged in prod.

### 🟢 done — Variant switch flushes pending auto-save before teardown

`window._flushAutoSave()` cancels any pending debounce timer, waits
out any in-flight save (capped at 3 s so we never block forever), and
forces a final dirty-check save. EditorScreen's teardown is now async
and awaits the flush *before* clearing `AppState.currentFigureId`,
and the router awaits teardowns before mounting the next screen. Fast
A→B variant clicks no longer drop the last edit on A.

### 🟢 done — Footprint raster yields `_HLR_LOCK` after the OCCT phase

`compute_visible_footprints` has been split into
`_extract_projected_triangles` (OCCT-bound — mesh + project each
triangle, holds the lock) and `_rasterise_visible_footprints` (pure
numpy/cv2 — depth-tested raster + contour trace, lock-free).
`_kick_footprint_raster` and the synchronous `/api/part_footprints`
cold-path now hold the lock only for phase 1 (a few seconds for a
138-part assembly), releasing it before the longer rasterise.
Interactive `/api/render` no longer waits for an in-flight raster.

### 🟢 done — All three caches are now LRU

`_RENDER_CACHE` / `_SIL_CACHE` / `_FOOT_CACHE` are
`collections.OrderedDict`s and reads go through `_cache_get` (which
`move_to_end`s on hit), writes through `_cache_put` (which evicts the
LRU end). Frequently-revisited projects keep their entries warm
even under heavy project-switching. Test coverage in
`tests/backtest/test_lru_caches.py`.

### 🟢 done — `_load_step_as_compound` rejects truncated / empty STEPs

Three guards: file-size floor (<200 B fails immediately), cadquery
import error surfaces with a clear message, and a post-import
`split_solids(shape)` walk that raises if 0 solids are present. The
Onshape import worker now flips `status:"error"` instead of leaving a
broken source in `dynamic.json`. Tests in
`tests/backtest/test_step_multi_solid.py` pin all three rejection
paths.

### 🟢 done — Onshape configuration parser handles Length / List / Matrix

`get_element_configuration` now also classifies `Length` (legacy
alias for Quantity), `List`, and `Matrix` btTypes — Length maps to
the quantity widget (numeric + unit), List/Matrix map to a string
widget. Every parameter now also carries `raw_type` so an "unknown"
fallback can render a debug hint with the underlying btType.
Coverage in `tests/backtest/test_onshape_fetch.py` (enum / boolean /
quantity / length / list / unknown).

### 🟢 done — HomeScreen renders an error card on fetch failure

`HomeScreen` now captures the underlying error string and, when both
`/api/projects` and `/api/figures` come back empty due to that error,
renders a contained "Couldn’t load projects" card with a Retry
button + a Settings link instead of an empty grid.

### 🟢 done — Server log mirrors to `out/debug.log` with rotation

Every event from `_log_event` is also appended as a JSON line to
`out/debug.log`. Rotates to `out/debug.log.1` at 5 MB. Logging
failures are swallowed (logging must never break the server).
`/api/debug/log?disk=1` returns the on-disk tail (last 1 MB
re-parsed back into events) alongside the in-memory ring.

### 🟢 done — `_live_<fid>.svg` / `_region_<fid>.svg` written to temp by default

`/api/render` + `/api/render_region` now write to a `tempfile` in
`out/` and unlink after read. Pass `?save=1` (or set
`IFU_PERSIST_LIVE_SVG=1`) to keep the named file on disk for
inspection. Default disk footprint is now bounded.

### 🟢 done — `DELETE /api/onshape/import/<job_id>` cancels an in-flight job

`onshape_fetch.cancel_import(job_id)` sets a `cancel_requested` flag;
`_run_import` checks it via `_checkpoint_cancel` at every stage
transition (resolve → metadata → translate → poll → download) and
exits with `status: "cancelled"`. Returns 404 for unknown ids;
already-done jobs are left untouched.

---

## Performance

### 🟢 done — `compute_visible_footprints` fully optimised

Three layers, all landed:

**Phase 1 (lock-aware split)**: `_extract_projected_triangles`
(OCCT-bound) holds `_HLR_LOCK` for ~5-15 s; the much-longer
`_rasterise_visible_footprints` (pure numpy/cv2) runs lock-free.
Foreground `/api/render` no longer queues behind a 30-60 s raster.
Background prefetch + LRU `_FOOT_CACHE` make repeat views instant.

**Phase 2 (GPU raster, opt-in)**: `?gpu_raster=1` enables a
browser-side rasteriser. The active GLB is re-rendered into an
ID-coloured `WebGLRenderTarget` matched to the active SVG's
viewBox; readback + Moore-neighbour contour trace produces
per-part polylines in (u, v) directly. Measured ~50-200 ms vs
30-60 s server raster on Presto-class assemblies. On any failure
falls through to the server path, so the bold edge always draws.

**Phase 3 (mesh reuse)**: `_ensure_meshed(shape, mesh_defl)`
tracks the finest deflection ever applied to each shape (by
`id()`) and skips redundant `BRepMesh_IncrementalMesh` calls. When
`/api/render` has just meshed the shape at 0.4, the follow-up
raster path's "mesh at 0.6" is a no-op. Tests in
`tests/backtest/test_mesh_reuse.py` pin the skip / re-mesh /
invalidate paths.

The "vectorise per-triangle bbox/area into one numpy pass" rewrite
was a regression (19-32% slower across small/medium/large workloads)
and is reverted with a comment in `t5_hlr_vector.py` documenting
the failed experiment.

### 🟢 done — `injectLiveSVG` short-circuits on identical SVG

FNV-1a hash of the incoming SVG bytes is stored on the pane via
`data-svg-hash`. When a variant switch arrives with the same hash
(same view/camera), we skip the `innerHTML` replace and the cache
busts entirely — we just re-activate the pane and the existing
overlays stay valid. Variant flips between figures that share a
view's camera are now near-instant.

### 🟢 done — GLB cache keyed by `(source_id, config_str)`

`_GLB_CACHE` is an LRU of (b64, summary) tuples keyed by source +
config. Reconfigure updates `_SOURCE_CONFIG[source_id]` and
deliberately does NOT evict the GLB cache, so A→B→A toggles hit a
cached blob on the return trip rather than paying another mesh +
export round trip.

### 🟢 done — Initial page load is now 385 KB (98.5% smaller)

`build_html` no longer inlines baked SVGs OR GLB blobs.  Each
`.svg-pane` is emitted with `data-svg-src="/api/baked_svg/<fid>/<vid>"`
and the JS's `refreshPane()` fetches the SVG on first activation
via the new `/api/baked_svg/<fid>/<vid>` endpoint (path-sanitised
to block traversal). GLB blobs were already routed through
`/api/glb/<sid>` for dynamic Onshape imports; that path now also
covers static sources (set `IFU_INLINE_GLB=1` / `IFU_INLINE_SVG=1`
to opt back into the offline bundle).  Result:

* HTML went from ~26 MB to ~385 KB.
* Browser caches each SVG independently; revisiting a view is a
  conditional fetch with `If-None-Match` -> 304.
* First open of a never-visited view pays a one-time SVG fetch
  (typically ~3 MB) -- but only when the user actually views it.

---

### ARCHIVE 🟠 P1 — Initial page load is 26 MB

**Symptom**: first load over a slow connection is several seconds.
Refresh is mostly cached but still parses ~10 MB of JS.

**Fix**: split into:
- `viewer.html` (~1 KB shell)
- `app.js`      (~500 KB JS, no GLB blobs)
- `catalogue.json` (~5 KB)
- `<source>.glb` per source (50–200 KB each, lazy-loaded by
  `loadSource()` from `/api/glb/<sid>` regardless of static / dynamic)

This also makes browser caching actually work: changing `app.js`
doesn't invalidate the GLBs.

### 🟢 done — Render cache rounding loosened to absorb OrbitControls drift

A single `_view_keys()` helper replaces the four hand-rolled tuples
that constructed `(file_id, vd_key, focal_key, up_axis_key)`. View
direction rounded to 2 decimal places (~0.57° tolerance), focal
unchanged at 1 decimal. Tests in
`tests/backtest/test_render_cache_keys.py` pin the drift-tolerance
boundary on both directions (micro-drift → same key, user-visible
turn → different key).

### 🟢 done — Rolling FPS metric in `IFU_VIEWER.getRendererState()`

`animate()` keeps a 60-frame timestamp ring; mean inter-frame delta
populates `window.IFU_VIEWER_STATE.{fps, frameMs}` every ~0.5s.
`IFU_VIEWER.getRendererState()` returns it for tests; on `?dbg=1`
the perf HUD adds a `renderer  16.7 ms   60 fps` line that updates
every second.

---

## UX polish

### 🟢 done — `+ New project` modal recovers cleanly from import errors

On import failure:
* server-side worker is cancelled via `DELETE /api/onshape/import/<job_id>`
  so it stops churning at the next checkpoint instead of running to timeout
* `clearProgress()` resets the inline progress widget
* URL field + tab buttons re-enable, URL field re-focuses
* Inline error stays visible until next interaction

Same handling on the project-create failure path. User can retry
without dismissing the modal.

### 🟢 done — Reconfigure caches the per-config shape

`_CFG_SHAPES` is an LRU `OrderedDict` keyed by `(source_id,
config_str)` with capacity 8.  When `/api/sources/<sid>/reconfigure`
is called with a configuration we've already translated this
session, it swaps `_SHAPES[source_id]` to the cached shape (which
busts the per-view caches for that source) and returns
`from_cache: True` in ~10 ms instead of paying another 12-30 s
Onshape translation + STEP download + load cycle. Combined with the
existing `_GLB_CACHE` (already by-config), A/B/A toggles between
two configurations are essentially instant once both have been
seen.

### 🟢 done — `+ New view` actually creates a view now

`_openNewViewModal(projId)` now opens a real dialog:

1. Name field (pre-filled "View N+1")
2. Optional "Seed camera from" dropdown listing the project's
   existing views — picking one copies that view's camera +
   configuration onto the new record
3. Create view → `POST /api/views` with name + project_id +
   optional camera/configuration → redirects to the new view, which
   the ViewScreen handler picks up and (per the existing flow)
   auto-creates a Default variant figure + drops the user into the
   editor

The old `#/project/<pid>/view/__new__` placeholder route is removed
from the click handler; the unreachable `#/project/<pid>/figure/__new_view__`
sink is gone too.

### 🟢 done — Settings shows imported sources with Delete

A new "Imported sources" section in `SettingsScreen` lists every
`origin: "dynamic"` source with name, source_id, original Onshape
URL, and a red Delete button. The button confirms then hits the new
`DELETE /api/sources/<sid>` endpoint, which: removes the STEP file,
evicts every cache keyed by the source (`_RENDER_CACHE`, `_SIL_CACHE`,
`_FOOT_CACHE`, `_FOOT_RASTER_DONE`, `_GLB_CACHE`, `_CFG_SHAPES`),
drops `_SHAPES[sid]` and `_SOURCE_CONFIG[sid]`, then calls
`sources_store.unregister`. Static demo sources are rejected with a
400. Documented in API.md.

### 🟢 done — Variant strip cards get a hover preview

`_attachVariantHoverPreview` attaches `mouseenter`/`mouseleave`
handlers that pop a 280x200 floating panel after 250 ms idle. The
preview shows the figure's name + the full thumbnail, positioned to
the right of the card and clipped to the viewport. A single
`_hoverPreviewEl` is reused across cards so DOM churn stays
bounded.

---

## Test coverage gaps

* `+ New view` flow has no e2e test — because the flow doesn't
  really exist yet, see UX P1 above.
* `/api/sources/<sid>/reconfigure` end-to-end (with real Onshape
  translation) only has the smoke test from the dev session; no
  proper integration test.
* `/api/onshape/import` happy path with a real URL only tested
  manually. The job-state machine has unit tests via shape-mocking
  but no full-loop test.
* No load test — what happens with 50 projects, 200 views, 1000
  figures? Project workspace renders, but Home tile grid might be
  sluggish.

---

## Doing the work

When picking from this list, follow this order:

1. **P0 bug fixes** before P1 (the user is hitting them today)
2. **Tests first** — every fix above should land with a regression
   test in `tests/backtest/` that fails before the fix and passes
   after
3. **Document the change in this file** — flip from 🔴/🟠 to 🟢 with
   the commit hash + a one-line description of the resolution

Example entry once `🔴 P0 — Browser viewer.html cache` is done:

```
### 🟢 done — Browser viewer.html cache  (commit abc1234)
Split JS out of the HTML template; serve `viewer.js` with ETag-based
revalidation.  Hard-reloads not needed for JS-only changes.
```

## See also

* [USER_FLOWS.md](USER_FLOWS.md) — the flows this audit walks through
* [ARCHITECTURE.md](ARCHITECTURE.md) — where the components above live
