# Data model

All persistence is **JSON files on disk** under `out/`. No database, no
schema migrations. Field shape is permissive: extra fields round-trip
untouched so we can add e.g. `bound_revision` without breaking existing
records.

## Hierarchy

```
Project   one CAD model + the work authored against it
   │
   ├──< View   a saved camera angle for that model
   │     │
   │     └──< Figure   a specific selection + styles within that view
   │
   └── owns the Source that the views render against
```

Pre-Phase 3 data had figures with their own cameras and no view layer.
The boot-time migration in `views.py::migrate_existing_figures`
spawns a 1:1 View per orphan figure so old data flows into the new model
without user intervention.

---

## Project — `out/projects/<id>.json`

```json
{
  "id": "281cf2da3885",
  "name": "Chair IFU R02",
  "description": "Powered 4-motor chair, technical illustrations for the IFU.",
  "primary_source_id": "basic_4_motor_chair_c0319b",
  "onshape_ids": { "did": "...", "wid": "...", "eid": "...", "wv": "w" },
  "figure_ids": ["...", "..."],
  "created_at": "2026-05-17T19:32:50Z",
  "updated_at": "2026-05-18T10:11:02Z"
}
```

* `primary_source_id` is set when the project is created via the
  new-project modal (either from an Onshape import or by picking an
  existing source). Views inherit it by default.
* `onshape_ids` is recorded for Onshape-sourced projects so we can
  re-bind to a specific Version later.
* `figure_ids` is the legacy direct-attach list, still maintained for
  backwards-compatible queries.

---

## View — `out/views/<id>.json` (+ `<id>.png` thumbnail)

```json
{
  "id": "5fa6def9e18f",
  "project_id": "281cf2da3885",
  "source_id": "basic_4_motor_chair_c0319b",
  "name": "Iso, front-right",
  "camera": {
    "eye":    [1112, -1221, -80],
    "target": [0, 376, 102],
    "up_axis": "Z"
  },
  "configuration": null,
  "figure_ids": ["...", "..."],
  "created_at": "2026-05-17T20:02:19Z",
  "updated_at": "2026-05-17T20:08:04Z"
}
```

* `camera` is the only thing a View "owns". Every figure under this
  view inherits this camera (a figure can override on its own
  record but the variant-strip UX treats them as siblings).
* `configuration` is the Onshape configuration string applied at the
  STEP-translation level (e.g. `Rise=Up;Tilt=Down`). Different views
  can render the same source at different configurations.
* `figure_ids` is the ordered list of variants shown in the editor's
  left-sidebar strip.
* Thumbnail is a 320×240 PNG written by the client to
  `/api/views/<id>/thumbnail` after each successful save.

---

## Figure — `out/figures/<id>.json` (+ `<id>.png` thumbnail)

```json
{
  "id": "74ca39cbc191",
  "name": "Motors highlighted",
  "project_id": "281cf2da3885",
  "view_id":    "5fa6def9e18f",
  "source_id":  "basic_4_motor_chair_c0319b",
  "camera": {
    "eye":    [-1783, 179, 878],
    "target": [0, 376, 102],
    "up_axis": "Z"
  },
  "configuration": null,
  "selection":      [12, 24, 36],
  "styles_per_part": {
    "12": { "stroke": "#00836a", "width": 4.0, "fillOn": true,
             "fillColor": "#cce6e0", "fillAlpha": 0.35 }
  },
  "layers_on": {
    "outline_v": true, "sharp_v": true, "smooth_v": false,
    "hidden_outline": false, "hidden_sharp": false
  },
  "detail": "normal",
  "annotations": [],
  "notes": "",
  "bound_revision": null,
  "created_at": "2026-05-17T20:08:04Z",
  "updated_at": "2026-05-18T08:42:11Z"
}
```

* `selection` is the part-index set being highlighted, persisted as a
  sorted array (Sets don't JSON-serialise).
* `styles_per_part` maps each highlighted part idx → the preset that
  was applied. We store the resolved style dict, not the preset id,
  so renaming or removing presets doesn't break old figures.
* `camera` here is what the figure remembers; the auto-render path
  reads from here. New figures created under a view inherit the
  view's camera, but the user can save a custom angle on this
  figure that won't drift back to the view's default.
* `bound_revision` is the Phase-D Onshape Version binding (still
  WIP). For now, it's `null` on almost every figure.
* Thumbnail is a 320×240 PNG written by the client after each save.

---

## Source — static and dynamic

Two flavours:

### Static — `ifu/config.py` tuple

Built-in demos. Each entry:

```python
("siderail", "Folding siderail", Path(...), {"mesh_defl": 0.8, ...}, None, None)
```

Tuple positions: `(id, label, step_path, hlr_kwargs, pre_rotation, onshape_ids)`.

### Dynamic — `out/sources/dynamic.json`

Onshape imports persist here as a flat list:

```json
{
  "sources": [{
    "id": "basic_4_motor_chair_c0319b",
    "label": "Basic 4 motor chair",
    "step_path": "...\\out\\imports\\basic_4_motor_chair_c0319b.step",
    "hlr_kwargs": { "mesh_defl": 1.5, "sample_defl": 1.0 },
    "pre_rotation": null,
    "onshape_ids": { "did": "...", "wid": "...", "eid": "...", "wv": "w" },
    "origin": "dynamic",
    "created_at": "...",
    "updated_at": "...",
    "imported_from": "https://cad.onshape.com/documents/.../w/.../e/..."
  }]
}
```

The merged view (`sources_store.all_sources()`) is what `/api/sources`
exposes to the UI — static first (declaration order), dynamic newest-first.

---

## On-disk layout

```
out/
├── viewer.html             ← the built UI (built artefact, not in git)
├── projects/<id>.json
├── views/<id>.json
├── views/<id>.png          ← view thumbnail (captured client-side)
├── figures/<id>.json
├── figures/<id>.png        ← figure thumbnail
├── sources/dynamic.json    ← imported Onshape sources
├── imports/<sid>.step      ← raw STEP files from /api/onshape/import
├── revisions/<sid>.json    ← cached Onshape Versions list per source
├── settings.json           ← user app-level settings
└── _live_<sid>.svg         ← scratch SVG written by /api/render
```

## Safety properties

* **File names are sanitised** before path construction — anything
  outside `[A-Za-z0-9_-]` is stripped. URL-supplied ids can't escape
  the directory.
* **Permissive schema** — readers tolerate missing fields. The figure
  loader fills defaults (`new_figure()`); the project loader is happy
  with absent `view_ids` / `figure_ids`.
* **Idempotent migrations** — `views.migrate_existing_figures()` is
  re-runnable; figures already pointing at a real view are skipped.
* **Thumbnail size cap** — 200 KB enforced server-side on PUT
  `/figures/<id>/thumbnail` and `/views/<id>/thumbnail`. A
  misbehaving client can't fill the disk.

## See also

* [API.md](API.md) — the endpoints that read/write these records
* [USER_FLOWS.md](USER_FLOWS.md) — when each record is created
