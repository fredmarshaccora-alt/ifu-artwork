"""F.5 Editor route + breadcrumb tests."""
from __future__ import annotations


def test_home_route_is_reachable(page):
    """Set hash to '#/'; Home screen mounts.  (Empty hash still
    shows the legacy editor for now -- the redirect-to-Home is
    deferred until the editor is fully migrated.)"""
    page.evaluate("location.hash = '#/'")
    page.wait_for_timeout(400)
    has_home = page.evaluate(
        "() => !!document.querySelector('.app-topbar .crumbs .current') && "
        "document.querySelector('.app-topbar .crumbs .current').textContent "
        "=== 'Home'")
    assert has_home, "navigating to '#/' should mount the Home screen"


def test_editor_route_shows_breadcrumb(page):
    """Navigate to a figure's editor route; breadcrumb appears above
    the legacy header."""
    # Set up: project + figure
    seed = page.evaluate("""async () => {
        const pr = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F5-route-test'}),
        });
        const proj = await pr.json();
        const fr = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F5-route-figure',
                                    source_id: 'siderail',
                                    project_id: proj.id}),
        });
        return {proj, fig: await fr.json()};
    }""")
    pid = seed["proj"]["id"]
    fid = seed["fig"]["id"]
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(800)
        info = page.evaluate("""() => {
            const crumb = document.getElementById('editor-breadcrumb');
            if (!crumb) return null;
            return {
                links: Array.from(crumb.querySelectorAll('a'))
                            .map(a => ({href: a.getAttribute('href'),
                                         text: a.textContent})),
                current: crumb.querySelector('.current')?.textContent,
            };
        }""")
        assert info, "breadcrumb missing in editor route"
        assert any(l["href"] == "#/" for l in info["links"]), \
            f"no home link in breadcrumb: {info}"
        # Project link
        assert any(l["href"].startswith("#/project/")
                    and l["text"] == "F5-route-test"
                    for l in info["links"]), \
            f"no project link: {info}"
        assert info["current"] == "F5-route-figure", \
            f"current crumb wrong: {info['current']!r}"
    finally:
        page.evaluate(
            "(pid) => fetch(API_BASE + '/api/projects/' + encodeURIComponent(pid)"
            " + '?cascade=1', {method: 'DELETE'})", pid)


def test_leaving_editor_removes_breadcrumb(page):
    """Navigate away from editor route -> breadcrumb is gone."""
    seed = page.evaluate("""async () => {
        const pr = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F5-cleanup-test'}),
        });
        const proj = await pr.json();
        const fr = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F5-cleanup-figure',
                                    source_id: 'siderail',
                                    project_id: proj.id}),
        });
        return {proj, fig: await fr.json()};
    }""")
    pid = seed["proj"]["id"]
    fid = seed["fig"]["id"]
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(800)
        page.evaluate("location.hash = '#/'")
        page.wait_for_timeout(400)
        gone = page.evaluate(
            "() => !document.getElementById('editor-breadcrumb')")
        assert gone, "breadcrumb survived navigating away"
    finally:
        page.evaluate(
            "(pid) => fetch(API_BASE + '/api/projects/' + encodeURIComponent(pid)"
            " + '?cascade=1', {method: 'DELETE'})", pid)
