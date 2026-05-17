"""Loading overlay + variant-switch reliability.

The user reported that clicking a variant card sometimes left the
main view blank (preview thumb appears but no SVG renders).  Two
pinning contracts:

  1. When a variant click navigates to a new figure, the canvas
     shows a spinner overlay until the render completes -- so the
     user sees that loading is in progress.
  2. After the render completes, the live svg-pane has the .active
     class and at least one <svg> child, regardless of any pan/zoom
     state carried over from the previous variant.
"""
from __future__ import annotations


def _seed(page, slug, n_variants=2):
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
                                    camera: {{eye:[1000,1000,800],
                                                target:[0,0,0]}}}}),
        }});
        const view = await vr.json();
        const figs = [];
        for (let i = 0; i < {n_variants}; i++) {{
            const fr = await fetch(API_BASE + '/api/figures', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    name: 'Variant ' + (i + 1),
                    source_id: 'siderail',
                    project_id: proj.id,
                    view_id: view.id,
                    camera: view.camera,
                }}),
            }});
            const f = await fr.json();
            await fetch(API_BASE + '/api/views/' + view.id
                          + '/figures/' + f.id, {{method: 'POST'}});
            figs.push(f);
        }}
        return {{proj, view, figs}};
    }}""")


def _cleanup(page, pid):
    page.evaluate(f"""async () => {{
        await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                      {{method: 'DELETE'}});
    }}""")


def test_loading_overlay_appears_during_render(page):
    """Navigating into a figure-in-view shows the spinner overlay,
    then hides it once the render completes."""
    seed = _seed(page, 'OV-render', n_variants=1)
    pid = seed['proj']['id']
    vid = seed['view']['id']
    fid = seed['figs'][0]['id']
    try:
        page.evaluate(f"location.hash = "
                       f"'#/project/{pid}/view/{vid}/figure/{fid}'")
        # Within the 350ms-pre-render window OR during the in-flight
        # render, the spinner must be visible.  Poll for up to 1.5s.
        seen_visible = False
        for _ in range(20):
            page.wait_for_timeout(80)
            ov = page.evaluate("""() => {
                const el = document.querySelector(
                    '#canvas-wrap > .canvas-loading-overlay');
                if (!el) return null;
                return {
                    in_dom: true,
                    visible: !el.classList.contains('is-hidden'),
                    label: el.querySelector('.canvas-loading-label')?.textContent,
                };
            }""")
            if ov and ov['visible']:
                seen_visible = True
                break
        assert seen_visible, "spinner overlay never became visible"

        # And after the render finishes (give it generous time), the
        # overlay must hide.
        for _ in range(40):
            page.wait_for_timeout(500)
            hidden = page.evaluate("""() => {
                const el = document.querySelector(
                    '#canvas-wrap > .canvas-loading-overlay');
                return !el || el.classList.contains('is-hidden');
            }""")
            if hidden:
                return   # success
        raise AssertionError(
            "spinner overlay was still visible after 20s -- render hung?")
    finally:
        _cleanup(page, pid)


def test_variant_switch_shows_active_svg(page):
    """Click variant 1 -> wait -> click variant 2.  After the second
    click + render, the live svg-pane must be .active AND contain an
    <svg>.  This is the symptom the user hit: nothing in main view."""
    seed = _seed(page, 'OV-switch', n_variants=2)
    pid = seed['proj']['id']
    vid = seed['view']['id']
    fid1 = seed['figs'][0]['id']
    fid2 = seed['figs'][1]['id']
    try:
        # Open variant 1
        page.evaluate(f"location.hash = "
                       f"'#/project/{pid}/view/{vid}/figure/{fid1}'")
        # Wait for the first render to land
        for _ in range(40):
            page.wait_for_timeout(500)
            has_svg = page.evaluate("""() => {
                const pane = document.querySelector(
                    '.svg-pane.active[data-view="__live__"]');
                return !!pane && !!pane.querySelector('svg');
            }""")
            if has_svg: break

        # Click variant 2 in the strip
        clicked = page.evaluate(f"""() => {{
            const cards = document.querySelectorAll(
                '#variants-strip .variant-card');
            for (const c of cards) {{
                const nm = c.querySelector('.variant-name')?.textContent || '';
                if (nm === 'Variant 2') {{ c.click(); return true; }}
            }}
            return false;
        }}""")
        assert clicked, "Variant 2 card not found"

        # Wait for variant 2's render to land
        info = None
        for _ in range(40):
            page.wait_for_timeout(500)
            info = page.evaluate("""() => {
                const all = document.querySelectorAll(
                    '.svg-pane[data-view="__live__"]');
                const active = document.querySelector(
                    '.svg-pane.active[data-view="__live__"]');
                return {
                    panes: all.length,
                    has_active: !!active,
                    has_svg: !!(active && active.querySelector('svg')),
                    n_paths: active ? active.querySelectorAll('svg path').length : 0,
                };
            }""")
            if info['has_svg'] and info['n_paths'] > 0:
                break

        assert info['has_active'], \
            "no active live pane after variant switch"
        assert info['has_svg'], \
            "active pane has no <svg> after variant switch"
        assert info['n_paths'] > 0, \
            f"<svg> present but no <path> -- empty render? ({info})"
        # URL should reflect the new variant
        assert page.evaluate("location.hash").endswith('/' + fid2), \
            "URL didn't update to variant 2"
    finally:
        _cleanup(page, pid)
