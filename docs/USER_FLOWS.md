# User flows

The canonical end-to-end paths a user takes. Each flow is annotated
with the endpoints fired and the files written.

## 1. Import an Onshape document → ready-to-illustrate project

```
Home  →  "+ New project"  →  "Import from Onshape" tab  →  paste URL
                                  │
                                  │  POST /api/onshape/probe  (debounced)
                                  │  ← document_name auto-fills name field
                                  │
                                  Create
                                  │
                                  │  POST /api/onshape/import {url}
                                  │  ← {id: "<job_id>", status: "queued"}
                                  │
                                  Modal flips to progress
                                  │
                                  ┌── 1.5s poll loop:
                                  │   GET /api/onshape/import/<job_id>
                                  │   ← status: queued → resolving →
                                  │      translating → downloading → ready
                                  │      progress + message strings
                                  └──
                                  │
                                  status == "ready"
                                  │
                                  │  POST /api/projects {name, primary_source_id}
                                  │  ← project record
                                  │
                                  location.hash = '#/project/<pid>'
```

**Background work**: STEP translation in Onshape's farm (sync poll
every 3 s for ≤ 15 min); HTTP download of the resulting STEP blob;
`_load_step_as_compound` reads it into a TopoDS_Compound; registered
under `out/sources/dynamic.json`.

**Files written**:
* `out/imports/<source_id>.step`
* `out/sources/dynamic.json` (updated)
* `out/projects/<pid>.json`

---

## 2. Create a View → editor opens auto-rendered

```
Project workspace  →  "+ New view"  →  redirects through ViewScreen
                                       to a placeholder figure
                                       (TODO: full new-view flow)
```

For the typical pre-existing-view path:

```
Project workspace  →  click View tile
                            │
                            │  ViewScreen briefly mounts, picks the
                            │  view's first figure (or creates a
                            │  "Default variant" if empty)
                            │
                            location.hash = '#/project/<pid>/view/<vid>/figure/<fid>'
                            │
                            EditorScreen mounts:
                              GET /api/projects/<pid>
                              GET /api/figures/<fid>
                              _loadFigureIntoEditor:
                                fileSel.value = figure.source_id
                                snapCameraTo(figure.camera.eye, target)
                                if autoGenerate (figure has a camera):
                                  showCanvasLoading("rendering view...")
                                  setTimeout 350ms (let three.js settle)
                                  generateLiveSVGForCamera(figure.camera)
                                    │
                                    │  POST /api/render {file_id, eye, target}
                                    │  ← image/svg+xml + X-Render-Polylines
                                    │
                                    │  Server kicks off background
                                    │  footprint raster on a thread.
                                    │
                                    injectLiveSVG(file_id, view_dir, svg)
                                      pane.innerHTML = cleaned svg
                                      refreshPane() activates the pane
                                      reset pan/zoom to identity
                                      switch layout to "split"
                                  hideCanvasLoading()
                              _renderVariantStrip(pid, vid, fid)
                                GET /api/views/<vid>/figures
                                ← left-sidebar thumbnail cards
```

**Latency**:
* HLR cold (first time on this camera): 3–25 s depending on parts
  count + mesh_defl.
* HLR warm: cache hit, instant.
* Footprint raster cold: 10–60 s in the background, transparent to
  the user. First click on a part may still wait briefly if the user
  clicked before the raster finished — `showShadedOutlineLoading()`
  badge pops bottom-left until cache hit.
* Variant switch: route change → new EditorScreen → same render flow.
  Each variant click triggers a new render (camera is per-figure).

---

## 3. Highlight parts + apply a preset style

