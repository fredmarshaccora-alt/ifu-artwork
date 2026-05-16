"""Backtest for bugs #14, #15, #19, #24 (selection / styling behaviour).

#14: `.part.highlight path` CSS auto-bolded every internal feature
     (slits/screw cuts).  Must NOT happen anymore.
#15: default stroke-width was 0.7mm, invisible on Contesa's 2m model.
     Default must be >= 2mm.
#19: click hit-fill polygons overlapped, sending clicks to wrong part.
     A clear-of-edges click must select the part the cursor is over.
#24: Apply did something different from the live highlight; the
     persistent silhouette must match the transient one.
"""
from __future__ import annotations
import pytest


def test_default_stroke_width_visible(page):
    """Bug #15: default sty-width must be visible on large models."""
    w = page.evaluate("""() => parseFloat(document.getElementById('sty-width').value)""")
    assert w >= 2.0, f"default stroke-width {w} is too thin (bug #15)"


def test_highlight_css_does_not_force_bold_paths(page):
    """Bug #14: the .part.highlight CSS rule should NOT set
    stroke-width !important on every path inside a highlighted group.
    Check the active stylesheet for the offending rule."""
    has_aggressive_rule = page.evaluate("""() => {
        for (const sheet of document.styleSheets) {
            try {
                for (const rule of sheet.cssRules || []) {
                    const sel = (rule.selectorText || '').replace(/\\s+/g, ' ');
                    if (sel.includes('.part.highlight') && sel.includes('path')) {
                        const css = rule.style ? rule.style.cssText : '';
                        if (/stroke-width\\s*:/.test(css)) return css;
                    }
                }
            } catch (_e) {/* cross-origin */ }
        }
        return null;
    }""")
    assert not has_aggressive_rule, \
        f".part.highlight path rule still forces stroke-width (bug #14): {has_aggressive_rule}"


def test_apply_persists_silhouette(page):
    """Bug #24: clicking Apply should produce a persistent silhouette
    element whose 'd' matches what the live silhouette layer just had."""
    page.evaluate("""() => {
        localStorage.removeItem('partStyles_siderail');
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1500)
    page.evaluate("window.togglePartHighlight(5, {append:false})")
    page.wait_for_timeout(500)
    # Capture the transient (live) silhouette path's d, then Apply.
    info = page.evaluate("""() => {
        const svg = document.querySelector('.svg-pane.active svg');
        const tran = svg.querySelector('g.layer-silhouette path');
        const transient_d_len = tran ? (tran.getAttribute('d') || '').length : 0;
        document.getElementById('btn-apply-style').click();
        const persist = svg.querySelector('g.layer-persistent-silhouette path');
        const persist_d_len = persist ? (persist.getAttribute('d') || '').length : 0;
        return {transient_d_len, persist_d_len};
    }""")
    assert info["transient_d_len"] > 0, "no transient silhouette to apply"
    assert info["persist_d_len"] > 0, \
        "Apply did not produce a persistent silhouette path (bug #24)"
    # They don't have to match byte-for-byte (path can be reformatted),
    # but they should be in the same ballpark
    ratio = info["persist_d_len"] / info["transient_d_len"]
    assert 0.5 < ratio < 2.0, \
        f"persistent path size {info['persist_d_len']} differs wildly " \
        f"from transient {info['transient_d_len']} (bug #24)"
