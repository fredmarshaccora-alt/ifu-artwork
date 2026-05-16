"""F.3 Home screen smoke tests.

Navigation in, project grid renders, new-project card creates and
navigates to its project route.
"""
from __future__ import annotations
import pytest


def test_navigating_to_root_mounts_home(page):
    """Setting location.hash = '#/' renders the home grid."""
    page.evaluate("location.hash = '#/'")
    page.wait_for_timeout(500)
    info = page.evaluate("""() => ({
        h1: document.querySelector('.app-topbar .crumbs .current')?.textContent,
        has_grid: !!document.querySelector('.card-grid'),
        has_new_card: !!document.querySelector('.card.placeholder'),
    })""")
    assert info["h1"] == "Home", f"home crumb wrong: {info['h1']!r}"
    assert info["has_grid"], "no project grid"
    assert info["has_new_card"], "no new-project placeholder card"


def test_legacy_header_link_goes_home(page):
    """The legacy editor's header logo links to '#/'."""
    page.evaluate("location.hash = ''")  # legacy default
    page.wait_for_timeout(200)
    href = page.evaluate(
        "() => document.querySelector('header a[href=\"#/\"]')?.getAttribute('href')")
    assert href == "#/", "legacy header missing Home link"


def test_home_lists_projects_from_api(page):
    """Create a project via API, navigate to home, see it in the grid."""
    page.evaluate("""async () => {
        await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F3-test-proj'}),
        });
    }""")
    page.evaluate("location.hash = '#/'")
    # Force re-render in case we were already on home
    page.evaluate("window.IFU_APP.renderRoute()")
    page.wait_for_timeout(500)
    names = page.evaluate("""() => Array.from(
        document.querySelectorAll('.card .card-title'))
        .map(n => n.textContent)""")
    assert "F3-test-proj" in names, \
        f"new project not shown on home: {names}"
    # Cleanup
    page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/projects');
        const data = await r.json();
        for (const p of data.projects) {
            if (p.name === 'F3-test-proj') {
                await fetch(API_BASE + '/api/projects/' + encodeURIComponent(p.id),
                             {method: 'DELETE'});
            }
        }
    }""")
