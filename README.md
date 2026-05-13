# Accora IFU artwork generator

Generates publication-clean line-art illustrations from Onshape/STEP assemblies
for use in Instructions For Use (IFU) documents.

## What it does

Takes a STEP file of an assembly (Presto bed, Folding siderail, Contesa, etc.),
runs analytical hidden-line removal on the B-rep, and emits:

- per-part-tagged **SVG** (vector, publication-grade at any zoom)
- a **PNG** raster for inline preview
- an interactive **HTML viewer** with pan/zoom, part highlight, callout
  arrows, layer toggles, and annotated-SVG export

Each edge is classified into the same buckets Composer exposes —
**Profile / Sharp / Smooth** — so the look matches the IFU style Accora has
been using.

## Pipeline

```
STEP file
   │
   ▼  cadquery.importers.importStep
TopoDS_Shape
   │
   ▼  rotate_shape (per-source pre-rotation → long axis along world X)
oriented shape
   │
   ▼  HLRBRep_PolyAlgo + HLRBRep_PolyHLRToShape
edges classified (visible / hidden × silhouette / sharp / smooth)
   │
   ▼  GCPnts_UniformDeflection on each curve
projected polylines (u,v)
   │
   ├─► write_svg_parts → per-solid tagged SVG
   └─► PIL rasterise   → PNG preview
```

`build_viewer.py` then bundles every (file × view × mode) into a single
self-contained `out/viewer.html`.

## Files

| file | role |
|---|---|
| `t5_hlr_vector.py` | Core HLR + SVG/PNG writers. `STD_VIEWS` = camera dirs. |
| `build_viewer.py` | Orchestrator. `SOURCES` lists STEP inputs + pre-rotation. |
| `common.py` | STEP → vtkPolyData helper and shared camera setup. |
| `slim_svg.py` | Strip whitespace + decimals from generated SVGs. |
| `rebuild_html.py` | Re-bundle existing SVGs into a fresh `viewer.html` without re-running HLR. |
| `render_all.py` | Convenience runner for the legacy raster tests. |
| `progression.py` | Generates the technique-comparison montage. |
| `t1_pbr_metal.py` / `t2_ssao_clay.py` / `t3_toon.py` / `t4_shaded_outline.py` | Earlier raster experiments kept for reference. |
| `fetch_contesa_step.py` | Pulls the Contesa STEP from Onshape via API. |

Older exploration (HLR vs mesh-silhouette vs VTK-EDL etc.) lives next door in
`../step_lineart_test/`.

## Running

```bash
# Render everything in SOURCES and build the viewer
python build_viewer.py

# Re-build the viewer HTML without re-running HLR (fast)
python rebuild_html.py

# Single STEP smoke test (just iso)
python t5_hlr_vector.py
```

Outputs land in `out/` (gitignored).

## Adding a new source

1. Drop the STEP file somewhere on disk (or fetch via API).
2. Add an entry to `SOURCES` in `build_viewer.py`:
   ```python
   ("mysource", "Friendly label", Path(r"...\file.step"),
    {"mesh_defl": 1.5, "sample_defl": 1.0},
    pre_rotate),   # ((axis), angle_deg) — or None if model is already X-long, Z-up
   ```
3. The bbox snapshot printed at load time tells you whether a pre-rotation
   is needed: a bed should have its largest extent on X.

## Orientation convention

`STD_VIEWS` assume **X = length, Z = up**.  Each source is pre-rotated to
that frame so the iso/front/side views all line up.  Models that come in
with a different convention (e.g. Presto's native Z-along-length) get a
one-line rotation entry in `SOURCES`.

## Dependencies

- `cadquery` (brings OCCT bindings via `OCP`)
- `vtk`
- `Pillow`
- `numpy`

No package manifest yet — install ad-hoc in the active Python env.
