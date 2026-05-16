# Architecture

How IFU Artwork is shaped as a real product.  Companion to [DESIGN.md]
(the visual / workflow spec) and [PLAN.md] (the running build journal).

---

## 1. The four-screen model

Most workspace apps with this complexity converge on the same shape:

```
            ┌─────────┐
            │  Home   │  pick a project, or create one
            └────┬────┘
                 ▼
         ┌───────────────┐
         │   Project     │  list of figures in this project
         │   workspace   │
         └───────┬───────┘
                 ▼
          ┌─────────────┐
          │   Editor    │  one figure at a time, full toolset
          └─────────────┘

  Settings ⚙  is reachable from any screen; it's a sibling, not a child.
```

Each screen has ONE clear job.  The current monolithic viewer.html
tries to do all three at once.  Splitting clears the cognitive load
and gives every workflow a natural home.

### Screen 1 — Home (`#/`)

The "what am I working on?" landing.

- Grid of project cards (thumbnail = first figure's outline, name,
  figure count, "⬆N CAD updates available" badge)
- Recent figures strip (last 5 edited, across all projects)
- **+ New project** button (modal: name, description, initial source)
- Top-right: **Settings ⚙**

### Screen 2 — Project workspace (`#/project/<id>`)

Everything about ONE project.

- Breadcrumb: `Home > <project name>`
- Source binding bar: which sources this project uses + revision
  status + refresh-from-Onshape button
- Grid of figure cards (thumbnail + name + revision badge + `⋯`
  for rename/duplicate/delete)
- **+ New figure** modal: pick source, pick standard view or "use
  current 3D pose", enter name -> drops into editor

### Screen 3 — Editor (`#/project/<pid>/figure/<fid>`)

The current editor, just chrome-cleaned.

- Breadcrumb: `Home > <project> > <figure name>`
- Three-column: Tree (collapsible) | 3D/2D canvas | Properties
- Top toolbar: 2D / Split / 3D, layer toggles, save indicator, export
- Bottom status bar: revision binding + "view changes" + "update to Rnn"
- All the existing tools (selection, styling, callouts, applied-styles
  list, hi-detail render) live here unchanged

### Screen 4 — Settings (`#/settings`)

App-level, not per-project.

- **General**: default render detail, default stroke/fill defaults
- **Sources**: edit configured STEP/Onshape sources, add new
- **Onshape**: credentials, connected account, "refresh"
- **Storage**: where projects live on disk, "open in Explorer"
- **About**: version, GitHub link

---

## 2. Technical architecture

We are NOT moving to React for this.  Vanilla JS + a 50-line hash
router is enough and keeps the build chain at zero dependencies.  The
backend is unchanged; only the frontend organisation changes.

### Layers

```
┌────────────────────────────────────────────────────────────────┐
│ Browser (single page, viewer.html)                             │
│                                                                │
│  Hash router (#/home, #/project/X, #/project/X/figure/Y, ...)  │
│      │                                                         │
│      ▼                                                         │
│  Screen modules: home, project, editor, settings               │
│  Each: mount(container) / unmount() lifecycle                  │
│      │                                                         │
│      ▼                                                         │
│  Reusable components: ProjectCard, FigureCard, Breadcrumb,     │
│  Toolbar, StatusBar, PropertyPanel                             │
│      │                                                         │
│      ▼                                                         │
│  AppState + storage facade                                     │
└──────────────┬─────────────────────────────────────────────────┘
               │ fetch(API_BASE + ...)
               ▼
┌────────────────────────────────────────────────────────────────┐
│ Flask server (serve.py)                                        │
│                                                                │
│  Routes (threaded; OCCT-touching ones serialised by _HLR_LOCK):│
│    /                                  serves viewer.html       │
│    /api/healthz                       cheap                    │
│    /api/sources                       cheap                    │
│    /api/projects[/*]                  cheap                    │
│    /api/figures[/*]                   cheap                    │
│    /api/sources/<id>/versions[/*]     cheap (no Onshape) / I/O │
│    /api/settings                      cheap        << NEW F.1  │
│    /api/figures/<id>/thumbnail        OCCT   <<    NEW F.7    │
│    /api/render*                       OCCT  (serialised)       │
│    /api/part_silhouettes              OCCT  (serialised)       │
│    /api/part_footprints               OCCT  (serialised)       │
│    /api/render_region                 OCCT  (serialised)       │
│      │                                                         │
│      ▼                                                         │
│  ifu/ package (modules):                                       │
│    config       sources + view presets                         │
│    mesh         per-solid triangulation                        │
│    glb          GLB export                                     │
│    step_tree    STEP product hierarchy                         │
│    onshape_*    Onshape API client + tree fetch                │
│    svg_bake     full pipeline orchestrator                     │
│    catalogue    persist/restore catalogue.json                 │
│    figures      figure CRUD (Phase A)                          │
│    projects     project CRUD (Phase B)                         │
│    revisions    Onshape Versions cache (Phase C)               │
│    settings     app-level settings   << NEW F.1                │
│                                                                │
│  t5_hlr_vector.py:                                             │
│    HLR + footprint rasterizer + per-part silhouette helpers    │
└────────────────────────────────────────────────────────────────┘
```

### On-disk layout

