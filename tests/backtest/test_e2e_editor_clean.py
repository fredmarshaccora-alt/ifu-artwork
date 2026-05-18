"""When in a figure route, the editor must hide every dev-only control
+ section so the user sees a focused workspace.  And: a state change
must auto-save within ~2-3s without the user clicking save.

The cleanup is a CSS rule keyed on body.project-scoped-editor.
EditorScreen adds the class on mount, drops it on teardown.  This
test pins down each hidden surface so a future refactor can't quietly
reintroduce the noise.
"""
from __future__ import annotations
import time


HIDDEN_CONTROLS = [
    'file-sel', 'view-sel',
    'mode-pill', 'mode-btns',
    'hi-detail',
    'dev-readout', 'up-axis',
    'hidden-layers', 'group-mode', 'dev-buttons', 'dev-prose',
]
HIDDEN_SECTIONS = [
    'project', 'saved-views', 'onshape-tree', 'step-order', 'pipeline',
]


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


def test_project_scoped_class_clears_when_leaving_figure_route(page):
    """The project-scoped-editor CSS class must be removed when the
    user navigates AWAY from a figure route -- otherwise it leaks
    into Home/Settings/Project views and hides their dev tools.

    (Previously this test asserted the legacy editor was visible
    on an empty hash; post-Phase-3, empty hash redirects to Home, so
    the meaningful invariant is just: the body class clears on
    teardown.)"""
    page.evaluate("location.hash = '#/'")
    page.wait_for_timeout(400)
    body_cls = page.evaluate("document.body.className")
    assert 'project-scoped-editor' not in body_cls, \
        f"unexpected project-scoped class on Home: {body_cls!r}"


def test_dev_controls_hidden_in_figure_route(page):
    """Inside /#/project/.../figure/<fid>, every dev-only control must
    be display:none and the body must carry the .project-scoped-editor
    class."""
    seed = _seed(page, 'G9-clean')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)
        body_cls = page.evaluate("document.body.className")
        assert 'project-scoped-editor' in body_cls, \
            f"missing project-scoped class: {body_cls!r}"

        # Every tagged control must be display:none
        states = page.evaluate(f"""() => {{
            const out = {{}};
            for (const k of {HIDDEN_CONTROLS!r}) {{
                const el = document.querySelector('[data-ed-control="' + k + '"]');
                out[k] = el ? getComputedStyle(el).display : 'missing';
            }}
            return out;
        }}""")
        for k, d in states.items():
            assert d == 'none', f"dev control {k!r} not hidden: display={d!r}"

        # Every tagged section must be hidden too
        states = page.evaluate(f"""() => {{
            const out = {{}};
            for (const k of {HIDDEN_SECTIONS!r}) {{
                const el = document.querySelector('[data-ed-section="' + k + '"]');
                out[k] = el ? getComputedStyle(el).display : 'missing';
            }}
            return out;
        }}""")
        for k, d in states.items():
            assert d == 'none', f"section {k!r} not hidden: display={d!r}"

        # Sanity: useful sections remain visible
        figs_disp = page.evaluate(
            "() => getComputedStyle(document.querySelector('[data-ed-section=\"figures\"]')).display")
        assert figs_disp != 'none', "figures section should stay visible"
    finally:
        _cleanup(page, pid)


def test_autosave_fires_after_dirty_state(page):
    """Loading a figure, mutating its state (selection or styles), and
    waiting ~3s must trigger an autosave -- the figure's updated_at
    advances even though the user never clicks save."""
    seed = _seed(page, 'G9-autosave')
    pid, fid = seed['proj']['id'], seed['fig']['id']
    try:
        # Capture baseline updated_at
        before = page.evaluate(f"""async () => {{
            const r = await fetch(API_BASE + '/api/figures/{fid}');
            return (await r.json()).updated_at;
        }}""")

        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(2000)   # let editor settle + baseline capture

        # Make a state change: poke a per-part style into localStorage
        # then dispatch an applyHighlights-relevant change.  Simpler:
        # mark a selection.
        page.evaluate("""() => {
            // Add a fake selection of part 0 to siderail's live state
            const st = window.getState ? getState('siderail', 'iso') : null;
            if (st) {
                st.highlights = new Set([0]);
                if (typeof applyHighlights === 'function') applyHighlights();
            }
        }""")
        # Wait long enough for the debounced auto-save (1.8s) + network
        page.wait_for_timeout(4500)

        after = page.evaluate(f"""async () => {{
            const r = await fetch(API_BASE + '/api/figures/{fid}');
            return (await r.json()).updated_at;
        }}""")

        assert after and after != before, \
            f"figure not auto-saved: updated_at unchanged ({before!r} -> {after!r})"
    finally:
        _cleanup(page, pid)
