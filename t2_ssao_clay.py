"""t2: Matte clay + screen-space ambient occlusion.

Single light-grey diffuse material with SSAO darkening tight crevices and
contact regions. Reads as an engineering clay model / Onshape "Shaded with
matte" view. No specular, no edges - shape comes from form and AO alone.
"""
from __future__ import annotations
import time
from pathlib import Path
import vtk
from common import step_to_vtk, setup_camera, write_png, STEP_FILES, VIEWS


def render(poly, bbox, diag, view_dir, out_png: Path,
           width=4500, height=2700,
           body_color=(0.82, 0.82, 0.84),
           ssao_radius_frac=0.012, ssao_bias_frac=0.0008, ssao_kernel=64):
    body_mapper = vtk.vtkOpenGLPolyDataMapper()
    body_mapper.SetInputData(poly)
    body_mapper.ScalarVisibilityOff()

    body = vtk.vtkActor()
    body.SetMapper(body_mapper)
    p = body.GetProperty()
    p.SetColor(*body_color)
    p.SetAmbient(0.35)
    p.SetDiffuse(0.85)
    p.SetSpecular(0.0)
    p.SetInterpolationToPhong()

    ren = vtk.vtkOpenGLRenderer()
    ren.SetBackground(1.0, 1.0, 1.0)
    ren.AddActor(body)

    # Soft 3-point lighting (the AO does the heavy lifting; lights stay flat)
    lk = vtk.vtkLightKit()
    lk.SetKeyLightIntensity(0.85)
    lk.SetKeyLightWarmth(0.55)
    lk.SetKeyToHeadRatio(6)
    lk.SetKeyToFillRatio(2.0)
    lk.SetKeyToBackRatio(2.2)
    lk.AddLightsToRenderer(ren)

    # SSAO pass over the basic render
    basic = vtk.vtkRenderStepsPass()
    ssao = vtk.vtkSSAOPass()
    ssao.SetRadius(diag * ssao_radius_frac)
    ssao.SetBias(diag * ssao_bias_frac)
    ssao.SetKernelSize(ssao_kernel)
    ssao.SetBlur(True)
    ssao.SetDelegatePass(basic)
    ren.SetPass(ssao)

    rwin = vtk.vtkRenderWindow()
    rwin.SetOffScreenRendering(True)
    rwin.SetMultiSamples(8)
    rwin.AddRenderer(ren)
    rwin.SetSize(width, height)

    setup_camera(ren, bbox, diag, view_dir)
    rwin.Render()
    write_png(rwin, out_png)


def run(step_path: Path, out_dir: Path, view_name: str, view_dir,
        width=4500, height=2700):
    print(f"\n--- {step_path.name} / {view_name} ---")
    t0 = time.time()
    poly, bbox, diag = step_to_vtk(step_path)
    print(f"  mesh: {poly.GetNumberOfPoints()} verts {poly.GetNumberOfCells()} tris  "
          f"in {time.time()-t0:.1f}s")
    out_png = out_dir / f"{step_path.stem}__{view_name}__t2_ssao.png"
    t1 = time.time()
    render(poly, bbox, diag, view_dir, out_png, width=width, height=height)
    print(f"  render {time.time()-t1:.1f}s -> {out_png.name}  "
          f"{out_png.stat().st_size/1024:.0f}KB")
    return out_png


if __name__ == "__main__":
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    for f in STEP_FILES:
        if not f.exists():
            print("missing:", f); continue
        for vn, vd in VIEWS:
            run(f, out_dir, vn, vd)
