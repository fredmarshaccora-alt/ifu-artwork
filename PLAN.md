# IFU Artwork — Plan

Local-first IFU artwork tool that started as a prototype to replace
hand-drawn Composer illustrations.  Used by a small Accora team for
internal IFU/PRS figure generation; not productised.

## Operating model

- Everything runs on the user's machine via `python serve.py`.
- Open `http://localhost:5000` in any browser.
- STEP files live where the user already keeps them (gitignored).
- GitHub is for version control + bug-tracking + backup only — not
  deployment infrastructure.

## What's done (v1.0)

| | |
|---|---|
| Pipeline | STEP -> per-part HLR -> tagged SVG -> single-file viewer |
| Sources | Folding siderail, Presto, Contesa (FL8) |
| Trees | Onshape API or STEP product hierarchy fallback |
| 2D viewer | Pan/zoom, click-select, layer toggles, per-part styling, applied-styles list |
| 3D viewer | Z-locked orbit, PBR materials, grid + axes helpers, selection-dim |
| Live render | `/api/render` + `/api/part_silhouettes` + `/api/part_footprints` |
| Selection | Click-anywhere via convex-hull layer; ctrl-click multi-select; group/per-part fill |
| Footprints | Per-pixel z-buffer + morph-clean + CCOMP contours (visible+holes) |
| Backtests | 77 tests, every prototype bug protected from regression |
| Repo | https://github.com/fredmarshaccora-alt/ifu-artwork |

## What this plan is for

A focused list of next improvements driven by user feedback during real
use, in priority order.  Every item ships in this rhythm:

1. Write a backtest that fails when the issue exists
2. Ship the fix
3. Backtest passes; commit; push
4. Move on

No phase gates, no productisation track, no cloud build-out.  Just:
make-it-better, with regression protection.

## Active priorities (May 2026)

### P1 — Easy 3D <-> 2D sync (the big one)

Composer/Cadasio mental model: every state (camera + selection +
styling) is a named view; swapping between 3D pose and 2D drawing
is one click; coupled in Split mode so they update each other.

Borrowed from Composer:
- **Saved views** = camera + selection + applied styles + annotations
  (we have camera; extend to capture the whole state)
- **Marker styles** = reusable per-part style sets you can apply by
  name (we have per-part styles; promote them to named sets)
- **Snapshot workflow** = "create view from current 3D pose"

Borrowed from Cadasio:
- **Live coupled cameras** in side-by-side view: orbit 3D, see the
  2D preview track via lightweight render
- **Render quality tier**: draft (instant) vs publication (slow)

Concrete deliverables for P1:
- **P1.a** — "Snap 3D to this view" button on every 2D view preset
- **P1.b** — Coupled-camera toggle in Split mode: orbiting 3D updates
  a "draft direction" pill in the 2D pane with one-click full render
- **P1.c** — Named views capture: camera + selection + applied styles
  + visible-layer toggles, all restored on click

### P2 — Detail on zoom (perf)

Today the SVG bakes one detail level per source.  When zoomed out we
push more polylines than we need; when zoomed in we'd like FINER detail.

User idea: **render just the visible window**, not the whole bed.
Implementation:

- **P2.a** — `/api/render_region` endpoint: take `{file_id, view_dir,
  bbox_uv}` in 2D coords, filter parts whose 3D bbox projects inside
  bbox_uv, run HLR on just those at chosen detail, return SVG tile.
- **P2.b** — Client LOD swap: when the user zooms past, say, 3x the
  base scale, auto-request a high-detail tile of the current visible
  window and overlay it; pan/zoom out drops back to the base SVG.
- **P2.c** — Two-tier detail in the baked SVG: a coarse layer (current
  default) for the overview, finer per-source override available on
  demand.

P2 is bigger; ship P1 first, see if it removes the pain, then revisit.

### P3 — Smaller quality-of-life

- Keyboard shortcuts (1/2/3 for layout, F to fit, R to reset 3D, Esc to clear)
- "Recent views" list (last 5 angles you actually rendered)
- Drag-and-drop new STEP file = new source registered + meshed in
  background
- Export current 2D view as PNG at chosen DPI (we already have SVG)

## Backtest tiers (current)

```bash
pytest tests/backtest -m "unit" -q           # ~10s, run on every change
pytest tests/backtest -m "integration" -q    # ~7min, needs STEP files + server
pytest tests/backtest -m "e2e" -q            # ~1min, needs server + Playwright
```

Total: 77 tests covering every issue we hit in the prototype.

## Rule of thumb

A change should land with a backtest that protects it.  If the bug is
hard to repro automatically, file it as a known limitation in
`tests/backtest/` with `@pytest.mark.xfail` so future-us has the
context.  Don't ship features without the test scaffolding -- we
already proved (via the v0 prototype) what happens when you don't.
