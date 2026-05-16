"""GLB export.

Wraps every solid in a named trimesh node (``part_NNN``) and bundles into
a single GLB so the WebGL viewer can highlight per part by name.  Mesh
deflection is intentionally coarse: the 3D pane is a view-finder, not a
print pipeline; the final SVG goes through analytical HLR.
"""
from __future__ import annotations
import base64
import trimesh

from t5_hlr_vector import split_solids
from .mesh import solid_mesh_arrays


def export_glb_b64(shape, mesh_defl):
    """Mesh ``shape`` and serialise to a base64-encoded GLB.

    Returns ``(b64_str, summary_dict)`` where summary contains the part
    count, triangle count and final blob size in KB.  Returns
    ``(None, {parts: 0, ...})`` if no solid produced a triangulation.
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    BRepMesh_IncrementalMesh(shape, mesh_defl, False, 0.5, True)

    scene = trimesh.Scene()
    n_parts = 0
    n_tris = 0
    for idx, label, solid in split_solids(shape):
        v, t = solid_mesh_arrays(solid)
        if v is None or len(v) == 0 or len(t) == 0:
            continue
        m = trimesh.Trimesh(vertices=v, faces=t, process=False)
        node_name = f"part_{idx:03d}"
        scene.add_geometry(m, node_name=node_name, geom_name=label)
        n_parts += 1
        n_tris += len(t)
    if n_parts == 0:
        return None, {"parts": 0, "tris": 0, "kb": 0}
    glb_bytes = scene.export(file_type="glb")
    b64 = base64.b64encode(glb_bytes).decode("ascii")
    return b64, {"parts": n_parts, "tris": n_tris,
                 "kb": len(glb_bytes) // 1024}
