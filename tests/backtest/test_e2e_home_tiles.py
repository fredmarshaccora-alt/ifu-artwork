"""Phase 4: Home page renders project tiles with visual previews.

Two contracts:
  1. A project with at least one View shows the view's thumbnail as
     the tile preview.
  2. A project with no Views shows a monogram tile (initials over
     teal gradient).
"""
from __future__ import annotations
import base64


# 1x1 PNG so we have SOMETHING to PUT for the view thumbnail
TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
TINY_PNG_URL = "data:image/png;base64," + TINY_PNG


def _seed_project_with_view(page, name):
    return page.evaluate(f"""async () => {{
        const pr = await fetch(API_BASE + '/api/projects', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{name}',
                                    primary_source_id: 'siderail'}}),
        }});
        const proj = await pr.json();
        const vr = await fetch(API_BASE + '/api/views', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{project_id: proj.id,
                                    source_id: 'siderail',
                                    name: 'tile-test-view'}}),
        }});
        const view = await vr.json();
        // Plant a thumbnail
        await fetch(API_BASE + '/api/views/' + view.id + '/thumbnail', {{
            method: 'PUT',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{data_url: '{TINY_PNG_URL}'}}),
        }});
        return {{proj, view}};
    }}""")


def _seed_empty_project(page, name):
    return page.evaluate(f"""async () => {{
        const pr = await fetch(API_BASE + '/api/projects', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{name}',
                                    primary_source_id: 'siderail'}}),
        }});
        return await pr.json();
    }}""")


def _cleanup(page, pid):
    page.evaluate(f"""async () => {{
        await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                      {{method: 'DELETE'}});
    }}""")


def test_project_card_uses_view_thumbnail_when_view_exists(page):
    seed = _seed_project_with_view(page, 'P4-with-thumb')
    pid = seed['proj']['id']
    vid = seed['view']['id']
    try:
        # Force re-render -- empty hash now auto-redirects to #/ at
        # page load, so setting hash='#/' is a no-op if the page is
        # already there.  Call renderRoute() directly so HomeScreen
        # re-fetches the freshly-seeded project + view.
        page.evaluate("location.hash = '#/'")
        page.evaluate("window.IFU_APP && window.IFU_APP.renderRoute()")
        page.wait_for_timeout(1200)
        info = page.evaluate(f"""() => {{
            const cards = document.querySelectorAll('.card.project-card');
            for (const c of cards) {{
                const t = c.querySelector('.card-title')?.textContent || '';
                if (t.includes('P4-with-thumb')) {{
                    const img = c.querySelector('img');
                    return {{
                        has_img: !!img,
                        src: img?.getAttribute('src') || '',
                    }};
                }}
            }}
            return null;
        }}""")
        assert info, "P4-with-thumb card not rendered"
        assert info['has_img'], "view-thumbnail <img> missing on project card"
        assert f"/api/views/{vid}/thumbnail" in info['src'], \
            f"img src not pointing at view thumbnail: {info['src']!r}"
    finally:
        _cleanup(page, pid)


def test_project_card_monogram_when_no_view(page):
    proj = _seed_empty_project(page, 'Mono Test Proj')
    pid = proj['id']
    try:
        page.evaluate("location.hash = '#/'")
        page.evaluate("window.IFU_APP && window.IFU_APP.renderRoute()")
        page.wait_for_timeout(1000)
        text = page.evaluate("""() => {
            const cards = document.querySelectorAll('.card.project-card');
            for (const c of cards) {
                const t = c.querySelector('.card-title')?.textContent || '';
                if (t.includes('Mono Test Proj')) {
                    // Monogram is the first child div with no <img>
                    const tile = c.firstElementChild;
                    if (!tile || tile.tagName === 'IMG') return null;
                    return tile.textContent.trim();
                }
            }
            return null;
        }""")
        # Two-letter initials from "Mono Test Proj" -> "MT" (first two words)
        assert text == 'MT', f"monogram should be 'MT', got {text!r}"
    finally:
        _cleanup(page, pid)


def test_home_new_project_placeholder_present(page):
    page.evaluate("location.hash = '#/'")
    page.wait_for_timeout(400)
    has_new = page.evaluate("""() => {
        const placeholders = document.querySelectorAll(
            '.card-grid .card.placeholder');
        return Array.from(placeholders).some(c =>
            c.textContent.includes('New project'));
    }""")
    assert has_new, "no '+ New project' placeholder card on Home"
