# Architecture

The tool is a **local-only Flask app** with a single HTML page (`out/viewer.html`)
served at `/`. There's no database, no service worker, no build step beyond
running `python rebuild_html.py` to re-stitch `viewer.html` from
`build_viewer.py`'s template. Per-user data lives on disk under `out/`.

## Three layers

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (out/viewer.html — single self-contained file)     │
│  • hash router → screen mounts (Home / Project / View /     │
│    Editor / Settings)                                       │
│  • three.js 3D viewer (OrbitControls, IBL, palette)         │
│  • SVG 2D pane (HLR output, per-part interactivity)         │
│  • editor sidebar with variant strip, preset styles         │
│  • auto-save, dirty-state, loading overlays                 │
└──────────────────────────────┬──────────────────────────────┘
                               │  fetch(API_BASE + '/api/...')
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  Flask server (serve.py)                                    │
│  • /api/render          OCCT HLR → SVG bytes                │
│  • /api/part_footprints rasterised closed-loop outlines     │
│  • /api/glb/<sid>        on-the-fly GLB for the 3D viewer   │
│  • /api/projects, /api/views, /api/figures                   │
│  • /api/onshape/*       URL probe + import + reconfigure    │
│  • /api/sources, /api/settings                              │
│  • /api/debug/log       rolling structured log              │
│  Background threads: footprint raster prefetch, Onshape     │
│  import worker.  _HLR_LOCK serialises OCCT calls.           │
└──────────────────────────────┬──────────────────────────────┘
                               │  in-memory + on-disk
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  Python persistence + CAD layer (ifu/, t5_hlr_vector.py)    │
│  • ifu/projects.py, views.py, figures.py, sources.py        │
│    Plain JSON files under out/projects/, out/views/, etc.   │
│  • ifu/onshape_fetch.py — REST translation, polling,        │
│    download, configuration parsing                          │
│  • t5_hlr_vector.py — OCCT HLR + footprint rasterizer       │
│  • ifu/glb.py — trimesh GLB export                          │
│  In-memory caches in serve.py: _SHAPES (TopoDS), _RENDER_CACHE│
│  (SVG bytes), _FOOT_CACHE (per-part polylines),             │
│  _FOOT_RASTER_DONE / _INFLIGHT (raster state).              │
└─────────────────────────────────────────────────────────────┘
```

## Key files

| file | role |
|---|---|
| `serve.py` | Flask app; all HTTP endpoints; in-memory shape cache; background raster thread; structured log |
| `build_viewer.py` | Builds `out/viewer.html` — bakes the catalogue (per-source parts + view dirs) + GLB blobs into a single page |
| `rebuild_html.py` | Rebuilds `viewer.html` from cached SVGs without re-running HLR — fast iteration on the JS |
| `t5_hlr_vector.py` | OCCT HLR pipeline (`run_hlr_per_solid`, `write_svg_parts`), footprint rasterizer (`compute_visible_footprints`), shape rotation helpers |
| `ifu/config.py` | SOURCES tuple (built-in demos: siderail / presto / contesa), VIEWS preset directions, `OUT` path |
| `ifu/sources.py` | Dynamic source registry — Onshape imports live here, persisted to `out/sources/dynamic.json` |
| `ifu/projects.py` | Project CRUD, file-per-id JSON under `out/projects/` |
| `ifu/views.py` | View CRUD + idempotent migration that spawns a View per pre-Phase-3 figure |
| `ifu/figures.py` | Figure CRUD + thumbnail PNG storage (`out/figures/<id>.png`) |
| `ifu/onshape_fetch.py` | Onshape URL parsing, STEP translation submit/poll/download, configuration normalisation, background import worker |
| `ifu/onshape_client.py` | TBA-auth wrapper around the Onshape REST API |
| `ifu/glb.py` | trimesh GLB export of a TopoDS shape (used by `/api/glb/<sid>`) |
| `ifu/settings.py` | Local-only app settings (default colours, last-loaded source, etc.) |
| `out/viewer.html` | The actual UI — built artefact, ~26 MB (catalogue + GLBs inline) |

## Build vs runtime

* **Build time** (`python rebuild_html.py`): scrape catalogue from existing
  baked SVGs, optionally re-mesh + re-export GLBs, splice into the HTML
  template inside `build_viewer.py`, write `out/viewer.html`.
* **Runtime** (`python serve.py`): load every STEP into `_SHAPES`, run
  the boot-time view-migration, serve `viewer.html`, answer API calls.

The user never re-runs HLR for the bundled sources unless the source's STEP
changes. Onshape imports run translation + STEP download + GLB gen on the fly
the first time and cache from then on.

## In-browser architecture

```
location.hash  →  _matchRoute()  →  mount fn  →  teardown fn
                       │
       ┌───────────────┼─────────────────────────┐
       ▼               ▼                          ▼
   HomeScreen     ProjectScreen          EditorScreen
                       │                          │
                       ▼                          ▼
                   ViewScreen          (legacy editor + variant strip)
                  (redirects)
```

Routes (post-Phase 3):

| route | mount |
|---|---|
| `#/` | `HomeScreen` — project tiles |
| `#/project/<pid>` | `ProjectScreen` — views grid + legacy "unfiled figures" |
| `#/project/<pid>/view/<vid>` | `ViewScreen` — redirector → editor on view's first figure |
| `#/project/<pid>/view/<vid>/figure/<fid>` | `EditorScreen` — editor + variant strip |
| `#/project/<pid>/figure/<fid>` | `EditorScreen` — for legacy figures with no view |
| `#/settings` | `SettingsScreen` |
| empty hash | redirects to `#/` |

The "editor" is the original pre-router page (`<header>` + `<main>` in the
HTML template); `EditorScreen` just shows it instead of mounting into
`#app-root`. The `body.project-scoped-editor` CSS class on figure routes
hides developer noise + reveals the preset/variant sidebars.

## Data model

See [DATA_MODEL.md](DATA_MODEL.md) for the full schema. Short version:

```
Project ──< View ──< Figure
   │         │        │
   │         │        └── selection + per-part styles + auto-saved
   │         └── shared camera angle + (optional) configuration
   └── owns one primary source (the model being illustrated)
```

A source is either:
* **static** — built into `ifu/config.py` (siderail, presto, contesa)
* **dynamic** — imported from Onshape, persisted to `out/sources/dynamic.json`

## The HLR pipeline

```
STEP file  →  _SHAPES (in-memory TopoDS_Compound)
                            │
            /api/render ────┤  ──→  HLRBRep_PolyAlgo  ──→  per-edge polylines
                            │                              tagged by category
                            │                                     │
                            │                                     ▼
                            │                              write_svg_parts
                            │                                     │
                            │              SVG bytes (cached)  ◄──┘
                            │
            /api/part_      │   triangle z-buffer raster  ──→  per-part
            footprints  ───┤   (compute_visible_footprints)    closed-loop
                            │                                   polylines
                            │                                   (cached)
                            │
  background thread       ──┘  same raster, runs immediately after each
  (prefetch)                   /api/render so the cache is warm by the
                               time the user clicks a part
```

`build_projector(view_dir, focal)` is the **single source of truth** for the
camera → screen coordinate mapping. HLR and the footprint rasterizer
both go through it. The test in
`tests/backtest/test_projection_frame.py` pins that they agree to 1e-3 mm.

## See also

* [DATA_MODEL.md](DATA_MODEL.md) — schema details
* [API.md](API.md) — every endpoint
* [USER_FLOWS.md](USER_FLOWS.md) — what each route does step-by-step
* [AUDIT.md](AUDIT.md) — known reliability + optimisation work
