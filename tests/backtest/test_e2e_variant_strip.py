"""Variant strip in editor sidebar.

When the user opens a figure under a View, the left sidebar shows a
vertical strip of thumbnail cards -- one per highlight variant of
that view -- plus a "+" card to add a new variant.  Clicking the
"+" card creates a fresh figure that inherits the view's camera and
hops the route to it.  Auto-save handles persistence so switching
variants is safe.
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


def test_variant_strip_renders_card_per_variant(page):
    seed = _seed(page, 'VS-render', n_variants=3)
    pid = seed['proj']['id']
    vid = seed['view']['id']
    first_fid = seed['figs'][0]['id']
    try:
        page.evaluate(f"location.hash = "
                       f"'#/project/{pid}/view/{vid}/figure/{first_fid}'")
        page.wait_for_timeout(1800)   # let figure load + strip render
        info = page.evaluate("""() => {
            const strip = document.getElementById('variants-strip');
            if (!strip) return null;
            const cards = strip.querySelectorAll('.variant-card');
            const adds = strip.querySelectorAll('.variant-card.add');
            const active = strip.querySelectorAll('.variant-card.is-active');
            const variantNames = Array.from(
                strip.querySelectorAll('.variant-card .variant-name'))
                .map(n => n.textContent);
            return {
                strip_visible: getComputedStyle(strip).display !== 'none',
                card_count: cards.length,
                add_count: adds.length,
                active_count: active.length,
                variants: variantNames,
            };
        }""")
        assert info, "variants-strip element missing"
        assert info['strip_visible'], "strip not visible in project mode"
        # 3 variants + 1 add = 4 cards
        assert info['card_count'] == 4, \
            f"expected 4 cards (3 variants + add), got {info['card_count']}"
        assert info['add_count'] == 1, \
            f"expected exactly 1 add-card, got {info['add_count']}"
        assert info['active_count'] == 1, \
            f"expected exactly 1 active card, got {info['active_count']}"
        assert set(info['variants']) == {'Variant 1', 'Variant 2', 'Variant 3'}, \
            f"unexpected variant names: {info['variants']}"
    finally:
        _cleanup(page, pid)


def test_plus_card_creates_new_variant(page):
    seed = _seed(page, 'VS-plus', n_variants=1)
    pid = seed['proj']['id']
    vid = seed['view']['id']
    first_fid = seed['figs'][0]['id']
    try:
        page.evaluate(f"location.hash = "
                       f"'#/project/{pid}/view/{vid}/figure/{first_fid}'")
        page.wait_for_timeout(1800)
        # Click the + add card
        page.evaluate("""() => {
            const btn = document.querySelector(
                '#variants-strip .variant-card.add');
            btn.click();
        }""")
        # Wait for the new figure to be created and the route to change
        page.wait_for_timeout(2500)
        # Verify a second figure now exists under the view
        n_figs = page.evaluate(f"""async () => {{
            const r = await fetch(API_BASE + '/api/views/{vid}/figures');
            return ((await r.json()).figures || []).length;
        }}""")
        assert n_figs == 2, f"expected 2 figures after + click, got {n_figs}"
        # Hash should now point at the newly-created figure
        hash_ = page.evaluate("location.hash")
        assert f"/view/{vid}/figure/" in hash_, \
            f"didn't navigate to the new variant: {hash_}"
        # And the variant id in the URL is NOT the original
        new_fid = hash_.split('/')[-1]
        assert new_fid != first_fid, \
            f"hopped to same figure: {new_fid}"
    finally:
        _cleanup(page, pid)


def test_view_screen_redirects_to_editor_on_first_figure(page):
    seed = _seed(page, 'VS-redirect', n_variants=1)
    pid = seed['proj']['id']
    vid = seed['view']['id']
    fid = seed['figs'][0]['id']
    try:
        # Navigate to ViewScreen (the path that used to render figures
        # grid).  Should redirect to editor on the view's first figure.
        page.evaluate(f"location.hash = '#/project/{pid}/view/{vid}'")
        page.wait_for_timeout(1500)
        hash_ = page.evaluate("location.hash")
        assert hash_ == f"#/project/{pid}/view/{vid}/figure/{fid}", \
            f"ViewScreen should redirect to editor: {hash_}"
    finally:
        _cleanup(page, pid)
