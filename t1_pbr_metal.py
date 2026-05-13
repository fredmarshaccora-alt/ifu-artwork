"""t1: VTK PBR metallic + procedural HDRI.

Anodised/brushed aluminium look. No edges, no creases - pure physically based
shading with image-based lighting from a procedural studio-gradient skybox.
Reference look: Solidworks Visualize / Keyshot product hero shot.
"""
from __future__ import annotations
import time
from pathlib import Path
import numpy as np
import vtk
from vtk.util import numpy_support
from common import step_to_vtk, setup_camera, write_png, STEP_FILES, VIEWS


def make_studio_hdri(w: int = 1024, h: int = 512,
                     top=(1.05, 1.10, 1.15),
                     mid=(0.80, 0.80, 0.80),
                     bot=(0.30, 0.30, 0.32),
                     key_dir=(0.4, -0.6, 0.7), key_intensity=2.4, key_radius=0.18,
                     fill_dir=(-0.7, 0.3, 0.4), fill_intensity=0.8, fill_radius=0.30,
                     ground_kick=0.6):
    """Procedural equirectangular HDR: vertical gradient + two soft lobes.

    Returns a (h, w, 3) float32 array of linear-space radiance values >0.
    """
    img = np.zeros((h, w, 3), dtype=np.float32)
    v_col = np.linspace(0.0, 1.0, h).reshape(h, 1, 1)
    top_a = np.array(top, dtype=np.float32).reshape(1, 1, 3)
    mid_a = np.array(mid, dtype=np.float32).reshape(1, 1, 3)
    bot_a = np.array(bot, dtype=np.float32).reshape(1, 1, 3)
    # vertical 3-stop ramp (top -> mid -> bot)
    t_upper = np.clip(1.0 - v_col * 2.0, 0.0, 1.0)
    t_lower = np.clip((v_col - 0.5) * 2.0, 0.0, 1.0)
    t_mid = 1.0 - t_upper - t_lower
    img[:] = t_upper * top_a + t_mid * mid_a + t_lower * bot_a

    # Equirect lat/lon mesh
    v = np.linspace(0.0, 1.0, h).reshape(h, 1)
    u = np.linspace(0.0, 1.0, w).reshape(1, w)
    phi = (u - 0.5) * 2.0 * np.pi
    theta = (0.5 - v) * np.pi
    dx = np.cos(theta) * np.sin(phi)
    dy = np.sin(theta) * np.ones_like(phi)
    dz = np.cos(theta) * np.cos(phi)

    def lobe(direction, intensity, radius):
        d = np.array(direction, dtype=np.float32)
        d /= np.linalg.norm(d)
        cosang = dx * d[0] + dy * d[1] + dz * d[2]
        ang = np.arccos(np.clip(cosang, -1.0, 1.0))
        falloff = np.exp(-(ang / radius) ** 2)
        return falloff.astype(np.float32) * intensity

    key = lobe(key_dir, key_intensity, key_radius)
    fill = lobe(fill_dir, fill_intensity, fill_radius)
    # Soft ground bounce (lower hemisphere only)
    ground = np.clip(-dy, 0.0, 1.0).astype(np.float32) * ground_kick

    for c in range(3):
        img[..., c] += key + fill * (0.85 if c == 2 else 1.0) + ground * (0.95 if c == 0 else 0.85)

    img = np.clip(img, 0.0, 12.0)
    return img


def hdri_to_vtk_texture(hdri: np.ndarray) -> vtk.vtkTexture:
    """Wrap a float32 (h,w,3) array as an equirectangular vtkTexture."""
    h, w, _ = hdri.shape
    flat = np.ascontiguousarray(hdri[::-1, :, :].reshape(-1, 3))
    arr = numpy_support.numpy_to_vtk(flat, deep=True, array_type=vtk.VTK_FLOAT)
    arr.SetNumberOfComponents(3)
    img = vtk.vtkImageData()
    img.SetDimensions(w, h, 1)
    img.GetPointData().SetScalars(arr)
    tex = vtk.vtkTexture()
    tex.SetColorModeToDirectScalars()
    tex.MipmapOn()
    tex.InterpolateOn()
    tex.SetInputData(img)
    tex.Update()
    return tex


def render(poly, bbox, diag, view_dir, out_png: Path,
           width=4500, height=2700,
           base_color=(0.78, 0.78, 0.80), metallic=0.85, roughness=0.32,
           show_skybox=False):
    body_mapper = vtk.vtkOpenGLPolyDataMapper()
    body_mapper.SetInputData(poly)
    body_mapper.ScalarVisibilityOff()

    body = vtk.vtkActor()
    body.SetMapper(body_mapper)
    p = body.GetProperty()
    p.SetInterpolationToPBR()
    p.SetColor(*base_color)
    p.SetMetallic(metallic)
    p.SetRoughness(roughness)

    ren = vtk.vtkOpenGLRenderer()
    ren.SetBackground(1.0, 1.0, 1.0)
    ren.AddActor(body)
    ren.UseImageBasedLightingOn()
    ren.UseSphericalHarmonicsOn()
    hdri = make_studio_hdri()
    tex = hdri_to_vtk_texture(hdri)
    ren.SetEnvironmentTexture(tex, False)

    if show_skybox:
        skybox = vtk.vtkSkybox()
        skybox.SetTexture(tex)
        skybox.SetProjectionToSphere()
        ren.AddActor(skybox)

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
    out_png = out_dir / f"{step_path.stem}__{view_name}__t1_pbr.png"
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
