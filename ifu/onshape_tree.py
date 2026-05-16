"""Onshape feature-tree fetcher.

Returns a nested list of dicts shaped like::

    [{"name": ..., "type": "Part" or "Assembly",
      "part_id": ..., "children": [...]}, ...]

Returns ``None`` on any failure (network, auth, client unavailable).
The viewer falls back to ``step_tree.fetch_step_tree`` gracefully.
"""
from __future__ import annotations
from .onshape_client import OnshapeClient


def fetch_onshape_tree(ids):
    if OnshapeClient is None or ids is None:
        return None
    try:
        c = OnshapeClient()
        did, wid, eid = ids["did"], ids["wid"], ids["eid"]
        asm = c.get(f"/assemblies/d/{did}/w/{wid}/e/{eid}",
                    params={"includeMateFeatures": "false",
                            "includeNonSolids": "false",
                            "includeMateConnectors": "false"})
        root = asm.get("rootAssembly") or {}
        sub_asms = {sa.get("elementId", "") + "/" + sa.get("documentId", ""): sa
                    for sa in (asm.get("subAssemblies") or [])}

        def build(instances):
            nodes = []
            for inst in instances or []:
                node = {
                    "name": inst.get("name") or inst.get("partId") or "?",
                    "type": inst.get("type", "Part"),
                    "part_id": inst.get("partId") or "",
                    "children": [],
                }
                if inst.get("type") == "Assembly":
                    sa_key = inst.get("elementId", "") + "/" + inst.get("documentId", "")
                    sa = sub_asms.get(sa_key)
                    if sa is not None:
                        node["children"] = build(sa.get("instances") or [])
                nodes.append(node)
            return nodes

        return build(root.get("instances") or [])
    except Exception as exc:
        print(f"  Onshape tree fetch failed: {type(exc).__name__}: {exc}",
              flush=True)
        return None
