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

---

## Reliability

### 🔴 P0 — Browser `viewer.html` cache (26 MB) sometimes stale after rebuild

**Symptom**: user rebuilds `out/viewer.html`, refreshes the page, and
still sees old behaviour. Required `Ctrl+Shift+R` ~daily.

**Why**: `serve.py`'s `_cors_and_no_cache` sets `Cache-Control:
no-store` for `/` and `/viewer.html`, but the file is so large that
Chrome's blink-side memory cache keeps the parsed result. Hash
navigation doesn't bump revalidation.

**Fix**: append a per-build ETag query string when the server emits
the HTML wrapper (e.g. `<link>` to the JS could carry `?v=<mtime>`),
OR split the JS out of the HTML template and let the browser cache
the JS independently. Today we ship one fat HTML; tomorrow we could
have viewer.html load `viewer.js` separately, then ETag the JS file.

### 🔴 P0 — Server has no auto-reload during development

**Symptom**: edit `serve.py`, forget to restart, spend ten minutes
puzzled that the new endpoint 404s.

**Why**: `app.run(threaded=True)` without `use_reloader=True`.

**Fix**: gate Werkzeug's reloader behind `IFU_DEV=1` env var. Reload
on filesystem change for `serve.py` + `ifu/*.py`. Be careful: the
reloader will re-run `boot()` and re-load every STEP (~3 min) on
each Python file save. Throttle.

### 🟠 P1 — Variant switch with auto-save race

**Symptom**: edit variant A, click variant B fast. Sometimes A's
last edit isn't persisted.

**Why**: auto-save debounce is 1.8 s. If the route changes before
the timer fires, the pending edit is lost — the EditorScreen
teardown clears `AppState.currentFigureId` first, then the timer
fires and `updatingExisting` is false, so it POSTs a new figure (or
errors).

**Fix**: flush pending auto-saves before the route changes. The
teardown function can `await` an outstanding save and skip cleanup
of `currentFigureId` until it completes. Add a "saving..." indicator
to the variant card the user is leaving so they see the round-trip.

### 🟠 P1 — Footprint raster blocks `/api/render` indirectly via `_HLR_LOCK`

**Symptom**: user generates 2D at angle A, raster prefetches. User
rotates and clicks generate 2D again before raster A finishes.
Second render request waits 30-60 s for the raster to release the
lock.

**Why**: OCCT isn't thread-safe so `_HLR_LOCK` serialises everything
that touches a TopoDS shape. Background raster grabs it for the full
raster duration. A user-initiated `/api/render` blocks behind it.

**Fix**: split the raster into chunked work (one solid at a time)
that yields the lock between chunks. Or run the raster against a
**copy** of the shape so the lock is only needed for the brief copy
(BRep operations can be cheap when the input is already meshed).

### 🟠 P1 — `_FOOT_CACHE` size cap is FIFO with no per-source tracking

**Symptom**: switch projects often, eventually the cache evicts the
footprints for the project you're back in, and the first click
triggers a fresh 30-60 s raster.

**Why**: `_FOOT_CACHE_MAX = 2000` entries, evict-oldest-first
regardless of access. Each view caches `n_parts` entries so a
50-part assembly fills 50 slots per view.

**Fix**: LRU (`collections.OrderedDict.move_to_end` on access).
Better: bound by total polyline memory rather than entry count
(some views have 1 polyline per part, some have 5).

### 🟠 P1 — `_load_step_as_compound` doesn't validate against partial / corrupt STEPs

**Symptom**: a STEP that arrives truncated (network blip during
download) is loaded as a TopoDS_Compound with 0 solids; the source
is marked loaded; subsequent renders return 0 polylines and the
user gets the "0 lines" warning with no clear cause.

**Fix**: in `_load_step_as_compound` after building the compound,
walk it and `assert split_solids(comp)` returns ≥ 1 solid. Raise
explicitly if not, so the import worker can mark the job
`status: "error"` instead of leaving a broken source in the dynamic
registry.

### 🟠 P1 — Onshape configuration parser only knows enum / boolean / quantity / string

**Symptom**: documents with `BTMConfigurationParameterMatrix`,
`...Length` (different from Quantity), or list-of-enum params come
back as `{type: "unknown"}` and render as a plain text input. User
types nonsense in, the next `start_step_translation` may accept it
but produce unexpected geometry.

**Fix**: extend `get_element_configuration` parser to handle all
five common Onshape parameter types. Add a "raw mode" fallback that
shows the raw `btType` next to the input so power users know what's
unsupported.

### 🟠 P1 — Empty hash redirect can ping-pong if Home throws

**Symptom**: any error in HomeScreen's mount (e.g. `/api/projects`
500s) leaves the user on a blank `app-root` with `#/` in the URL.
A refresh stays on the broken Home.

**Fix**: HomeScreen should render a fallback "couldn't load projects"
card on fetch failure with a Retry button (and a link to Settings).
Don't silently `catch(_e){}`.

### 🟠 P1 — Server log buffer is process-local, lost on restart

**Symptom**: user reports "I generated 2D yesterday and nothing
appeared"; we restart the server (good practice) and the log of
yesterday's request is gone.

**Fix**: also write the log to `out/debug.log` (append-only, capped
at e.g. 5 MB with rotation). The `/api/debug/log` endpoint can
return both in-memory + on-disk tail.

### 🟡 P2 — `out/_live_<fid>.svg` files accumulate

**Symptom**: every `/api/render` writes the SVG to disk for
debugging; over time `out/` grows by ~3 MB per render.

**Fix**: write to a temp file by default, only persist on `?save=1`
query flag. Or rotate (keep the last 20).

### 🟡 P2 — Onshape import worker has no cancel

**Symptom**: user pastes the wrong URL, sees "translating…" tick up,
realises it's wrong. There's no way to abort — they wait 5 min, then
delete the source manually.

**Fix**: `DELETE /api/onshape/import/<job_id>` to abort an in-flight
job. The translation poll loop checks a `cancelled` flag each tick.

---

## Performance

### 🔴 P0 — `compute_visible_footprints` is single-threaded Python + cv2

**Symptom**: 138-part siderail at full res (3000 px) takes 5 minutes
to raster. At the coarser `mesh_defl × 1.5` + 1500 px we're now down
to ~1 min, but it's still the dominant cost on the first interaction
with a view.

**Roads to a real fix**:
1. **Numpy-vectorise the rasteriser**. Today it loops every triangle
   in Python with `cv2.fillPoly` per triangle. Batching with
   `cv2.fillPoly(img, [all_triangles])` would help marginally —
   the real win is moving per-pixel z-test into vectorised numpy.
2. **Move the z-buffer to GPU via three.js**. We already have the
   shape meshed in the browser; render every part to a distinct
   color in an off-screen canvas, read back pixels, run cv2 contour
   trace on the result. Browser-side raster is ~50× faster.
3. **Reuse the HLR mesh**. `compute_visible_footprints` calls
   `BRepMesh_IncrementalMesh` independently of HLR's own mesh.
   Sharing would save ~30%.

### 🟠 P1 — `injectLiveSVG` always discards the existing pane content

**Symptom**: switching variants triggers a fresh `pane.innerHTML =
cleaned` even when the source + camera are identical (same view's
sibling figures all share view.camera). The browser re-parses 2.5 MB
of SVG for nothing.

**Fix**: hash the incoming SVG bytes; skip the innerHTML if the hash
matches the previously-injected one. Move just the selection /
styles updates.

### 🟠 P1 — GLB regen on every reconfigure

**Symptom**: changing an Onshape config + flipping back to a
previous config re-runs the GLB export every time. ~5 s per
toggle on the chair.

**Fix**: cache the GLB blob keyed by (source_id, config_string). The
shape itself is in `_SHAPES`; cache the bytes too.

### 🟠 P1 — Initial page load is 26 MB

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

### 🟡 P2 — Render cache is keyed by exact float vd / focal

**Symptom**: user generates 2D at camera A, rotates by 0.001°,
generates again — cache miss, full HLR re-run.

**Why**: cache key rounds to 3 decimal places (~ 0.057° at unit
length) but tiny float drift in OrbitControls plus mouse-quantization
crosses the boundary often.

**Fix**: round to 2 decimal places (one extra digit of tolerance)
OR quantise the camera deltas in the JS so distinct user gestures
produce distinct cache keys but tiny drift doesn't.

### 🟡 P2 — three.js perf HUD only updates on user interaction

**Symptom**: pan/zoom feels smooth then suddenly drops to 5 FPS for
no obvious reason; no metric exposes it.

**Fix**: keep a rolling 60-frame FPS estimate, expose via
`IFU_VIEWER.getRendererState()`, surface in the `?dbg=1` HUD.

---

## UX polish

### 🟠 P1 — `+ New project` modal has no "back to project list" if the user pastes a bad URL

**Symptom**: paste a URL that parses but the document isn't shared
with the user — import fails with a 502, modal stays open with the
URL field disabled. Cancel button works but if the user clicks
"Create" again the error repeats.

**Fix**: on import error, re-enable the URL field + the source-tab
buttons, surface the underlying error message inline in the modal
(not just a toast), and clear the progress widget.

### 🟠 P1 — Onshape Configurations: changes are immediate, no preview

**Symptom**: user wants to A/B compare configurations but each pick
re-translates synchronously (~12 s on the chair) and overwrites the
on-disk STEP. There's no "preview only" mode.

**Fix**: maintain a `(source_id, config_string)` → step_path cache,
let the user toggle between cached configs without re-translation.

### 🟠 P1 — "+ New view" doesn't fully exist yet

**Symptom**: clicking `+ New view` on the project workspace redirects
to a placeholder figure id that doesn't really save anything. To
genuinely create a new view, the user has to be in an existing view's
editor.

**Fix**: implement the proper flow:
1. Click `+ New view` → editor opens in "view-creation" mode
2. User poses the camera
3. Click `Save view` → server POSTs `/api/views` with the camera,
   server-rasters a thumbnail, redirect to that view's editor

### 🟡 P2 — Settings screen doesn't surface dynamic sources

**Symptom**: import 5 Onshape docs over a month, no UI to see them
or delete them.

**Fix**: in Settings, a "Imported sources" section listing every
dynamic source, with a per-row Delete button (cascade-removes the
STEP + the dynamic.json entry).

### 🟡 P2 — Variant strip thumbnails are 56×42 — too small to distinguish

**Symptom**: 4 variants of the same view with subtly different
highlights look identical at thumbnail size.

**Fix**: hover preview (250 ms delay) shows a bigger 256×180 inline
near the cursor.

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
