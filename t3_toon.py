"""t3: Cel/toon shading.

Render world-space surface normals + a depth buffer, dot the normals against
two synthetic light directions in numpy, then posterise the result into 3
flat bands. Produces the technical-illustration "manga-shaded" look: flat
colour zones that still read as a 3D form.

A thin black silhouette is drawn from the depth buffer to anchor the part
against the page (silhouette only - no internal feature creases).
"""
from __future__ import annotations
import time
import math
from pathlib import Path
import numpy as np
import vtk
from vtk.util import numpy_support
from PIL import Image
from scipy.ndimage import binary_dilation
from common import step_to_vtk, setup_camera, STEP_FILES, VIEWS


def render_normals(poly, bbox, diag, view_dir, w, h):
    """RGB image where each pixel = (n+1)/2*255 with n = world-space normal."""
    n_arr = poly.GetPointData().GetNormals()
    n_np = numpy_support.vtk_to_numpy(n_arr)
    rgb = np.clip((n_np * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)
    colors = vtk.vtkUnsignedCharArray()
    colors.SetNumberOfComponents(3); colors.SetName("Colors")
    colors.SetNumberOfTuples(rgb.shape[0])
    for i in range(rgb.shape[0]):
        colors.SetTuple3(i, int(rgb[i, 0]), int(rgb[i, 1]), int(rgb[i, 2]))
    poly.GetPointData().SetScalars(colors)

    m = vtk.vtkPolyDataMapper()
    m.SetInputData(poly); m.SetColorModeToDirectScalars(); m.ScalarVisibilityOn()
    a = vtk.vtkActor(); a.SetMapper(m)
    pp = a.GetProperty(); pp.SetAmbient(1.0); pp.SetDiffuse(0.0)
    pp.SetSpecular(0.0); pp.LightingOff()

    ren = vtk.vtkRenderer(); ren.SetBackground(0.5, 0.5, 0.5); ren.AddActor(a)
    rwin = vtk.vtkRenderWindow(); rwin.SetOffScreenRendering(True)
    rwin.AddRenderer(ren); rwin.SetSize(w, h); rwin.SetMultiSamples(0)
    setup_camera(ren, bbox, diag, view_dir)
    rwin.Render()

    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(rwin); w2i.SetInputBufferTypeToRGB(); w2i.ReadFrontBufferOff(); w2i.Update()
    img = w2i.GetOutput(); dims = img.GetDimensions()
    arr = numpy_support.vtk_to_numpy(img.GetPointData().GetScalars()) \
        .reshape((dims[1], dims[0], 3))
    return arr[::-1, :, :]


def render_depth(poly, bbox, diag, view_dir, w, h):
    m = vtk.vtkPolyDataMapper(); m.SetInputData(poly); m.ScalarVisibilityOff()
    a = vtk.vtkActor(); a.SetMapper(m); a.GetProperty().SetColor(1, 1, 1)
    ren = vtk.vtkRenderer(); ren.SetBackground(1, 1, 1); ren.AddActor(a)
    rwin = vtk.vtkRenderWindow(); rwin.SetOffScreenRendering(True)
    rwin.AddRenderer(ren); rwin.SetSize(w, h); rwin.SetMultiSamples(0)
    setup_camera(ren, bbox, diag, view_dir)
    rwin.Render()
    z = vtk.vtkFloatArray()
    rwin.GetZbufferData(0, 0, w - 1, h - 1, z)
    arr = numpy_support.vtk_to_numpy(z).reshape((h, w))
    return arr[::-1, :]


def posterise(intensity, bands):
    """Quantise [0,1] -> nearest of len(bands)-1 thresholds, return band index."""
    thr = np.array(bands[:-1], dtype=np.float32)
    out = np.zeros_like(intensity, dtype=np.int32)
    for i, t in enumerate(thr):
        out[intensity >= t] = i + 1
    return out


def render(poly, bbox, diag, view_dir, out_png: Path,
           width=4500, height=2700,
           palette=((0.96, 0.96, 0.97),  # band 0 (lit)
                    (0.78, 0.80, 0.85),  # band 1 (mid)
                    (0.50, 0.55, 0.62)), # band 2 (shadow)
           thresholds=(0.0, 0.55, 0.82),
           key_dir=(0.4, -0.6, 0.7),
           silhouette_w=2):
    normals_img = render_normals(poly, bbox, diag, view_dir, width, height)
    depth = render_depth(poly, bbox, diag, view_dir, width, height)

    n = normals_img.astype(np.float32) / 127.5 - 1.0  # (-1,1)
    L = np.array(key_dir, dtype=np.float32); L /= np.linalg.norm(L)
    ndotl = np.clip(n[..., 0] * L[0] + n[..., 1] * L[1] + n[..., 2] * L[2], 0.0, 1.0)

    bg = depth >= (depth.max() - 1e-4)
    fg = ~bg

    band_idx = posterise(ndotl, thresholds)
    pal = (np.array(palette) * 255).astype(np.uint8)
    img = pal[band_idx]
    img[bg] = (255, 255, 255)

    # Soft silhouette around the body
    if silhouette_w > 0:
        outer = binary_dilation(fg, iterations=silhouette_w) & ~fg
        img[outer] = (40, 45, 55)

    Image.fromarray(img, mode="RGB").save(out_png)


def run(step_path: Path, out_dir: Path, view_name: str, view_dir,
        width=4500, height=2700):
    print(f"\n--- {step_path.name} / {view_name} ---")
    t0 = time.time()
    poly, bbox, diag = step_to_vtk(step_path)
    print(f"  mesh: {poly.GetNumberOfPoints()} verts {poly.GetNumberOfCells()} tris  "
          f"in {time.time()-t0:.1f}s")
    out_png = out_dir / f"{step_path.stem}__{view_name}__t3_toon.png"
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
