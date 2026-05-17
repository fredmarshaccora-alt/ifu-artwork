"""F.4 Project workspace screen smoke tests."""
from __future__ import annotations
import json


def test_project_screen_loads_and_shows_breadcrumb(page):
    """Create a project, navigate to its route, verify the screen
    shows the project name in the breadcrumb."""
    proj = page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F4-test-proj',
                                    description: 'Phase F.4 test'}),
        });
        return await r.json();
    }""")
    pid = proj["id"]
    try:
        page.evaluate(f"location.hash = '#/project/{pid}'")
        page.wait_for_timeout(500)
        info = page.evaluate("""() => {
            const h1 = document.querySelector('.app-topbar .crumbs');
            return {
                text: h1 ? h1.textContent : null,
                has_home_link: !!document.querySelector('.app-topbar .crumbs a[href="#/"]'),
                // Phase 3: workspace is a Views grid now; the placeholder
                // card prompts for a NEW VIEW (or new figure, accept either
                // copy because the placeholder text varies during the
                // structural transition).
                has_placeholder: !!document.querySelector('.card.placeholder'),
            };
        }""")
        assert info["text"] and "F4-test-proj" in info["text"], \
            f"breadcrumb wrong: {info['text']!r}"
        assert info["has_home_link"], "no Home link in breadcrumb"
        assert info["has_placeholder"], "no new-view / new-figure placeholder card"
    finally:
        page.evaluate(
            "(pid) => fetch(API_BASE + '/api/projects/' + encodeURIComponent(pid)"
            " + '?cascade=1', {method: 'DELETE'})", pid)


def test_project_screen_unknown_id_shows_stub(page):
    """Navigating to a project that doesn't exist shows a "not found" stub
    instead of crashing or showing the legacy editor."""
    page.evaluate("location.hash = '#/project/does-not-exist'")
    page.wait_for_timeout(400)
    text = page.evaluate("document.getElementById('app-root').textContent")
    assert "not found" in text.lower(), \
        f"expected 'not found' stub, got: {text[:200]!r}"


def test_project_screen_lists_attached_figures(page):
    """Create project, attach a figure, project route shows it."""
    proj = page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F4-figs-test'}),
        });
        return await r.json();
    }""")
    pid = proj["id"]
    fig = page.evaluate(f"""async () => {{
        const r = await fetch(API_BASE + '/api/figures', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: 'F4-figs-content',
                                    source_id: 'siderail',
                                    project_id: '{pid}'}}),
        }});
        return await r.json();
    }}""")
    try:
        page.evaluate(f"location.hash = '#/project/{pid}'")
        page.wait_for_timeout(500)
        names = page.evaluate("""() => Array.from(
            document.querySelectorAll('.card .card-title'))
            .map(n => n.textContent)""")
        assert "F4-figs-content" in names, \
            f"figure not shown in project: {names}"
    finally:
        page.evaluate(
            "(pid) => fetch(API_BASE + '/api/projects/' + encodeURIComponent(pid)"
            " + '?cascade=1', {method: 'DELETE'})", pid)
