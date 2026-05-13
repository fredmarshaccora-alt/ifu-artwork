"""t4: Shaded 3D body + clean silhouette outline (IFU hybrid).

Combines the t2 SSAO clay body with the vtkPolyDataSilhouette outline from
v5 - but no internal feature/crease edges. The single bold contour anchors
the part on the page; the soft AO body gives it real 3D form.

Closest to the look a Cadasio / Solidworks Composer IFU figure gives you.
"""
from __future__ import annotations
import time
from pathlib import Path
import vtk
from common import step_to_vtk, setup_camera, write_png, STEP_FILES, VIEWS


def render(poly, bbox, diag, view_dir, out_png: Path,
           width=4500, height=2700,
           body_color=(0.88, 0.88, 0.90),
           outline_width=4.5,
           ssao_radius_frac=0.012, ssao_bias_frac=0.0008, ssao_kernel=64,
           include_creases=False, feature_angle=35.0):
    body_mapper = vtk.vtkOpenGLPolyDataMapper()
    body_mapper.SetInputData(poly)
    body_mapper.ScalarVisibilityOff()
    body = vtk.vtkActor()
    body.SetMapper(body_mapper)
    p = body.GetProperty()
    p.SetColor(*body_color)
    p.SetAmbient(0.40); p.SetDiffuse(0.85); p.SetSpecular(0.0)
    p.SetInterpolationToPhong()

    sil = vtk.vtkPolyDataSilhouette()
    sil.SetInputData(poly)
    sil.SetEnableFeatureAngle(False)
    sil.SetBorderEdges(True)
    sil_mapper = vtk.vtkPolyDataMapper()
    sil_mapper.SetInputConnection(sil.GetOutputPort())
    sil_actor = vtk.vtkActor()
    sil_actor.SetMapper(sil_mapper)
    sp = sil_actor.GetProperty()
    sp.SetColor(0, 0, 0)
    sp.SetLineWidth(outline_width)

    ren = vtk.vtkOpenGLRenderer()
    ren.SetBackground(1.0, 1.0, 1.0)
    ren.AddActor(body)
    ren.AddActor(sil_actor)
    sil.SetCamera(ren.GetActiveCamera())

    if include_creases:
        feat = vtk.vtkFeatureEdges()
        feat.SetInputData(poly); feat.SetFeatureAngle(feature_angle)
        feat.BoundaryEdgesOn(); feat.FeatureEdgesOn()
        feat.NonManifoldEdgesOff(); feat.ManifoldEdgesOff()
        feat.ColoringOff(); feat.Update()
        fm = vtk.vtkPolyDataMapper(); fm.SetInputConnection(feat.GetOutputPort())
        fa = vtk.vtkActor(); fa.SetMapper(fm)
        fa.GetProperty().SetColor(0, 0, 0)
        fa.GetProperty().SetLineWidth(outline_width * 0.3)
        ren.AddActor(fa)

    lk = vtk.vtkLightKit()
    lk.SetKeyLightIntensity(0.80)
    lk.SetKeyLightWarmth(0.55)
    lk.SetKeyToFillRatio(2.2); lk.SetKeyToBackRatio(2.5); lk.SetKeyToHeadRatio(6)
    lk.AddLightsToRenderer(ren)

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
        width=4500, height=2700, variant="clean"):
    print(f"\n--- {step_path.name} / {view_name} / {variant} ---")
    t0 = time.time()
    poly, bbox, diag = step_to_vtk(step_path)
    print(f"  mesh: {poly.GetNumberOfPoints()} verts {poly.GetNumberOfCells()} tris  "
          f"in {time.time()-t0:.1f}s")
    out_png = out_dir / f"{step_path.stem}__{view_name}__t4_{variant}.png"
    t1 = time.time()
    include_creases = (variant == "crease")
    render(poly, bbox, diag, view_dir, out_png, width=width, height=height,
           include_creases=include_creases)
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
            run(f, out_dir, vn, vd, variant="clean")
            run(f, out_dir, vn, vd, variant="crease")
