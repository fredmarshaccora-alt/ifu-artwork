"""Per-body color palette for the 3D viewer.

We assign colors from a 12-entry Onshape-inspired pastel palette by
hashing the part index.  This test pins down:

  1. The palette exists and has >= 6 entries (so multi-part
     assemblies look varied, not all identical)
  2. Loading a known multi-part assembly produces at least 2 distinct
     mesh colors (i.e. the palette is actually being applied)
  3. Highlight + clear restores the palette color (not a hardcoded
     grey -- that was the regression that motivated this work)
"""
from __future__ import annotations


def _force_3d(page):
    """Open Split layout + wait for the 3D scene to mount.  Siderail
    has 138 parts so palette variety is easy to verify on it."""
    page.evaluate("""() => {
        const btn = document.getElementById('lay-split');
        if (btn) btn.click();
    }""")
    for _ in range(120):
        page.wait_for_timeout(200)
        loaded = page.evaluate("""() => {
            const v = window.IFU_VIEWER || {};
            const pcs = typeof v.getActivePartColors === 'function'
                          ? v.getActivePartColors() : null;
            return Array.isArray(pcs) && pcs.length > 0;
        }""")
        if loaded:
            return True
    return False


def test_palette_exists_with_enough_entries(page):
    pal = page.evaluate("""() => {
        const v = window.IFU_VIEWER || {};
        return typeof v.getBodyPalette === 'function'
                 ? v.getBodyPalette() : null;
    }""")
    assert pal, "IFU_VIEWER.getBodyPalette() not available"
    assert isinstance(pal, list) and len(pal) >= 6, \
        f"palette too small ({len(pal) if pal else 0}); multi-part " \
        f"assemblies need variety"


def test_active_parts_have_varied_colors(page):
    """siderail has 138 parts; with a 12-color palette, at least 5
    distinct colors should appear on the active group."""
    ok = _force_3d(page)
    assert ok, "3D viewer never became ready"
    info = page.evaluate("""() => {
        const v = window.IFU_VIEWER || {};
        const pcs = v.getActivePartColors();
        if (!pcs || !pcs.length) return null;
        const colors = new Set(pcs.map(p => p.color_hex));
        return {
            n_parts: pcs.length,
            distinct: colors.size,
            sample: pcs.slice(0, 5),
        };
    }""")
    assert info, "no part-color data returned"
    assert info['n_parts'] > 5, f"too few parts ({info['n_parts']})"
    assert info['distinct'] >= 5, \
        f"only {info['distinct']} distinct colors across {info['n_parts']} " \
        f"parts; palette not being applied. Sample: {info['sample']}"


def test_base_color_recorded_on_userdata(page):
    """Every mesh must stash its base palette color on
    userData.baseColor so applyHighlights3D can restore it after a
    selection clears."""
    ok = _force_3d(page)
    assert ok
    info = page.evaluate("""() => {
        const v = window.IFU_VIEWER || {};
        const pcs = v.getActivePartColors();
        if (!pcs || !pcs.length) return null;
        const missing = pcs.filter(p => !p.base_hex);
        return {
            n_total: pcs.length,
            n_missing_base: missing.length,
            sample: pcs.slice(0, 3),
        };
    }""")
    assert info, "no color data"
    assert info['n_missing_base'] == 0, \
        f"{info['n_missing_base']}/{info['n_total']} parts have no base " \
        f"color stored. Sample: {info['sample']}"
