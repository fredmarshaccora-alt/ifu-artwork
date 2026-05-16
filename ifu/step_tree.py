"""STEP product-hierarchy extraction.

Used as a fallback tree for sources without Onshape doc IDs so the user
can still select by sub-assembly rather than by individual body.

Critical detail (see backtest #8): some Onshape "Parts" export to
multiple TopoDS_Solid bodies.  We walk TopAbs_SOLID exploration on each
leaf's referred shape and reserve a CONTIGUOUS range of solid indices so
the tree-leaf count matches the cadquery solid count and clicking a
leaf-Part highlights ALL of its bodies.
"""
from __future__ import annotations
from pathlib import Path


def fetch_step_tree(step_path: Path):
    """Return the STEP product hierarchy as a list-of-dicts shaped like
    ``fetch_onshape_tree`` output.

    Each leaf-Part is assigned ``_solid_indices`` (a list of int) by
    depth-first leaf order so the viewer's positional mapping
    (i-th leaf <-> i-th cadquery solid) lines up.

    Returns ``None`` if STEPCAFControl isn't available or the read fails.
    """
    try:
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TDocStd import TDocStd_Document
        from OCP.XCAFApp import XCAFApp_Application
        from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDF import TDF_LabelSequence, TDF_Label
        from OCP.TDataStd import TDataStd_Name
    except Exception as exc:
        print(f"  STEPCAFControl unavailable: {exc}", flush=True)
        return None

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app.NewDocument(TCollection_ExtendedString("XmlOcaf"), doc)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    status = reader.ReadFile(str(step_path))
    if int(status) != 1:   # IFSelect_RetDone
        print(f"  STEPCAFControl ReadFile failed: status={int(status)}",
              flush=True)
        return None
    try:
        reader.Transfer(doc)
    except Exception as exc:
        print(f"  STEPCAFControl Transfer failed: {exc}", flush=True)
        return None

    stool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def name_of(label):
        n = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), n):
            try:
                return str(n.Get().ToExtString())
            except Exception:
                return "?"
        return ""

    # NAUO* and similar component-instance names are auto-generated and
    # usually not useful; prefer the referred shape's name.
    def looks_auto(nm):
        if not nm:
            return True
        if nm.startswith("NAUO") or nm.startswith("SHAPE"):
            return True
        return False

    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID

    def count_solids_in(shape):
        if shape is None or shape.IsNull():
            return 0
        n = 0
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp.More():
            n += 1
            exp.Next()
        return n

    def walk(label, counter):
        target = label
        is_ref = XCAFDoc_ShapeTool.IsReference_s(label)
        if is_ref:
            ref = TDF_Label()
            if XCAFDoc_ShapeTool.GetReferredShape_s(label, ref):
                target = ref
        comp_nm = name_of(label) if is_ref else ""
        tgt_nm = name_of(target)
        nm = tgt_nm if tgt_nm else (comp_nm if not looks_auto(comp_nm) else "?")

        is_asm = XCAFDoc_ShapeTool.IsAssembly_s(target)
        if is_asm:
            children_seq = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(target, children_seq, False)
            kids = []
            for i in range(1, children_seq.Length() + 1):
                kids.append(walk(children_seq.Value(i), counter))
            return {"name": nm, "type": "Assembly",
                    "part_id": "", "children": kids}

        # Leaf - count how many solids this Part contains; reserve a
        # contiguous range of solid indices so the per-Part selection
        # lights up all bodies.  Backtest #8 protects this.
        try:
            shape = XCAFDoc_ShapeTool.GetShape_s(target)
            n = count_solids_in(shape) or 1
        except Exception:
            n = 1
        start = counter[0]
        counter[0] += n
        indices = list(range(start, start + n))
        return {"name": nm, "type": "Part",
                "part_id": f"step_{start}", "children": [],
                "_solid_indices": indices}

    free = TDF_LabelSequence()
    stool.GetFreeShapes(free)
    counter = [0]
    nodes = []
    for i in range(1, free.Length() + 1):
        nodes.append(walk(free.Value(i), counter))
    print(f"  STEP tree: {counter[0]} leaf parts via STEPCAFControl_Reader",
          flush=True)
    return nodes


def count_tree(nodes) -> int:
    """Total number of nodes in a nested tree (assemblies + parts)."""
    if not nodes:
        return 0
    return sum(1 + count_tree(n.get("children") or []) for n in nodes)
