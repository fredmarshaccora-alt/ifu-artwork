"""Backtest for bug #18 (slider drag DOM thrash).

Dragging a style slider used to fire applyHighlights on every `input`
event (60+ times/sec) which walked every .part element in the SVG.
Verify that style controls now route through restyleSilhouetteOnly and
do NOT trigger a full applyHighlights call."""
from __future__ import annotations
import pytest


def test_slider_drag_does_not_call_applyHighlights(page):
    """Instrument applyHighlights and drag the width slider.  Assert
    applyHighlights is NOT called for slider input events."""
    page.evaluate("""() => {
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1500)
    page.evaluate("window.togglePartHighlight(5, {append:false})")
    page.wait_for_timeout(500)
    # Instrument
    page.evaluate("""() => {
        window.__appliedHighlightCount = 0;
        const orig = window.applyHighlights;
        window.applyHighlights = function() {
            window.__appliedHighlightCount++;
            return orig.apply(this, arguments);
        };
    }""")
    # Drive slider with 30 input events
    page.evaluate("""() => {
        const slider = document.getElementById('sty-width');
        for (let i = 0; i < 30; i++) {
            slider.value = String(3 + (i % 5));
            slider.dispatchEvent(new Event('input', {bubbles: true}));
        }
    }""")
    page.wait_for_timeout(400)
    n = page.evaluate("window.__appliedHighlightCount")
    assert n < 3, \
        f"slider drag triggered applyHighlights {n}x (bug #18) -- " \
        f"should be 0 (slider routed through restyleSilhouetteOnly)"