```
out/
  viewer.html              the SPA
  _catalogue.json          cached source metadata
  <source_id>__<view>.svg  baked HLR per (source, view)
  figures/<id>.json        per-figure state
  projects/<id>.json       per-project state
  revisions/<source>.json  cached Onshape Versions list
  thumbnails/<fig>.png     server-rendered figure previews   << NEW
  settings.json            app-level settings                << NEW
```

### State management

One module-level `AppState` object replaces today's scattered globals:

```javascript
const AppState = {
  // Navigation
  route: '#/',
  routeParams: {},

  // Active selection at each level (mirrors route, but durable across
  // route changes)
  currentProjectId: null,
  currentFigureId: null,

  // Cached objects (fetched on demand, invalidated by mutations)
  projects: [],
  figures: [],         // figures in CURRENT project (or all if home)
  sources: [],
  settings: {},

  // Editor scratch (per-figure; only meaningful in editor screen)
  editor: {
    selection: new Set(),
    styles: {},
    layers: {},
    detail: 'normal',
    dirty: false,      // unsaved changes flag for the auto-save indicator
  },
};
```

Mutations go through tiny named actions (`setRoute`, `selectFigure`,
`toggleHighlight`) so a future audit log or undo stack has a stable
API surface to plug into.

### Routing

Vanilla JS, ~50 lines.  Hash-based to avoid server-side rewriting.

```javascript
const routes = [
  { pattern: /^#\/$/,                            mount: HomeScreen },
  { pattern: /^#\/project\/([^/]+)$/,            mount: ProjectScreen },
  { pattern: /^#\/project\/([^/]+)\/figure\/([^/]+)$/,
                                                  mount: EditorScreen },
  { pattern: /^#\/settings$/,                    mount: SettingsScreen },
];

window.addEventListener('hashchange', renderCurrentRoute);
```

Each screen module exports `mount(container, params) -> teardownFn`.

### Component conventions

Vanilla DOM helpers, no framework:

```javascript
function ProjectCard({ project, onClick }) {
  return h('div.card', { onClick }, [
    h('img.thumb', { src: thumbUrl(project.id) }),
    h('div.name', project.name),
    h('div.meta', `${project.figure_ids.length} figures`),
  ]);
}
```

(`h(tag, attrs, children)` is a 20-line hyperscript helper we'll ship
in F.2.)

### Design tokens

One CSS file with custom properties.  Replaces ad-hoc inline styles.

```css
:root {
  /* spacing scale (8px grid) */
  --space-1: 4px;  --space-2: 8px;  --space-3: 12px;
  --space-4: 16px; --space-5: 24px; --space-6: 32px;
  /* type scale */
  --type-1: 11px;  /* meta */
  --type-2: 13px;  /* body */
  --type-3: 15px;  /* emphasis */
  --type-4: 18px;  /* heading */
  --type-5: 24px;  /* page title */
  /* brand */
  --accora-teal: #00836a;
  --accora-pale: #cce6e0;
  --line: #d4d4d8;
  --muted: #71717a;
  --bg: #fafafa;
  --bg-canvas: #fff;
}
```

---

## 3. What survives the rewrite

Everything in `ifu/` and `t5_hlr_vector.py` is unchanged.  Every
backtest in `tests/backtest/` still applies — the URLs being tested
don't change.  The only thing being restructured is how the browser
renders the response.

| Survives | Touched | New |
|---|---|---|
| `ifu/*`, `t5_hlr_vector.py`, `serve.py` (existing endpoints) | `build_viewer.py` (frontend gets reorganised) | `ifu/settings.py`, `screens/*.js`, `router.js`, `components/*.js`, `tokens.css`, `/api/settings`, `/api/figures/<id>/thumbnail` |

---

## 4. Build sequence

Each phase ships independently.  Backtests run between phases.

| F.x | Goal | Effort | Ship-able alone? |
|---|---|---|---|
| **F.1** | `ifu/settings.py` + `/api/settings` endpoints + tests | half day | yes (invisible) |
| **F.2** | Hash router infra; AppState; screen-mount lifecycle (no screens migrated yet) | half day | yes (invisible) |
| **F.3** | Home screen (projects grid + recents) | 1 day | yes (route in works; editor still default) |
| **F.4** | Project workspace screen (figure grid) | 1 day | yes |
| **F.5** | Re-skin Editor with breadcrumb + cleaner chrome | 1-2 days | yes |
| **F.6** | Settings screen | half day | yes |
| **F.7** | Server-side figure thumbnails | 1 day | yes |
| **F.8** | Design tokens + spacing + transitions | 1 day | yes |

Cumulative ~6-8 days of focused work.  Today is F.1 + F.2.

---

## 5. Backwards compatibility

During the migration:
- Old `/` route renders the current viewer.html (default if no hash).
- New routes (`#/...`) render the new screens.
- Both share the same backend.

Once F.5 lands (Editor screen migrated), the old monolith inside
viewer.html is retired and `/` redirects to `#/`.

No data migration needed — figures, projects, revisions, settings
are all already disk-backed JSON files.

---

## 6. What this DOESN'T solve

- **Multi-user**: still single-user local-only.  Multi-user would
  need shared storage + auth.  Not on the roadmap.
- **Cloud hosting**: still local-first.  Cloud needs the Phase 2
  Supabase work from the old PLAN that we explicitly cut.
- **Native desktop window**: still a browser tab unless we add the
  pywebview wrapper (Option A from earlier).  Trivial post-F.8.
- **D-full revision diff**: still deferred.  Manual bind still works.