```
Editor  →  click part on SVG OR on 3D pane
              │
              │  handleCanvasClick / SVG click event
              │  togglePartHighlight(idx)
              │    state.highlights.add(idx)
              │    applyHighlights()
              │      applySilhouetteFill(svg, set, ...)
              │        for each highlighted idx:
              │          _getFootprint(fid, '__live__', idx)
              │          if present → draw closed-loop SVG path
              │          if missing → showShadedOutlineLoading(N)
              │      if cache miss: setTimeout(fetchSelectedFootprints, 0)
              │        POST /api/part_footprints {file_id, eye, target,
              │                                   part_indices: [missing]}
              │        ← polylines + stats
              │        _setFootprint() per part
              │        applyHighlights() re-runs → closed loops now drawn
              │
Editor  →  click preset (Highlight / Caution / etc.)  →  _applyPreset()
              │
              │  loadPartStyles(fid) ← localStorage[partStyles_<sid>]
              │  for each highlighted idx: m[idx] = {...preset.style}
              │  persistPartStyles(fid, m) ← back to localStorage
              │  applyStyleSheet() → injects <style> tag
              │
              │  (Dirty-state poll picks up change within 1s)
              │
              ┌── 1.8s debounced auto-save:
              │   PUT /api/figures/<fid>  (silent toast suppressed)
              │   ← figure record with new selection + styles_per_part
              │
              │   Thumbnail re-capture:
              │     800ms after the save, canvas-rasterise the active
              │     SVG to 320x240 PNG, PUT /api/figures/<fid>/thumbnail
              └──
```

**Persistence frequency**: 1.8 s after the last edit settles. Ctrl+S
forces immediate save. Manual `save` button is the same code path with
a toast.

---

## 4. Generate 2D at a new angle (same source)

```
Editor in split layout
   │
   ▶  Orbit the 3D pane to a new angle
   │
   ▶  Click "generate 2D" button
   │
   │  generateLiveSVG():
   │    clearHighlights()                ← drops the selection (highlights
   │                                       don't make sense across cameras)
   │    POST /api/render {file_id, eye, target}
   │    ← SVG bytes
   │    injectLiveSVG(file_id, view_dir, svg)
   │
   │  Server-side: background footprint raster fires for the new
   │  view direction.  Cache is per-(camera-tuple), so a different
   │  angle = different cache key = fresh raster.
```

---

## 5. Variant switch within a view

```
Editor with variant strip on the left
   │
   ▶  Click a variant card (figure id X)
   │
   │  Variant strip handler:
   │    clear live svg-pane.innerHTML  (immediate visual feedback)
   │    showCanvasLoading("loading <name>...")
   │    location.hash = '#/project/<pid>/view/<vid>/figure/X'
   │
   │  EditorScreen mounts X:
   │    GET /api/figures/X
   │    _loadFigureIntoEditor(X, autoGenerate:true)
   │      ... same as flow #2 ...
   │    _renderVariantStrip() re-rendered, X marked active
   │
   ▶  Click "+ new variant"
   │
   │  GET /api/views/<vid>  +  /api/views/<vid>/figures
   │  POST /api/figures  {name: "Variant N+1", inherit view.camera}
   │  POST /api/views/<vid>/figures/<new_fid>  (attach)
   │  location.hash = '#/project/<pid>/view/<vid>/figure/<new_fid>'
```

---

## 6. Configuration changes (Onshape import only)

```
Editor (source has onshape_ids)
   │
   ▶  3D pane has a "Onshape configuration" panel
   │  with parameter dropdowns
   │
   ▶  Change a value (e.g. Rise: Up → Down)
   │
   │  250ms debounce
   │  _cfgApply(source_id):
   │    POST /api/sources/<sid>/reconfigure
   │      {configuration: {<param_id>: <value>}}
   │
   │    Server:
   │      probe Onshape for element type
   │      POST /api/v10/<assemblies|partstudios>/.../translations
   │        with configuration=<encoded string>
   │      Poll translation (every 2s, 15min budget)
   │      Download new STEP, overwrite imports/<sid>.step
   │      _load_source_into_memory replaces _SHAPES[sid]
   │      Evict _RENDER_CACHE / _FOOT_CACHE / etc. for sid
   │      sources_store.register (upsert with new updated_at)
   │
   │    Client:
   │      Bust the local loaded.set(sid) GLB cache
   │      loadSource(sid) → /api/glb/<sid> for fresh mesh
   │      "3D updated" badge in the config panel
```

---

## 7. Export

```
Editor  →  "export SVG" button in header  →  legacy export path
              │
              │  Serialise the active SVG pane (with view-transform
              │  applied for current zoom/pan)
              │  Trigger browser download
              │
              No server round-trip.  The exported SVG is the literal
              DOM of the live pane -- highlights, callouts, applied
              styles all bake in.
```

## See also

* [API.md](API.md) — endpoint shapes
* [AUDIT.md](AUDIT.md) — reliability + perf gaps in these flows
