"""Phase-2 style presets: in project mode the right sidebar shows a
fixed set of preset buttons (Highlight / Caution / Info / Outline only
/ Subtle), and the legacy color/width/opacity controls are hidden.
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


def test_preset_row_visible_in_figure_route(page):
    seed = _seed(page, 'P2-preset-vis')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)
        info = page.evaluate("""() => {
            const row = document.getElementById('preset-row');
            if (!row) return null;
            return {
                display: getComputedStyle(row).display,
                n_buttons: row.querySelectorAll('.preset-btn').length,
                labels: Array.from(row.querySelectorAll('.preset-btn'))
                    .map(b => b.dataset.presetId),
            };
        }""")
        assert info, "preset-row missing"
        assert info['display'] != 'none', \
            f"preset-row hidden: display={info['display']!r}"
        assert info['n_buttons'] == 5, \
            f"expected 5 preset buttons, got {info['n_buttons']}"
        assert info['labels'] == ['highlight', 'caution', 'info',
                                    'outline', 'subtle'], \
            f"unexpected preset set: {info['labels']}"
    finally:
        _cleanup(page, pid)


def test_advanced_style_panel_hidden_in_figure_route(page):
    """The legacy color/width/opacity controls (data-ed-control=
    advanced-styling) must hide in project mode."""
    seed = _seed(page, 'P2-no-pickers')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)
        hidden = page.evaluate("""() => {
            const el = document.getElementById('style-panel');
            return el && getComputedStyle(el).display === 'none';
        }""")
        assert hidden, "advanced #style-panel should be hidden in project mode"
    finally:
        _cleanup(page, pid)


def test_preset_button_persists_style_via_localStorage(page):
    """Clicking a preset with a selection must write the corresponding
    {stroke,width,fillOn,...} object into the partStyles_<sid>
    localStorage key for every selected part idx."""
    seed = _seed(page, 'P2-preset-apply')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)
        # Plant a selection
        page.evaluate("""() => {
            const st = getState('siderail', 'iso');
            if (!st.highlights) st.highlights = new Set();
            st.highlights.add(0);
            if (typeof applyHighlights === 'function') applyHighlights();
        }""")
        page.wait_for_timeout(200)
        # Click the "Caution" preset
        page.evaluate("""() => {
            const btn = document.querySelector(
                '#preset-row .preset-btn[data-preset-id="caution"]');
            btn.click();
        }""")
        page.wait_for_timeout(400)
        # Inspect localStorage
        stored = page.evaluate("""() => {
            return JSON.parse(localStorage.getItem('partStyles_siderail') || '{}');
        }""")
        assert '0' in stored, f"part 0 has no stored style: {stored}"
        s = stored['0']
        # Caution preset stroke is the amber hex
        assert s.get('stroke') == '#b54708', \
            f"wrong stroke: {s.get('stroke')!r}"
        assert s.get('fillOn') is True, \
            f"caution should have fill on: {s}"
        assert s.get('fillColor') == '#fff3e0', \
            f"wrong fill color: {s.get('fillColor')!r}"
    finally:
        _cleanup(page, pid)
        page.evaluate(
            "() => localStorage.removeItem('partStyles_siderail')")
