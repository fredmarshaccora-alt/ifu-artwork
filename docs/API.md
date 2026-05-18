# HTTP API

All endpoints are served by `serve.py` on `http://localhost:5000`. Single
user; no auth. Responses are JSON unless otherwise noted.

OCCT calls are serialised through a module-level `_HLR_LOCK`; expensive
endpoints (`/api/render`, `/api/part_footprints`, `/api/render_region`,
`/api/glb/<sid>`, `/api/sources/<sid>/reconfigure`) wait their turn.

A rolling debug log records every request to `/api/debug/log` (level,
method, path, source_id, status, ms) plus structured op events for the
render / raster / Onshape paths.

---

## Catalogue

| Endpoint | Verb | Purpose |
|---|---|---|
| `/api/healthz` | GET | `{ok, sources}` — server up + loaded sources |
| `/api/debug/log` | GET | recent structured events, `?since=<seq>` for deltas |
| `/api/sources` | GET | merged static + dynamic source list |
| `/api/sources/<sid>/configuration` | GET | Onshape configuration parameters |
| `/api/sources/<sid>/reconfigure` | POST | re-translate at a different config |
| `/api/sources/<sid>/versions` | GET | cached Onshape Versions list |
| `/api/sources/<sid>/versions/refresh` | POST | hit Onshape, refresh cache |
| `/api/onshape/probe` | POST | check URL → return doc / element info, no download |
| `/api/onshape/import` | POST | start background STEP-translation job |
| `/api/onshape/import/<job_id>` | GET | poll a job |
| `/api/settings` | GET / PUT / PATCH | app-level settings |
| `/api/settings/reset` | POST | revert to defaults |
| `/api/projects` | GET / POST | list, create |
| `/api/projects/<pid>` | GET / PUT / DELETE | read, edit, delete (`?cascade=1`) |
| `/api/projects/<pid>/figures` | GET | figures belonging to a project |
| `/api/projects/<pid>/figures/<fid>` | POST / DELETE | attach / detach |
| `/api/projects/<pid>/views` | GET | views in a project (with figure_count) |
| `/api/views` | POST | create new view |
| `/api/views/<vid>` | GET / PUT / DELETE | read, edit, delete (`?cascade=1`) |
| `/api/views/<vid>/figures` | GET | resolved figures under this view |
| `/api/views/<vid>/figures/<fid>` | POST / DELETE | attach / detach |
| `/api/views/<vid>/thumbnail` | GET / PUT | image/png + base64 upload |
| `/api/views/migrate` | POST | idempotent 1:1 view-per-orphan-figure |
| `/api/figures` | GET / POST | list, create |
| `/api/figures/<fid>` | GET / PUT / DELETE | read, edit, delete |
| `/api/figures/<fid>/thumbnail` | GET / PUT | image/png + base64 upload |
| `/api/figures/<fid>/bind_revision` | POST | bind figure to an Onshape Version |
| `/api/figures/<fid>/revision_status` | GET | how many revisions behind |
| `/api/figures/orphans` | GET | figures with no project |
| `/api/render` | POST | OCCT HLR → SVG bytes |
| `/api/render_region` | POST | higher-detail HLR on a bbox sub-region |
| `/api/part_footprints` | POST | rasterised closed-loop outlines |
| `/api/part_silhouettes` | POST | per-part isolated HLR silhouettes |
| `/api/glb/<sid>` | GET | on-the-fly GLB export from a STEP shape |

Below: the endpoints that aren't standard CRUD have their request /
response shape documented in detail.

---

## `/api/render`  — POST

Run HLR on a source's TopoDS shape for a given camera and return an SVG.

### Body

Two camera shapes accepted (use whichever is more convenient):

```json
{
  "file_id": "siderail",
  "eye":    [1000, -1500, 800],
  "target": [0, 0, 0],
  "up_axis": { "axis": [0, 0, 1], "angle": 0 }
}
```

OR

```json
{
  "file_id": "siderail",
  "view_dir": [0.5, -0.7, 0.5],
  "focal":    [0, 0, 0]
}
```

### Response

`image/svg+xml` body. Headers:

| Header | Meaning |
|---|---|
| `X-Render-Seconds` | total seconds (HLR + SVG write) |
| `X-Render-Breakdown` | `hlr=2.0s mirror=0.0s svg-write=0.2s` or `cache-hit` |
| `X-Render-Polylines` | count of visible polylines drawn |

**Side effect**: spawns a daemon thread that runs
`compute_visible_footprints` against the same view + caches the per-part
closed-loop polylines, so when the user clicks a part the
`/api/part_footprints` response is instant.

### Errors

* `400` if `file_id` is unknown — body includes `{ known: [...] }`
* `400` if `eye == target` (zero view direction)
* `500` if HLR throws

---

