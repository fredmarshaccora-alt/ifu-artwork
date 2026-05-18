# Development

## Layout

```
serve.py             Flask server (no Werkzeug debug mode -- threaded=True
                     so OCCT background threads + HTTP requests can interleave)
build_viewer.py      Builds out/viewer.html from a giant r"""...""" template;
                     bakes the catalogue + GLB blobs inline
rebuild_html.py      Rebuilds viewer.html WITHOUT re-running HLR
                     (uses cached SVGs on disk).  Iterate JS by editing
                     build_viewer.py + running this.
t5_hlr_vector.py     OCCT HLR + footprint rasterizer
ifu/                 Python persistence + Onshape integration
tests/backtest/      pytest suites
docs/                You are here
```

## Run / iterate

### First-time bring-up

```bash
# 1. Install dependencies (cadquery-ocp brings most of OCCT)
pip install cadquery flask requests trimesh opencv-python numpy pillow \
            ezdxf python-dotenv

# 2. Configure Onshape creds in onshape-analytics/.env
#    (the OnshapeClient is imported from that sibling repo)
#    ONSHAPE_ACCESS_KEY=...
#    ONSHAPE_SECRET_KEY=...

# 3. First-time build of viewer.html.  This runs HLR on every bundled
#    source -- presto takes ~30s, contesa takes ~2min, siderail is
#    quick.  Only needed once; subsequent JS-only edits use
#    rebuild_html.py.
python build_viewer.py

# 4. Start the server
python serve.py
```

Then open <http://localhost:5000>.

### Iterating on the JS / UI

```bash
# Edit build_viewer.py (the JS is inside a Python r-string template)
python rebuild_html.py            # rebuilds out/viewer.html in ~1s
# Hard-reload the browser (Ctrl+Shift+R)
```

`rebuild_html.py` skips HLR entirely. It scrapes the existing baked SVGs
on disk to reconstruct the catalogue and re-emits `viewer.html` with the
new JS. **Don't** run `build_viewer.py` for JS-only changes — that path
re-runs HLR on every bundled source (~3 minutes total).

### Iterating on the Python server

```bash
# Edit serve.py / ifu/*.py
# Stop the server (Ctrl+C) and restart it -- Flask debug mode is off,
# there's no auto-reload (intentional; OCCT can't be re-loaded mid-run).
python serve.py
```

## Testing

### Test tiers

The `pytest_collection_modifyitems` hook in `tests/backtest/conftest.py`
auto-marks tests by filename:

| tier | naming | marker | expectation |
|---|---|---|---|
| unit | anything not `test_e2e_`, `test_integration_`, `test_api_*` | `unit` | pure Python, no STEP, no server |
| integration | `test_api_*`, `test_hlr*`, `test_integration_*`, `test_step_tree*`, `test_footprint*`, `test_tagging*` | `integration` | needs a STEP file loaded; may hit the live server |
| e2e | `test_e2e_*` | `e2e` | needs the Flask server **and** a Playwright browser |

### Run subsets

```bash
# Unit only (fast)
python -m pytest tests/backtest/ -m unit -q

# Integration (server must be running at 127.0.0.1:5000)
python -m pytest tests/backtest/ -m integration -q

# Full e2e (server + Playwright; ~5-10 min)
python -m pytest tests/backtest/test_e2e_*.py -q

# Single file with logs
python -m pytest tests/backtest/test_e2e_variant_strip.py -v

# Single test
python -m pytest tests/backtest/test_e2e_variant_strip.py::test_plus_card_creates_new_variant -v
```

### Test fixtures

`tests/backtest/conftest.py`:

* `step_root`, `siderail_step`, `presto_step`, `contesa_step` — STEP
  paths with `pytest.skip` if absent
* `server_url` — `http://127.0.0.1:5000` (set `IFU_SERVER=...` to
  override).  Skips when unreachable.
* `playwright_browser` — fresh Chromium per test (function-scoped on
  purpose; the 26 MB viewer.html accumulates memory across tests).
* `page` — new context + page; navigates to `/?dbg=1` and waits for
  `#file-sel` to populate before yielding.

### Common gotchas

* **Server already running**: e2e tests assume a *single* live server
  on `:5000`. Kill any leftover server before starting tests, or set
  `IFU_SERVER` to a fresh port.
* **Tests appear to pass alone but fail in suite**: most often the
  Playwright fixture's `wait_for_function('#file-sel options > 0')`
  is timing out because the page is still being parsed. The page is
  ~26 MB so first parse can take 3-5 s on a busy machine.
* **HLR cache pollution between tests**: tests that hit `/api/render`
  populate `_RENDER_CACHE` and `_FOOT_CACHE` per (file_id, vd_key,
  focal_key). Cleanup-on-failure isn't comprehensive — restart the
  server periodically if you notice odd cache behaviour.

## Debugging

### Server log overlay

In any editor view, click the **📡 log** button in the header. A
pinned overlay polls `/api/debug/log` every 1.5s and shows the
rolling structured-event buffer:

```
[20:33:13] req  POST /api/render source_id=basic_4_motor_chair_c0319b
[20:33:13] info render.start vd=(0.73,0.456,0.51) mesh_defl=1.5
[20:33:17] ok   render.hlr_done parts=77 parts_with_geom=77 polylines=45100
[20:33:17] ok   render.done svg_kb=2515 total_sec=2.27
```

Append `?dbg=1` to the URL to also enable the client-side perf HUD
in the top-right (timings per pipeline step on every applyHighlights).

### Direct API probes

```bash
# Healthcheck + loaded source list
curl -s http://127.0.0.1:5000/api/healthz

# Inspect a saved figure
curl -s http://127.0.0.1:5000/api/figures/<fid> | python -m json.tool

# Force a render at a specific camera
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"file_id":"siderail","eye":[1000,1000,800],"target":[0,0,0]}' \
  http://127.0.0.1:5000/api/render -o /tmp/render.svg \
  -w 'status: %{http_code}  polylines: %header{X-Render-Polylines}\n'

# Pull the structured log
curl -s 'http://127.0.0.1:5000/api/debug/log?since=400' | python -m json.tool
```

### Resetting state

```bash
# Wipe all projects / views / figures (DO NOT do this on real work)
rm -rf out/projects/* out/views/* out/figures/* out/sources/dynamic.json

# Restart server -- the boot-time migration will produce a clean tree
python serve.py
```

## Git workflow

```bash
git status
git log --oneline -10                # see recent work
git checkout -b feature/<thing>      # branch off
# ... edit, test, commit, etc. ...
git push -u origin feature/<thing>   # push to origin (which is GitHub)
```

This repository has 30+ uncommitted-to-origin commits on `main`. Push
when you're ready: `git push`.

## See also

* [ARCHITECTURE.md](ARCHITECTURE.md) — what each file does
* [API.md](API.md) — endpoint reference
* [AUDIT.md](AUDIT.md) — known gaps + improvement work
