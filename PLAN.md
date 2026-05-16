# IFU Artwork — Productisation Plan

A phased plan to take the working prototype to an internal tool and eventually
a sellable product, with hard test gates between phases and a regression
backtest covering every bug encountered during prototype development.

---

## 0. Strategic decisions

### Hosting

| Layer | Decision | Rationale |
|---|---|---|
| Frontend (React) | **Vercel** (free → Pro $20/mo) | Static deploy from this repo, preview URLs per PR, zero ops |
| Backend (FastAPI + OCCT) | **Fly.io** ($0–10/mo single VM) | Persistent process, ~1 GB STEP cache stays in memory, no 60 s cap, scale-to-zero when idle |
| Database | **Supabase** (free tier) | Managed Postgres + auth + storage in one product, RLS for multi-tenant |
| Object storage | **Cloudflare R2** or Supabase Storage | Zero-egress; cheaper than S3 |
| Render worker queue | **RQ + Redis** (Fly.io Upstash add-on) | Simple, Python-native; upgrade to Celery only if scale demands |
| Observability | **Sentry** (free tier) + Fly.io logs | Catch user errors; render queue depth on Fly metrics |

**Total infra at launch: $0–25/month**. Scales linearly.

Vercel functions are **not** used for HLR — they cap at 60 s and lose
state between invocations. They can host the static frontend and proxy
auth, but the OCCT pipeline lives on Fly.

### Stack

| Concern | Pick |
|---|---|
| Frontend framework | React 18 + Vite + TypeScript |
| 3D | three.js + @react-three/fiber + drei helpers |
| 2D | Raw SVG inside React (we already know the shape) |
| State | Zustand (lighter than Redux, simpler than RTK) |
| Routing | TanStack Router (typed) |
| API | FastAPI + Pydantic v2 |
| DB | PostgreSQL 15 via SQLAlchemy 2 + Alembic |
| Auth | Supabase Auth (email / Google / Microsoft SSO) |
| Tests | Vitest (FE), pytest (BE), Playwright (E2E) |
| CI | GitHub Actions |

---

## 1. The backtest (regression harness)

Every bug we hit in the prototype becomes a test in `tests/backtest/`. A
phase can only ship when all backtests pass. New regressions found later
get added to the harness.

### Catalogued bugs (issues #1–25)

Each test below maps to a specific bug we encountered. The harness must
gate every phase.

