"""Mesh extraction helpers.

Pulls vertex/triangle arrays out of a triangulated TopoDS_Solid for
either GLB export or rasterised analysis.  Callers must have already
run ``BRepMesh_IncrementalMesh`` on the parent shape (or this solid) --
the triangulation is read straight off ``BRep_Tool.Triangulation_s``
with no implicit meshing.
"""
from __future__ import annotations
import re
import numpy as np


def slugify(s: str) -> str:
    """URL-safe lowercase identifier; collapses non-alphanumeric runs to '_'."""
    return re.sub(r"[^a-z0-9_-]+", "_", s.lower()).strip("_")


def solid_mesh_arrays(solid):
    """Return ``(vertices Nx3, faces Mx3)`` numpy arrays for one TopoDS_Solid.

    Returns ``(None, None)`` if the solid has no triangulation.
    """
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    vs, ts = [], []
    voff = 0
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                vs.append((p.X(), p.Y(), p.Z()))
            reversed_face = face.Orientation() == 1
            for i in range(1, tri.NbTriangles() + 1):
                t = tri.Triangle(i)
                a, b, c = t.Get()
                if reversed_face:
                    ts.append((voff + b - 1, voff + a - 1, voff + c - 1))
                else:
                    ts.append((voff + a - 1, voff + b - 1, voff + c - 1))
            voff += tri.NbNodes()
        exp.Next()
    if not vs or not ts:
        return None, None
    return np.array(vs, dtype=np.float32), np.array(ts, dtype=np.uint32)


# Legacy alias -- the old name had a leading underscore.  Several call
# sites in the existing codebase import it as ``_solid_mesh_arrays``;
# keep the public name as well.
_solid_mesh_arrays = solid_mesh_arrays
