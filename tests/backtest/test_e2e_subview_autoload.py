"""When a figure is created under a View and the user opens it in the
editor, the View's SVG must auto-render -- the user shouldn't have to
click "generate 2D" first to see the base drawing.
"""
from __future__ import annotations


def _seed(page, slug):
    return page.evaluate(f"""async () => {{
        const pr = await fetch(API_BASE + '/api/projects', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{slug}-proj',
                                    primary_source_id: 'siderail'}}),
        }});
        const proj = await pr.json();
        const vr = await fetch(API_BASE + '/api/views', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{project_id: proj.id,
                                    source_id: 'siderail',
                                    name: '{slug}-view',
                                    camera: {{
                                        eye:[1000, 1000, 800],
                                        target:[0, 0, 0],
                                        up_axis: 'Z'
                                    }}}}),
        }});
        const view = await vr.json();
        // Figure inherits view's camera
        const fr = await fetch(API_BASE + '/api/figures', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{slug}-fig',
                                    source_id: 'siderail',
                                    project_id: proj.id,
                                    view_id: view.id,
                                    camera: view.camera}}),
        }});
        const fig = await fr.json();
        await fetch(API_BASE + '/api/views/' + view.id
                      + '/figures/' + fig.id, {{method: 'POST'}});
        return {{proj, view, fig}};
    }}""")


def _cleanup(page, pid):
    page.evaluate(f"""async () => {{
        await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                      {{method: 'DELETE'}});
    }}""")


def test_opening_subview_figure_auto_renders_base_svg(page):
    """Navigate to /project/<pid>/view/<vid>/figure/<fid> with a figure
    that carries a camera -- the 2D pane must populate without the
    user clicking generate 2D."""
    seed = _seed(page, 'SV-auto')
    pid = seed['proj']['id']
    vid = seed['view']['id']
    fid = seed['fig']['id']
    try:
        page.evaluate(f"location.hash = "
                       f"'#/project/{pid}/view/{vid}/figure/{fid}'")
        # Wait long enough for the render: figure load 200ms + camera
        # snap 350ms + /api/render call.  Siderail at 0.8mm mesh_defl
        # is ~3-5s cold, hits cache instantly when a previous test
        # already rendered the same camera.  Generous 30s ceiling so
        # the test doesn't false-flag on a busy server.
        for _ in range(60):
            page.wait_for_timeout(500)
            n_paths = page.evaluate("""() => {
                const pane = document.querySelector(
                    '.svg-pane[data-view="__live__"]');
                if (!pane) return 0;
                return pane.querySelectorAll('svg path').length;
            }""")
            if n_paths > 0:
                break
        n_paths = page.evaluate("""() => {
            const pane = document.querySelector(
                '.svg-pane[data-view="__live__"]');
            return pane ? pane.querySelectorAll('svg path').length : 0;
        }""")
        assert n_paths > 0, \
            ("expected the base view SVG to auto-render; "
             "no <path> elements in the live pane after 30s")
    finally:
        _cleanup(page, pid)
