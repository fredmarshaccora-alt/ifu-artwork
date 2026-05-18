# IFU artwork

Local Python + Flask + OCCT + three.js tool that turns Onshape /
STEP assemblies into publication-clean line-art illustrations for
Accora's Instructions For Use (IFU) documents.

## What it does

You import a CAD model from Onshape (or pick one of the bundled
demo assemblies), pose a camera angle in the live 3D viewer, and
the server runs **analytical hidden-line removal** on the
B-rep to produce a true-vector SVG drawing. You then click parts
to **highlight** them with preset styles and stack callouts.

Output is a per-part tagged SVG file you can drop straight into an
IFU document; no rasterisation, infinite zoom, edges classified by
category (silhouette / sharp / smooth / hidden).

## Quick start

```bash
# 1. install python deps (cadquery-ocp, flask, requests, trimesh, opencv, etc.)
pip install -r requirements.txt    # (or whatever your env uses)

# 2. start the local server
python serve.py

# 3. open http://localhost:5000 in a browser
```

The server boots the bundled STEP files into memory (~30 s the first
time), loads any imported Onshape sources, and serves the UI at the
root path.

## Workflow

1. **Home** — project tiles. Click `+ New project`.
2. **New project modal** — name + either paste an Onshape URL
   (which triggers a STEP translation + download in the background)
   or pick one of the bundled demo assemblies.
3. **Project workspace** — Views grid. Each View is a saved camera
   angle for the project's model. Click `+ New view`.
4. **Editor** opens on a view's first figure. The 2D pane shows the
   HLR drawing; the 3D pane shows the same model interactively.
   Drag the splitter to resize.
5. **Highlight parts** — click parts on either pane. Pick a preset
   style from the right sidebar (Highlight / Caution / Info / Outline
   only / Subtle). Changes auto-save.
6. **Add variants** — the left sidebar has a strip of thumbnail
   cards, one per highlight variant of the current view, plus a `+`
   card. Each variant is a separate Figure under the same View.
7. **Export** — `export SVG` in the header writes the styled,
   annotated drawing to disk.

## Documentation

See `docs/`:

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — system layers, where the
  3D viewer, HLR pipeline, router, and storage live
- [DATA_MODEL.md](docs/DATA_MODEL.md) — Project → View → Figure +
  Source schemas, persistence layout on disk
- [API.md](docs/API.md) — every HTTP endpoint, request/response shape
- [USER_FLOWS.md](docs/USER_FLOWS.md) — the canonical workflows
  end-to-end (import, create view, switch variants, export)
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — how to build the viewer
  HTML, run the test suite, debug a render
- [AUDIT.md](docs/AUDIT.md) — known reliability gaps + the plan for
  hardening them

## Testing

```bash
python -m pytest tests/backtest/test_e2e_*.py -q
```

The full e2e suite runs against a live server on `127.0.0.1:5000`
and takes ~5–10 min. Unit + API tests (subsets) are faster:

```bash
python -m pytest tests/backtest/test_projector.py tests/backtest/test_footprint.py -q
```

## Layout

```
serve.py             Flask server + the HLR / footprint endpoints
build_viewer.py      Builds out/viewer.html (single self-contained file)
rebuild_html.py      Rebuilds viewer.html without re-running HLR
t5_hlr_vector.py     OCCT HLR pipeline + footprint rasterizer
ifu/                 Python persistence + Onshape integration
  config.py            SOURCES, VIEWS, OUT
  sources.py           dynamic-source registry (imports)
  projects.py          Project CRUD
  views.py             View CRUD + migration
  figures.py           Figure CRUD + thumbnails
  onshape_fetch.py     Onshape URL parsing, STEP translation, configurations
  onshape_client.py    auth + transport
  ...
out/                 Runtime artefacts (figures, views, sources, imports, viewer.html)
tests/backtest/      pytest suites (unit + API + e2e)
docs/                Architecture + flows + audit notes
```