## `/api/part_footprints`  — POST

Visible closed-loop polygons per part, in the same `(u, v)` coordinate
space as `/api/render`'s SVG.

### Body

```json
{
  "file_id": "siderail",
  "eye": [...], "target": [...],
  "part_indices": [0, 12, 34]
}
```

### Response

```json
{
  "part_indices": [0, 12, 34],
  "polylines": {
    "0": [[[u1,v1],[u2,v2],...,[u1,v1]], [...]],
    "12": [...],
    "34": []
  },
  "stats": {
    "hits": 2,
    "misses": 1,
    "raster_seconds": 0.0,
    "raster_inflight": false,
    "raster_done":     true
  }
}
```

* `polylines[idx]` is an array of closed polylines for that part. A
  part occluded into 3 disjoint visible regions returns 3 polylines.
  Empty array = part not visible from this angle.
* `stats.raster_inflight` / `raster_done` lets the client render a
  "shaded outline computing for N parts…" badge while the prefetch
  finishes.

---

## `/api/glb/<sid>`  — GET

Return a base64 GLB of the source's TopoDS shape so the 3D viewer can
load Onshape imports (which don't have a baked GLB in the page).

### Response

```json
{
  "source_id": "basic_4_motor_chair_c0319b",
  "b64":   "<base64 GLB bytes>",
  "parts": 77,
  "tris":  3510,
  "kb":    82
}
```

* `404` if the source isn't loaded (`{ known: [...] }`)
* `500` on export failure

---

## `/api/onshape/probe`  — POST

URL inspection: parses the Onshape URL, fetches document + element
metadata, returns it. No STEP download.

### Body

```json
{ "url": "https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>" }
```

### Response

```json
{
  "document_name": "Powered chair v3",
  "element_name":  "Top assembly",
  "element_type":  "ASSEMBLY",
  "onshape_ids": { "did": "...", "wid": "...", "vid": null, "mid": null,
                    "eid": "...", "wv": "w" }
}
```

Errors: `400` on bad URL, `503` if Onshape creds missing, `502` on
upstream failure.

---

## `/api/onshape/import`  — POST

Start a background STEP-translation job. Returns immediately (`202`)
with a job id; poll `/api/onshape/import/<job_id>` for progress.

### Body

```json
{ "url": "https://cad.onshape.com/documents/..." }
```

### Response

```json
{
  "id": "c6c559ab7add",
  "status": "queued",
  "progress": 0,
  "message": "queued",
  "url": "...",
  "started_at": "2026-05-17T19:32:50Z"
}
```

### Polling response

```json
{
  "id": "c6c559ab7add",
  "status": "ready",                   // queued / resolving / translating /
                                         // downloading / ready / error
  "progress": 100,
  "message": "ready",
  "document_name": "Powered chair v3",
  "element_name":  "Top assembly",
  "element_type":  "ASSEMBLY",
  "source_id": "powered_chair_v3_c0319b",
  "step_path": "...\\out\\imports\\powered_chair_v3_c0319b.step",
  "onshape_ids": { "did": "...", "wid": "...", "eid": "...", "wv": "w" }
}
```

When `status: "ready"`, the server has registered a new dynamic source
+ loaded it into `_SHAPES` automatically. The next `/api/render` /
`/api/glb` call against `source_id` will work without restart.

---

## `/api/sources/<sid>/reconfigure`  — POST

Re-translate a dynamic source with a new Onshape configuration string.
Used by the editor's configuration panel.

### Body

```json
{ "configuration": { "<param_id>": "<value>", ... } }
```

### Response

```json
{
  "ok": true,
  "source_id": "...",
  "configuration": { ... },
  "step_path": "...",
  "translation_id": "...",
  "external_data_id": "..."
}
```

**Side effects**: evicts the source's `_RENDER_CACHE`, `_SIL_CACHE`,
`_FOOT_CACHE`, `_FOOT_RASTER_DONE` entries; updates the dynamic-source
record. Subsequent renders use the new geometry.

---

## `/api/debug/log`  — GET

Recent structured events from the rolling buffer (max 500). The editor's
"server log" overlay polls this with `?since=<seq>` to stream new lines.

### Response

```json
{
  "events": [
    { "seq": 369, "t": "20:33:17", "level": "info",
      "method": "POST", "path": "/api/render",
      "source_id": "basic_4_motor_chair_c0319b" },
    { "seq": 370, "t": "20:33:17", "level": "ok",
      "op": "render.done", "source_id": "...",
      "total_sec": 3.99, "polylines": 45100, "svg_kb": 2646 }
  ],
  "latest_seq": 423
}
```

## See also

* [DATA_MODEL.md](DATA_MODEL.md) — record shapes referenced above
* [USER_FLOWS.md](USER_FLOWS.md) — when each endpoint fires
