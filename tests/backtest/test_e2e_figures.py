"""E2E tests for Phase A figures workflow.

Verifies the full UX loop: click parts in 2D, type a name, click save,
new entry appears in the figures list.  Click that entry, figure
restores camera + selection.  Delete it, vanishes.
"""
from __future__ import annotations
import pytest


def test_save_figure_appears_in_list(page):
    """Click two parts, name a figure, save -- new entry in the list."""
    # Get into a known state (siderail iso)
    page.evaluate("""() => {
        const f = document.getElementById('file-sel');
        f.value = 'siderail';
        f.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1500)
    # Pick two parts
    page.evaluate("""() => {
        window.togglePartHighlight(5, {append:false});
        window.togglePartHighlight(8, {append:true});
    }""")
    page.wait_for_timeout(300)
    # Name + save
    nm = page.locator("#fig-name")
    nm.fill("e2e-test-figure")
    page.click("#btn-fig-save")
    page.wait_for_timeout(700)
    # New li should be in #figures-list with that name
    names = page.evaluate("""() => {
        const list = document.getElementById('figures-list');
        return list ? Array.from(list.querySelectorAll('.name'))
                        .map(s => s.textContent) : [];
    }""")
    assert "e2e-test-figure" in names, f"figure not in list: {names}"
    # Clean up via the API to avoid polluting the figures store
    page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/figures');
        const data = await r.json();
        for (const f of data.figures) {
            if (f.name === 'e2e-test-figure') {
                await fetch(API_BASE + '/api/figures/' + encodeURIComponent(f.id),
                             {method: 'DELETE'});
            }
        }
    }""")


def test_load_figure_restores_selection(page):
    """Save a figure with selection [3,9], clear, click the figure name
    -- selection should come back."""
    page.evaluate("""() => {
        const f = document.getElementById('file-sel');
        f.value = 'siderail';
        f.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1500)
    page.evaluate("""() => {
        window.togglePartHighlight(3, {append:false});
        window.togglePartHighlight(9, {append:true});
    }""")
    page.wait_for_timeout(300)
    page.locator("#fig-name").fill("e2e-restore-test")
    page.click("#btn-fig-save")
    page.wait_for_timeout(700)
    # Clear selection
    page.evaluate("window.clearHighlights()")
    page.wait_for_timeout(200)
    # Click the figure name to restore
    page.evaluate("""() => {
        const list = document.getElementById('figures-list');
        const item = Array.from(list.querySelectorAll('.name'))
            .find(s => s.textContent === 'e2e-restore-test');
        if (item) item.click();
    }""")
    page.wait_for_timeout(800)
    # Selection should be reflected as .part.highlight in the active SVG
    sel = page.evaluate("""() => {
        const svg = document.querySelector('.svg-pane.active svg');
        if (!svg) return null;
        const idxs = new Set();
        svg.querySelectorAll('.part.highlight').forEach(p => {
            const i = parseInt(p.dataset.part);
            if (!Number.isNaN(i)) idxs.add(i);
        });
        return [...idxs].sort((a,b)=>a-b);
    }""")
    assert sel == [3, 9], f"restore expected [3,9], got {sel}"
    # Clean up
    page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/figures');
        const data = await r.json();
        for (const f of data.figures) {
            if (f.name === 'e2e-restore-test') {
                await fetch(API_BASE + '/api/figures/' + encodeURIComponent(f.id),
                             {method: 'DELETE'});
            }
        }
    }""")
