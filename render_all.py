"""Render all 3D variants on each test STEP at iso view.

Loads each STEP file once (the expensive part - OCCT mesh tessellation)
then runs t1/t2/t3/t4_clean/t4_crease against the same vtkPolyData.
Plus a tuned 'painted' PBR variant (low-metallic) since the default polished
metal washes out badly against the white background.
"""
from __future__ import annotations
import time
from pathlib import Path
from common import step_to_vtk, STEP_FILES
import t1_pbr_metal as t1
import t2_ssao_clay as t2
import t3_toon as t3
import t4_shaded_outline as t4


def variants(width, height):
    return [
        ("t1a_polished",  lambda poly, b, d, vd, out: t1.render(
            poly, b, d, vd, out, width=width, height=height,
            base_color=(0.78, 0.78, 0.80), metallic=0.85, roughness=0.32)),
        ("t1b_painted",   lambda poly, b, d, vd, out: t1.render(
            poly, b, d, vd, out, width=width, height=height,
            base_color=(0.55, 0.60, 0.66), metallic=0.18, roughness=0.55)),
        ("t2_ssao",       lambda poly, b, d, vd, out: t2.render(
            poly, b, d, vd, out, width=width, height=height)),
        ("t3_toon",       lambda poly, b, d, vd, out: t3.render(
            poly, b, d, vd, out, width=width, height=height)),
        ("t4_clean",      lambda poly, b, d, vd, out: t4.render(
            poly, b, d, vd, out, width=width, height=height,
            include_creases=False)),
        ("t4_crease",     lambda poly, b, d, vd, out: t4.render(
            poly, b, d, vd, out, width=width, height=height,
            include_creases=True)),
    ]


def run(step_path: Path, out_dir: Path, view_name: str, view_dir,
        width=3000, height=1800):
    print(f"\n=== {step_path.name} / {view_name} ({width}x{height}) ===")
    t0 = time.time()
    poly, bbox, diag = step_to_vtk(step_path)
    print(f"  mesh: {poly.GetNumberOfPoints()} verts {poly.GetNumberOfCells()} "
          f"tris in {time.time()-t0:.1f}s  diag={diag:.0f}mm")
    for name, fn in variants(width, height):
        out_png = out_dir / f"{step_path.stem}__{view_name}__{name}.png"
        t1s = time.time()
        try:
            fn(poly, bbox, diag, view_dir, out_png)
            ok = out_png.stat().st_size / 1024
            print(f"  [{name}] {time.time()-t1s:5.1f}s  {ok:6.0f}KB")
        except Exception as e:
            print(f"  [{name}] FAIL: {type(e).__name__}: {e}")


if __name__ == "__main__":
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    # Siderail at full hi-res, Presto at moderate (much bigger mesh)
    # New default view dir matches t5 / viewer: front-right-above 3/4 iso.
    NEW_ISO = (-0.5, -1.0, 0.7)
    targets = [
        (STEP_FILES[0], "iso", NEW_ISO, 4500, 2700),
        (STEP_FILES[1], "iso", NEW_ISO, 3000, 1800),
    ]
    overall = time.time()
    for sp, vn, vd, w, h in targets:
        if not sp.exists():
            print("missing:", sp); continue
        # need to pass width/height through the render lambdas - rebind variants
        # we accept the slight ineffeciency of re-running variants() each STEP
        run(sp, out_dir, vn, vd, width=w, height=h)
    print(f"\ntotal {time.time()-overall:.1f}s")
