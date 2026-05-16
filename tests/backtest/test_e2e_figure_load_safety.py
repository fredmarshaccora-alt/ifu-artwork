"""Regression tests for figure-load destructiveness.

Two real bugs hit in production after Phase A landed:
  - loading a figure with no styles wiped the user's existing styles
  - loading a figure with a view_id that doesn't exist for the current
    source blanked the canvas

Both protected here so future refactors can't re-introduce them.
"""
from __future__ import annotations
import pytest


def test_load_empty_figure_does_not_wipe_existing_styles(page):
    """Apply a per-part style, save a NEW figure with no styles, then
    load the new figure -- existing localStorage styles must survive."""
    # Seed: apply a teal stroke to part 5 on siderail
    page.evaluate("""() => {
        const f = document.getElementById('file-sel');
        f.value = 'siderail';
        f.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1200)
    page.evaluate("""() => {
        window.togglePartHighlight(5, {append:false});
        const btn = document.getElementById('btn-apply-style');
        if (btn) btn.click();
    }""")
    page.wait_for_timeout(300)
    styles_before = page.evaluate(
        "() => JSON.parse(localStorage.getItem('partStyles_siderail') || '{}')")
    assert styles_before, "test setup failed: no styles after apply"
    n_before = len(styles_before)

    # Create a "blank" figure via API and try to load it
    fig = page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                name: 'empty-load-test',
                source_id: 'siderail',
                view_id: 'iso',
                styles_per_part: {},
                selection: [],
            }),
        });
        return await r.json();
    }""")
    # Auto-accept the confirm() dialog
    page.on("dialog", lambda d: d.accept())
    page.evaluate("(f) => window._loadFigureIntoEditor(f)", fig)
    page.wait_for_timeout(300)
    styles_after = page.evaluate(
        "() => JSON.parse(localStorage.getItem('partStyles_siderail') || '{}')")
    assert len(styles_after) >= n_before, \
        f"loading empty figure wiped styles: was {n_before}, now {len(styles_after)}"
    # Clean up
    page.evaluate(
        "(fid) => fetch(API_BASE + '/api/figures/' + fid, {method: 'DELETE'})",
        fig["id"])


def test_load_figure_with_bad_view_id_does_not_blank_canvas(page):
    """A figure saved with view_id='__live__' but no Live view currently
    on the source should leave the canvas on its existing view."""
    page.evaluate("""() => {
        const f = document.getElementById('file-sel');
        f.value = 'siderail';
        f.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1200)
    # Get the active view before
    before = page.evaluate("document.getElementById('view-sel').value")
    assert before, "no view selected before load"

    # Construct a synthetic figure with a view_id that doesn't exist
    page.on("dialog", lambda d: d.accept())
    page.evaluate("""() => window._loadFigureIntoEditor({
        id: 'fake',
        name: 'bad-view-test',
        source_id: 'siderail',
        view_id: '__live__',
        selection: [],
        styles_per_part: {},
    })""")
    page.wait_for_timeout(300)
    after = page.evaluate("document.getElementById('view-sel').value")
    # Either still on the original view, OR fell back to a valid one --
    # but NEVER to an unknown one
    valid = page.evaluate("""() => Array.from(
        document.getElementById('view-sel').options).map(o => o.value)""")
    assert after in valid, \
        f"figure load left view-sel at unknown value {after!r}"
