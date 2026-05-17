"""G.10 polished editor chrome:
  - "Back to project" pill appears in the header when in a figure
    route and links back to the project workspace
  - Figure cards on the project workspace include a thumbnail <img>
    pointing at /api/figures/<fid>/thumbnail and gracefully fall back
    to "no preview yet" when the endpoint 404s
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
        const fr = await fetch(API_BASE + '/api/figures', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{slug}-fig',
                                    source_id: 'siderail',
                                    project_id: proj.id}}),
        }});
        return {{proj, fig: await fr.json()}};
    }}""")


def _cleanup(page, pid):
    page.evaluate(f"""async () => {{
        await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                      {{method: 'DELETE'}});
    }}""")


def test_back_to_project_pill_visible_in_figure_route(page):
    seed = _seed(page, 'G10-pill')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)
        info = page.evaluate("""() => {
            const el = document.querySelector('header .back-to-project');
            return el ? {
                href: el.getAttribute('href'),
                visible: getComputedStyle(el).display !== 'none',
                text: el.textContent.trim(),
            } : null;
        }""")
        assert info is not None, "back-to-project pill not present"
        assert info['visible'], "pill is hidden"
        assert info['href'] == f"#/project/{pid}", \
            f"pill href wrong: {info['href']!r}"
        assert "G10-pill" in info['text'], \
            f"pill text doesn't mention project: {info['text']!r}"
    finally:
        _cleanup(page, pid)


def test_back_to_project_pill_removed_when_navigating_away(page):
    """After leaving the figure route the pill must be torn down so
    Home / Project / Settings views don't inherit a stale exit."""
    seed = _seed(page, 'G10-pill-cleanup')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)
        page.evaluate("location.hash = '#/'")
        page.wait_for_timeout(400)
        n = page.evaluate(
            "() => document.querySelectorAll('header .back-to-project').length")
        assert n == 0, f"pill leaked after navigating away ({n} found)"
    finally:
        _cleanup(page, pid)


def test_figure_card_falls_back_to_placeholder_when_no_thumbnail(page):
    """A figure created without a thumbnail must show the 'no preview
    yet' placeholder, not a broken image icon."""
    seed = _seed(page, 'G10-noimg')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}'")
        page.wait_for_timeout(800)
        info = page.evaluate("""() => {
            const card = document.querySelector('.card.figure-card');
            if (!card) return null;
            // The onerror handler replaces the <img> with a div whose
            // textContent is the placeholder copy.
            return Array.from(card.children).map(c => ({
                tag: c.tagName,
                text: (c.textContent || '').trim(),
            }));
        }""")
        assert info, "no figure cards rendered"
        placeholders = [c for c in info
                         if c['tag'] == 'DIV' and 'no preview yet' in c['text']]
        assert placeholders, f"expected placeholder div, got {info}"
    finally:
        _cleanup(page, pid)


def test_figure_card_shows_img_when_thumbnail_present(page):
    """When the figure has a thumbnail PUT, the card's <img> stays
    in the DOM (onerror never fires).  src points at the thumbnail
    endpoint with a cache-buster."""
    import base64
    # 1x1 PNG
    tiny = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
            "lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    durl = f"data:image/png;base64,{tiny}"

    seed = _seed(page, 'G10-withimg')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        # Upload thumbnail via API
        page.evaluate(f"""async () => {{
            await fetch(API_BASE + '/api/figures/{fid}/thumbnail', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{data_url: '{durl}'}})
            }});
        }}""")
        # Now navigate to the workspace
        page.evaluate(f"location.hash = '#/project/{pid}'")
        page.wait_for_timeout(1000)   # let <img> load
        info = page.evaluate("""() => {
            const card = document.querySelector('.card.figure-card');
            if (!card) return null;
            const img = card.querySelector('img');
            return img ? {
                src: img.getAttribute('src') || '',
                complete: img.complete,
                naturalWidth: img.naturalWidth,
            } : null;
        }""")
        assert info, "card has no img"
        assert f"/api/figures/{fid}/thumbnail" in info['src'], \
            f"src wrong: {info['src']!r}"
        assert "?v=" in info['src'], \
            f"src missing cache-buster: {info['src']!r}"
        assert info['naturalWidth'] >= 1, \
            f"img didn't load (naturalWidth={info['naturalWidth']})"
    finally:
        _cleanup(page, pid)
