"""Backtest for bug #24 (Apply matches live highlight).

Apply now uses the same data path as the transient silhouette overlay
(the rasterised footprint).  A persistent silhouette path appears in
the Applied Styles list, with the same colour/width settings.
"""
from __future__ import annotations
import pytest


def test_applied_style_appears_in_list(page):
    """Apply a style; verify the Applied Styles sidebar list grows by 1."""
    page.evaluate("""() => {
        localStorage.removeItem('partStyles_siderail');
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1500)
    page.evaluate("""() => {
        window.togglePartHighlight(7, {append:false});
        document.getElementById('btn-apply-style').click();
    }""")
    page.wait_for_timeout(400)
    info = page.evaluate("""() => {
        const list = document.getElementById('applied-styles-list');
        const rows = list ? list.querySelectorAll('li').length : 0;
        const stored = JSON.parse(localStorage.getItem('partStyles_siderail') || '{}');
        return {rows, stored_keys: Object.keys(stored)};
    }""")
    assert info["rows"] == 1, \
        f"applied-styles-list should have 1 entry, got {info['rows']}"
    assert info["stored_keys"] == ["7"], \
        f"localStorage should record part 7, got {info['stored_keys']}"


def test_delete_removes_persistent_silhouette(page):
    """Clicking the ✕ delete button removes the persistent overlay."""
    page.evaluate("""() => {
        localStorage.removeItem('partStyles_siderail');
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1500)
    page.evaluate("""() => {
        window.togglePartHighlight(7, {append:false});
        document.getElementById('btn-apply-style').click();
    }""")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
        // Click the delete button (the second <button> in the row)
        const row = document.getElementById('applied-styles-list').querySelector('li');
        const btns = row.querySelectorAll('button');
        btns[btns.length - 1].click();
    }""")
    page.wait_for_timeout(400)
    info = page.evaluate("""() => {
        const list = document.getElementById('applied-styles-list');
        const svg = document.querySelector('.svg-pane.active svg');
        const persist = svg.querySelector('g.layer-persistent-silhouette');
        return {
            rows: list ? list.querySelectorAll('li').length : 0,
            persist_paths: persist ? persist.querySelectorAll('path').length : 0,
        };
    }""")
    # After delete: list has the "none yet" placeholder (1 row) and no
    # persistent paths
    assert info["persist_paths"] == 0, \
        f"persistent silhouette path should be gone after delete, got {info}"
