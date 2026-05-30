"""Unit tests for the exploded-view / arrows / line-style-preset features.

Covers the pure-Python pieces that don't need a running server or a STEP
file on disk:
  * ifu.arrows  -- straight + rotation arrow projection, Rodrigues rotation
  * ifu.presets -- built-ins, style resolution, preview swatch
  * t5_hlr_vector.apply_explode -- per-solid translation keeps idx/label and
    moves the right solid (the 3D<->2D index linchpin), using OCP box prims.

Run: pytest tests/test_explode_arrows_presets.py -v
"""
import math
import pytest

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# ifu.arrows
# --------------------------------------------------------------------------

def test_straight_arrow_projects_to_shaft_and_head():
    from ifu import arrows
    # x_axis = model X -> screen u, y_axis = model Z -> screen v
    svg, bbox = arrows.render_arrows_svg(
        [{"type": "straight", "anchor": [0, 0, 0], "dir": [1, 0, 0],
          "length": 50}],
        (1, 0, 0), (0, 0, 1))
    assert svg and "layer-arrows" in svg
    # a straight arrow => one shaft <path> + one filled head <path>
    assert svg.count("<path") == 2
    # u-extent runs the full 50mm length of the arrow
    assert bbox is not None and bbox[2] == pytest.approx(50, abs=1.0)


def test_rotation_arrow_emits_arc_and_head():
    from ifu import arrows
    svg, bbox = arrows.render_arrows_svg(
        [{"type": "rotation", "center": [0, 0, 0], "axis": [0, 0, 1],
          "radius": 20, "sweep": 270}],
        (1, 0, 0), (0, 1, 0))
    assert svg and "<path" in svg
    # arc spans roughly the radius in each direction
    assert bbox is not None
    assert (bbox[2] - bbox[0]) > 20


def test_rotate_point_rodrigues_90_about_z():
    from ifu import arrows
    p = arrows.rotate_point((1, 0, 0), (0, 0, 1), 90)
    assert p[0] == pytest.approx(0, abs=1e-9)
    assert p[1] == pytest.approx(1, abs=1e-9)
    assert p[2] == pytest.approx(0, abs=1e-9)


def test_no_arrows_returns_empty():
    from ifu import arrows
    svg, bbox = arrows.render_arrows_svg([], (1, 0, 0), (0, 1, 0))
    assert svg == "" and bbox is None


# --------------------------------------------------------------------------
# ifu.presets
# --------------------------------------------------------------------------

def test_builtin_presets_present():
    from ifu import presets
    ids = [p["id"] for p in presets.list_all()]
    for want in ("crisp_technical", "bold_outline", "soft_pencil", "blueprint"):
        assert want in ids
    assert presets.DEFAULT_PRESET_ID in ids


def test_resolve_styles_only_known_categories():
    from ifu import presets
    st = presets.resolve_styles("bold_outline")
    assert st["outline_v"]["width"] == pytest.approx(1.20)
    # every key returned must be a real edge category
    assert set(st).issubset(set(presets.CATEGORIES))


def test_resolve_styles_unknown_returns_none():
    from ifu import presets
    assert presets.resolve_styles("does_not_exist") is None
    assert presets.resolve_styles(None) is None


def test_preview_svg_reflects_stroke_colour():
    from ifu import presets
    svg = presets.preview_svg("blueprint")
    assert svg.startswith("<?xml") and "<path" in svg
    # blueprint's silhouette stroke is a blue; it should appear in the swatch
    assert "#0b3d91" in svg


# --------------------------------------------------------------------------
# t5_hlr_vector.apply_explode (needs OCP, but only cheap box primitives)
# --------------------------------------------------------------------------

def _two_boxes():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 40, 40).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(100, 0, 0), 40, 40, 40).Shape()
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    bld.Add(comp, a)
    bld.Add(comp, b)
    return comp


def _solid_xrange(solid):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    bb = Bnd_Box()
    BRepBndLib.Add_s(solid, bb)
    xmin, _ymin, _zmin, xmax, _ymax, _zmax = bb.Get()
    return xmin, xmax


def test_apply_explode_moves_named_solid_only():
    import t5_hlr_vector as t5
    comp = _two_boxes()
    solids = t5.split_solids(comp)
    assert [s[1] for s in solids] == ["part_000", "part_001"]

    _new_comp, moved = t5.apply_explode(solids, {1: (60, 0, 0)})
    # idx/label preserved, order preserved
    assert [s[0] for s in moved] == [0, 1]
    assert [s[1] for s in moved] == ["part_000", "part_001"]
    # part 0 untouched; part 1 shifted +60 in X
    x0 = _solid_xrange(moved[0][2])
    x1 = _solid_xrange(moved[1][2])
    assert x0 == pytest.approx((0.0, 40.0), abs=1e-6)
    assert x1 == pytest.approx((160.0, 200.0), abs=1e-6)


def test_apply_explode_accepts_string_keys():
    import t5_hlr_vector as t5
    comp = _two_boxes()
    solids = t5.split_solids(comp)
    # JSON object keys arrive as strings -- apply_explode must handle both
    _comp, moved = t5.apply_explode(solids, {"1": (0, 70, 0)})
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    bb = Bnd_Box()
    BRepBndLib.Add_s(moved[1][2], bb)
    _xmin, ymin, _zmin, _xmax, ymax, _zmax = bb.Get()
    assert ymin == pytest.approx(70.0, abs=1e-6)
    assert ymax == pytest.approx(110.0, abs=1e-6)


def test_apply_explode_empty_is_noop():
    import t5_hlr_vector as t5
    comp = _two_boxes()
    solids = t5.split_solids(comp)
    new_comp, same = t5.apply_explode(solids, None)
    assert new_comp is None and same is solids