| # | Bug | Test (file::name) |
|---|---|---|
| 1 | OCCT projector Ax2 Z = -view_dir, camera on opposite side | `test_projector.py::test_ax2_z_matches_view_dir` |
| 2 | X-mirror band-aid broke for non-orthogonal view_dirs | `test_projector.py::test_no_x_mirror_needed` |
| 3 | Per-solid HLRBRep_Algo.Add stalled on Presto (O(N²)) | `test_hlr_perf.py::test_presto_render_under_180s` |
| 4 | `view_dir is not defined` after switching to {eye, target} | `test_api_render.py::test_eye_target_form` |
| 5 | Playwright `$` shortcut lost after page reload | `test_e2e_navigation.py::test_post_reload_shortcuts` |
| 6 | Python f-string `{...}` in JS template treated as placeholder | `test_html_emit.py::test_builds_without_format_errors` |
| 7 | JS string apostrophe `\\'` parse error | `test_html_emit.py::test_no_js_parse_errors` |
| 8 | STEP tree 126 leaves vs 138 cadquery solids (multi-body Parts) | `test_step_tree.py::test_leaf_solid_count_matches_cadquery` |
| 9 | precision=2 dedup left 30% duplicates, 2× SVG bloat | `test_dedup.py::test_precision_1_dedup_ratio` |
| 10 | UnicodeEncodeError ⚡ on Windows cp1252 stdout | `test_stdout.py::test_utf8_emoji_safe` |
| 11 | Silhouette layer wrong DOM level (outside scale(1,-1)) | `test_e2e_silhouette.py::test_layer_inside_scale_group` |
| 12 | `window.API_BASE` undefined (const doesn't attach to window) | `test_e2e_api.py::test_api_base_resolvable` |
| 13 | Footprint pixel bleed at part boundaries | `test_footprint.py::test_no_neighbour_pixel_leak` |
| 14 | `.part.highlight path` CSS bolded all internal features | `test_e2e_selection.py::test_internal_features_not_bolded` |
| 15 | Default stroke-width 0.7mm invisible on 2m model | `test_e2e_selection.py::test_default_stroke_width_visible` |
| 16 | Closed silhouette stroked through occluders | `test_e2e_silhouette.py::test_no_overdraw_on_occluder` |
| 17 | Mean-depth painter's algorithm wrong at hinge complexity | `test_zbuffer.py::test_per_pixel_z_correct_at_hinge` |
| 18 | Slider input fired applyHighlights 60×/s, 678-part DOM walk | `test_e2e_perf.py::test_slider_drag_does_not_walk_dom` |
| 19 | Rasterised hit-fill clicks landed on wrong part | `test_e2e_click.py::test_click_lands_on_clicked_part` |
| 20 | Convex hull oversized for concave parts (known trade-off) | `test_convex_hull.py::test_l_bracket_hull_documented` |
| 21 | Silhouette fetch never fired (window.API_BASE guard) | `test_e2e_api.py::test_silhouette_fetch_fires` |
| 22 | Bbox-tagging on Contesa mis-attributed edges | `test_tagging.py::test_contesa_edge_attribution` |
| 23 | Bold edge needed broken pieces under occluder | `test_footprint.py::test_occluded_part_multiple_contours` |
| 24 | Apply did different thing from live highlight | `test_e2e_apply.py::test_apply_matches_live_silhouette` |
| 25 | 3D viewer sketchy (no lighting, coarse GLB, up-axis hack) | `test_3d.py::test_3d_scene_quality` |

The backtest runs in CI on every PR and is the **only** way to merge to main.

### Backtest tiers

- **Unit tests** (#1, #2, #6, #7, #8, #9, #10, #17, #20, #22): fast (<1 s each)
- **Integration tests** (#3, #4, #12, #13, #21): need server + STEP files (~30 s each)
- **E2E tests** (#5, #11, #14, #15, #16, #18, #19, #23, #24, #25): full Playwright run

CI runs unit + integration on every push. E2E runs on PR + main only.

---

## 2. Phase plan

Each phase has acceptance tests **all of which must pass** to advance.

### Phase 0 — Baseline (today)

- ✅ Repo at https://github.com/fredmarshaccora-alt/ifu-artwork
- ✅ v0 commit pushed
- 🎯 Backtest harness written and baseline established

**Gate**: backtest catalogue covers every issue #1–25 with a runnable
test (even if some are red against v0 — those become Phase 1 work).

### Phase 1 — Stabilise the monolith (target: 2 weeks)

Goal: same UX, properly factored. Foundation for everything else.

1. Split `build_viewer.py` (~3000 lines) into:
   ```
   src/ifu/
   ├── sources.py        # SOURCES list, source loader
   ├── tree.py           # STEP / Onshape tree extraction
   ├── hlr.py            # run_hlr_per_solid, run_part_silhouettes, run_group_silhouette
   ├── raster.py         # compute_visible_footprints
   ├── svg_bake.py       # write_svg_parts + the merging pass
   ├── glb_export.py     # per-source GLB
   ├── catalogue.py      # save/load _catalogue.json
   └── html_emit.py      # bundle SVG/GLB/JS/CSS into viewer.html
   src/web/
   ├── api.py            # FastAPI (replaces Flask serve.py)
   ├── render_jobs.py    # render queue boundary (sync for now)
   └── static/           # built frontend
   src/frontend/
   ├── (TypeScript scaffolding only -- full migration in Phase 3)
   └── ...
   tests/
   ├── unit/
   ├── integration/
   ├── e2e/
   └── backtest/         # the regression catalogue
   ```
2. Move JS out of f-string template into a real `.ts` file built with Vite
3. Replace Flask with FastAPI (same endpoints, typed bodies)
4. Add `pyproject.toml`, `requirements.txt` → `requirements.in` + `pip-compile`
5. Set up GitHub Actions: lint (ruff + mypy + eslint), unit, integration
6. **Fix the 3D pane** (Phase 1.5, parallel):
   - AmbientLight + DirectionalLight + soft shadow
   - PBR material presets (matte plastic / aluminium / steel)
   - Proper orthographic camera with bounds calculated from source bbox
   - Grid + axis helpers + view-cube gizmo
   - Outline-shader selection feedback

**Phase 1 acceptance tests** (all must be green):

- [ ] `backtest/*` — every test in §1 above passes
- [ ] `unit/test_modules_import.py` — no module imports the legacy `build_viewer` (forces the split to be real)
- [ ] `integration/test_api_compatibility.py` — every Flask endpoint produces byte-identical output in FastAPI
- [ ] `e2e/test_smoke_3sources.py` — load each source, click a part, see closed silhouette + applied-styles list works
- [ ] `e2e/test_3d_quality.py` — 3D scene renders with shadows, materials, helpers visible
- [ ] CI green on `main`
- [ ] Coverage ≥ 60% on the new modules

### Phase 2 — Database + multi-user (target: 4 weeks)

Goal: state lives in the database, not in one user's browser. Multiple
people can use the tool.

1. Supabase project: Postgres + auth + RLS
2. SQLAlchemy + Alembic migrations for the domain model:
   ```
   users, projects, sources, source_revisions,
   figures, render_artifacts, audit_log
   ```
3. Migrate localStorage state to per-user records
4. Add Supabase Auth flow (email + Microsoft SSO)
5. Per-project access control (viewer / author / approver)

**Phase 2 acceptance tests**:

- [ ] All Phase 1 tests still green
- [ ] `integration/test_db_migrations.py` — migrations apply clean from empty
- [ ] `integration/test_rls.py` — User A cannot read User B's project
- [ ] `e2e/test_signup_login.py` — full auth flow with throwaway accounts
- [ ] `e2e/test_state_persistence.py` — apply a style, log out, log in, style still there
- [ ] `e2e/test_concurrent_users.py` — two users editing same project don't clobber each other (last-write-wins with toast)

### Phase 3 — Proper React editor (target: 6 weeks)

Goal: feels like a real product. Component-based, typed, undo/redo.

1. React + TypeScript scaffold under `src/frontend/`
2. Component tree:
   - `<Editor>` — top level, owns selection state
   - `<SourceTree>` — left sidebar with Onshape/STEP hierarchy
   - `<View3D>` — three.js scene
   - `<View2D>` — SVG viewer with pan/zoom/click
   - `<StylePanel>` — right sidebar with all the controls
   - `<AppliedStyles>` — the list we just added
   - `<Annotations>` — callout layer
3. Zustand stores: `editorStore`, `viewportStore`, `selectionStore`
4. Undo/redo via command pattern (every state mutation = a command)
5. Real-time presence via Supabase Realtime (just "X is viewing this figure")

**Phase 3 acceptance tests**:

- [ ] All previous tests still green
- [ ] `unit/store/*` — store reducers covered ≥ 90%
- [ ] `e2e/test_undo_redo.py` — every command in the editor undoes/redoes correctly
- [ ] `e2e/test_react_smoke.py` — full Playwright trace through editor, no console errors
- [ ] Lighthouse perf score ≥ 85 on a fresh load

### Phase 4 — Render pipeline (target: 3 weeks)

Goal: render-heavy work doesn't block the UI; results are cached forever.

1. RQ worker on Fly.io: subscribes to `render_queue`
2. Job types: `render_view`, `compute_footprint`, `export_svg`, `export_pdf`, `export_png`, `export_batch`
3. Frontend: render-status pill, progress bar, cancel button
4. PDF export via Inkscape headless (or Cairo for pure-Python)
5. Per-render cache keyed by `(source_revision_id, camera, mode, detail_level)`

**Phase 4 acceptance tests**:

- [ ] All previous tests still green
- [ ] `integration/test_render_queue.py` — job submitted, worker picks up, result cached
- [ ] `integration/test_pdf_export.py` — SVG → PDF roundtrip preserves all paths
- [ ] `e2e/test_batch_export.py` — export all figures in a project as one PDF bundle
- [ ] Queue depth metric exposed and accurate

### Phase 5 — Sellable product (target: 6-8 weeks)

Goal: a stranger can sign up and pay for this without our help.

1. Onboarding flow (sample project, guided tour)
2. Project templates (medical device IFU, generic technical doc)
3. Brand presets (colours, line weights, fonts)
4. Stripe billing (per-seat per-month, free tier + paid)
5. Marketing site at `accora-artwork.com` (or similar) on Vercel
6. Public docs at `docs.accora-artwork.com`

**Phase 5 acceptance tests**:

- [ ] All previous tests still green
- [ ] `e2e/test_signup_to_first_export.py` — fresh user creates account, uploads STEP, exports SVG within 10 minutes
- [ ] `e2e/test_billing.py` — Stripe test mode: signup → upgrade → downgrade → cancel
- [ ] Privacy policy + terms of service published
- [ ] Sentry error rate < 1% on production traffic

---

## 3. Execution rules

1. **No phase advance without all gates green.** CI enforces this — `main` is protected.
2. **Every bug fix becomes a backtest** — added to `tests/backtest/` in the same PR as the fix.
3. **Trunk-based development** — feature branches off `main`, PRs reviewed by self (or pair when available), merged with squash.
4. **Conventional commits** — `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`.
5. **One environment** at first — production on Fly + Vercel from `main`. Stage env added in Phase 2.
6. **No premature optimisation** — RQ before Celery, single-node before HA, raw SVG before WebGL acceleration. Ship the simplest thing that passes the gates.

---

## 4. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| OCCT Python wheel doesn't build on Fly Linux | Medium | High | Test in Phase 1.0 with the cadquery Docker base image |
| Customer concerned about uploading CAD to cloud | Medium | High | Phase 5: offer on-prem Tauri desktop build |
| Onshape API rate limits during heavy use | Low | Medium | Cache aggressively, batch tree fetches |
| Render queue spikes on batch export | Medium | Medium | Auto-scale workers on Fly; per-user concurrency cap |
| Browser SVG perf on a 700-part assembly | Already known | Medium | Already solved via path merging (98% DOM node reduction) |
| GDPR / regulatory data residency | Low until external customers | Medium | Supabase EU region, document data flow |

---

## 5. Backtest harness — how to run

```bash
# Quick unit tests (no server needed)
pytest tests/backtest -m "unit" -q

# Full backtest (boots server, plays the bug scenarios)
pytest tests/backtest -q

# Just one category
pytest tests/backtest -k "silhouette"
```

CI runs `pytest tests/backtest -q` on every push.

---

## 6. Today's next steps

In order:

1. Build `tests/backtest/` directory with one test file per bug catalogue entry
2. Run against v0 to establish baseline — record red/green per test
3. Phase 1 starts the day every red test on v0 is either passing or has a written ticket
