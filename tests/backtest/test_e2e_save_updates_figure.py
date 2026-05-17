"""When the user is in the editor at /#/project/<pid>/figure/<fid> and
clicks the legacy "save" button, it must UPDATE that figure (PUT) --
not create a new one (POST).  The previous behaviour silently spammed
duplicate figures whenever the user tweaked styles and pressed save.

Three contracts:
  1. Editor route pre-fills the figure-name input with the loaded
     figure's name.
  2. The hidden "save as new..." button appears in figure-route mode.
  3. Calling saveCurrentAsFigure() with a figure loaded updates that
     figure in place -- no new figure created.
"""
from __future__ import annotations


def _seed_project_and_figure(page, name):
    return page.evaluate(f"""async () => {{
        const pr = await fetch(API_BASE + '/api/projects', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{name}-proj',
                                    primary_source_id: 'siderail'}}),
        }});
        const proj = await pr.json();
        const fr = await fetch(API_BASE + '/api/figures', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{name: '{name}-fig',
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


def test_figure_name_prefilled_in_legacy_input(page):
    seed = _seed_project_and_figure(page, 'G8-prefill')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)  # let EditorScreen mount + load
        val = page.evaluate(
            "() => document.getElementById('fig-name').value")
        assert val == 'G8-prefill-fig', \
            f"fig-name input not pre-filled, got: {val!r}"
    finally:
        _cleanup(page, pid)


def test_save_as_new_button_visible_in_figure_route(page):
    seed = _seed_project_and_figure(page, 'G8-saveas')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)
        visible = page.evaluate("""() => {
            const b = document.getElementById('btn-fig-save-as');
            if (!b) return false;
            const cs = getComputedStyle(b);
            return cs.display !== 'none' && cs.visibility !== 'hidden';
        }""")
        assert visible, "save-as-new button not visible in figure route"
    finally:
        _cleanup(page, pid)


def test_save_button_updates_existing_figure(page):
    """The critical behavior: saving while a figure is loaded must
    UPDATE that figure (PUT) and not create a duplicate (POST)."""
    seed = _seed_project_and_figure(page, 'G8-update')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        # Baseline figure count for this project
        figs_before = page.evaluate(f"""async () => {{
            const r = await fetch(API_BASE + '/api/projects/{pid}/figures');
            return (await r.json()).figures || [];
        }}""")
        assert len(figs_before) == 1

        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)

        # Trigger save via the legacy save button
        page.evaluate("() => document.getElementById('btn-fig-save').click()")
        page.wait_for_timeout(600)

        figs_after = page.evaluate(f"""async () => {{
            const r = await fetch(API_BASE + '/api/projects/{pid}/figures');
            return (await r.json()).figures || [];
        }}""")
        assert len(figs_after) == 1, \
            f"save created a duplicate figure: {[f['name'] for f in figs_after]}"
        # The same id should still be present (figure updated in place)
        assert figs_after[0]['id'] == fid, \
            f"figure id changed: was {fid!r}, now {figs_after[0]['id']!r}"
    finally:
        _cleanup(page, pid)


def test_save_as_new_forks_a_new_figure(page):
    """Clicking 'save as new...' must POST a new figure (different id,
    same project)."""
    seed = _seed_project_and_figure(page, 'G8-fork')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)

        # Type a new name then click save-as-new
        page.evaluate("""() => {
            const fn = document.getElementById('fig-name');
            fn.value = 'forked-copy';
            fn.dispatchEvent(new Event('input'));
            document.getElementById('btn-fig-save-as').click();
        }""")
        page.wait_for_timeout(1000)

        figs = page.evaluate(f"""async () => {{
            const r = await fetch(API_BASE + '/api/projects/{pid}/figures');
            return (await r.json()).figures || [];
        }}""")
        names = sorted(f['name'] for f in figs)
        assert names == ['G8-fork-fig', 'forked-copy'], \
            f"unexpected figures after save-as-new: {names}"
    finally:
        _cleanup(page, pid)
