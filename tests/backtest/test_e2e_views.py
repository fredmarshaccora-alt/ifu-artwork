"""Phase-3 views UI: project workspace shows Views (not Figures
directly), clicking a view card opens ViewScreen, which lists the
figures attached to that view.
"""
from __future__ import annotations


def _seed(page, slug):
    """Create a project + a view + a figure attached to the view, all
    server-side via the API.  Returns the three ids."""
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
                                    name: 'Front-right iso',
                                    camera: {{eye: [1,1,1], target:[0,0,0]}}}}),
        }});
        const view = await vr.json();
        const fr = await fetch(API_BASE + '/api/figures', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{slug}-fig',
                                    source_id: 'siderail',
                                    project_id: proj.id}}),
        }});
        const fig = await fr.json();
        await fetch(
          API_BASE + '/api/views/' + view.id + '/figures/' + fig.id,
          {{method: 'POST'}});
        return {{proj, view, fig}};
    }}""")


def _cleanup(page, pid):
    page.evaluate(f"""async () => {{
        await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                      {{method: 'DELETE'}});
    }}""")


def test_project_workspace_shows_views(page):
    seed = _seed(page, 'V-ws')
    pid = seed['proj']['id']
    vid = seed['view']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}'")
        page.wait_for_timeout(800)
        info = page.evaluate("""() => {
            // Look for a section titled "Views"
            const titles = Array.from(
                document.querySelectorAll('.section-title'))
                .map(el => el.textContent);
            const cards = document.querySelectorAll('.card-grid .card');
            const titles_in_cards = Array.from(cards)
                .map(c => c.querySelector('.card-title')?.textContent || '');
            return { section_titles: titles, card_titles: titles_in_cards };
        }""")
        assert any('Views' in t for t in info['section_titles']), \
            f"no Views section: {info['section_titles']}"
        assert 'Front-right iso' in info['card_titles'], \
            f"view card missing: {info['card_titles']}"
    finally:
        _cleanup(page, pid)


def test_view_card_click_opens_editor_on_first_variant(page):
    """Phase-3-rev: ViewScreen is now a redirector.  Clicking a view
    card lands directly on the editor for the first figure under
    that view (and the variant strip in the editor sidebar lists the
    siblings)."""
    seed = _seed(page, 'V-click')
    pid = seed['proj']['id']
    vid = seed['view']['id']
    fid = seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}'")
        page.wait_for_timeout(800)
        # Click the view card
        page.evaluate("""() => {
            const cards = document.querySelectorAll('.card-grid .card');
            for (const c of cards) {
                if (c.classList.contains('placeholder')) continue;
                const t = c.querySelector('.card-title')?.textContent || '';
                if (t.includes('Front-right iso')) { c.click(); return; }
            }
        }""")
        page.wait_for_timeout(1200)
        hash_ = page.evaluate("location.hash")
        assert hash_ == f"#/project/{pid}/view/{vid}/figure/{fid}", \
            f"expected editor redirect to first variant; got {hash_}"
        # Variant strip should be populated
        n_cards = page.evaluate(
            "() => document.querySelectorAll('#variants-strip .variant-card').length")
        # 1 figure + 1 add card
        assert n_cards >= 2, \
            f"variant strip should have at least the figure + add card; got {n_cards}"
    finally:
        _cleanup(page, pid)


def test_new_view_card_navigates_to_new_view(page):
    seed = _seed(page, 'V-newcard')
    pid = seed['proj']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}'")
        page.wait_for_timeout(600)
        # Click the placeholder "+ New view" card
        page.evaluate("""() => {
            const card = document.querySelector('.card-grid .card.placeholder');
            if (card) card.click();
        }""")
        page.wait_for_timeout(500)
        hash_ = page.evaluate("location.hash")
        # ViewScreen with __new__ redirects to editor with __new_view__
        # placeholder figure id.  Either landing is fine.
        assert '__new__' in hash_ or '__new_view__' in hash_, \
            f"new-view didn't route to the new-view flow: {hash_}"
    finally:
        _cleanup(page, pid)
