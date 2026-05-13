"""Shared utilities for 3D rendering experiments.

STEP -> vtkPolyData (via OCCT/cadquery, same path as v5/v11 line-art tests)
plus a single setup_camera() so every variant frames the part identically.
"""
from __future__ import annotations
import math
from pathlib import Path
import vtk
import numpy as np
import cadquery as cq
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location


def step_to_vtk(step_path: Path, deflection: float | None = None):
    cq_shape = cq.importers.importStep(str(step_path))
    val = cq_shape.val()
    shape = val.wrapped
    bbox = val.BoundingBox()
    diag = (bbox.xlen**2 + bbox.ylen**2 + bbox.zlen**2) ** 0.5
    if deflection is None:
        deflection = max(diag / 3000.0, 0.05)
    BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)

    points = vtk.vtkPoints()
    cells = vtk.vtkCellArray()
    voff = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                points.InsertNextPoint(p.X(), p.Y(), p.Z())
            reversed_face = face.Orientation() == 1
            for i in range(1, tri.NbTriangles() + 1):
                t = tri.Triangle(i)
                a, b, c = t.Get()
                cells.InsertNextCell(3)
                if reversed_face:
                    cells.InsertCellPoint(voff + b - 1)
                    cells.InsertCellPoint(voff + a - 1)
                else:
                    cells.InsertCellPoint(voff + a - 1)
                    cells.InsertCellPoint(voff + b - 1)
                cells.InsertCellPoint(voff + c - 1)
            voff += tri.NbNodes()
        exp.Next()

    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetPolys(cells)

    nf = vtk.vtkPolyDataNormals()
    nf.SetInputData(poly)
    nf.ComputePointNormalsOn()
    nf.ComputeCellNormalsOn()
    nf.SplittingOff()
    nf.AutoOrientNormalsOn()
    nf.Update()
    return nf.GetOutput(), bbox, diag


def setup_camera(ren, bbox, diag, view_dir, zoom=1.35):
    cx = (bbox.xmin + bbox.xmax) / 2
    cy = (bbox.ymin + bbox.ymax) / 2
    cz = (bbox.zmin + bbox.zmax) / 2
    vlen = math.sqrt(sum(v * v for v in view_dir))
    eye = (
        cx + view_dir[0] / vlen * diag * 2,
        cy + view_dir[1] / vlen * diag * 2,
        cz + view_dir[2] / vlen * diag * 2,
    )
    cam = ren.GetActiveCamera()
    cam.SetParallelProjection(True)
    cam.SetFocalPoint(cx, cy, cz)
    cam.SetPosition(*eye)
    if abs(view_dir[2] / vlen) < 0.9:
        cam.SetViewUp(0, 0, 1)
    else:
        cam.SetViewUp(0, 1, 0)
    ren.ResetCameraClippingRange()
    ren.ResetCamera()
    cam.Zoom(zoom)
    return cam


def write_png(rwin, out_png: Path):
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(rwin)
    w2i.SetScale(1)
    w2i.SetInputBufferTypeToRGB()
    w2i.ReadFrontBufferOff()
    w2i.Update()
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(out_png))
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()


# Default test inputs (same as v5/v11)
STEP_FILES = [
    Path(r"C:\Users\FredMarshAccora\Downloads\P194-03-00 Folding siderail ASSE.STEP"),
    Path(__file__).parent.parent / "step_lineart_test" / "presto_top_level.step",
]

VIEWS = [
    ("iso", (-1.75, 1.1, 5)),
    ("3qtr", (-2.5, 1.5, 1.2)),
]
