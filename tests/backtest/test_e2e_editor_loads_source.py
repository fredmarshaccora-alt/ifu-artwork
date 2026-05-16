"""G.6 e2e: opening a figure in the editor switches the legacy file
selector to that figure's source_id, regardless of what was selected
before and without prompting the user.

This is the bug the user hit after G.5 -- the editor was showing
the previously-selected assembly instead of the figure's bound model.
"""
from __future__ import annotations


def test_editor_switches_file_selector_to_figure_source(page):
    """Create a project bound to presto + a figure on it.  Switch
    legacy editor to siderail manually.  Navigate to the figure's
    route -- the legacy fileSel should snap to 'presto'."""
    # Seed
    seed = page.evaluate("""async () => {
        const pr = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'G6-editor-source-test',
                                    primary_source_id: 'presto'}),
        });
        const proj = await pr.json();
        const fr = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'G6-fig',
                                    source_id: 'presto',
                                    project_id: proj.id}),
        });
        return {proj, fig: await fr.json()};
    }""")
    pid = seed["proj"]["id"]
    fid = seed["fig"]["id"]
    try:
        # Make sure the legacy file selector is on something else first
        page.evaluate("""() => {
            const fs = document.getElementById('file-sel');
            if (fs) {
                fs.value = 'siderail';
                fs.dispatchEvent(new Event('change'));
            }
        }""")
        page.wait_for_timeout(400)
        before = page.evaluate(
            "() => document.getElementById('file-sel').value")
        assert before == "siderail", \
            f"setup precondition failed: {before!r}"

        # Navigate to the editor route
        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)

        after = page.evaluate(
            "() => document.getElementById('file-sel').value")
        assert after == "presto", \
            f"editor didn't switch to figure's source: got {after!r}"
    finally:
        page.evaluate(f"""async () => {{
            await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                          {{method: 'DELETE'}});
        }}""")


def test_editor_no_confirm_prompt_on_route_entry(page):
    """The route handler should pass skipConfirm:true, so we don't get
    blocked by the 'replace current work?' dialog even when localStorage
    has style cache from a prior session."""
    # Seed a figure
    seed = page.evaluate("""async () => {
        const pr = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'G6-noconfirm-test',
                                    primary_source_id: 'siderail'}),
        });
        const proj = await pr.json();
        const fr = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'G6-fig',
                                    source_id: 'siderail',
                                    project_id: proj.id}),
        });
        return {proj, fig: await fr.json()};
    }""")
    pid = seed["proj"]["id"]
    fid = seed["fig"]["id"]
    try:
        # Plant some "current work" in localStorage so hasWork would
        # be true under the old logic.  Key shape matches _styleKey().
        page.evaluate("""() => {
            localStorage.setItem('partStyles_siderail',
                JSON.stringify({1: {stroke: '#ff0000'}}));
        }""")
        # Spy on confirm() -- if it's called we fail
        confirm_calls = page.evaluate("""() => {
            window._confirmCalls = 0;
            const _orig = window.confirm;
            window.confirm = (...args) => {
                window._confirmCalls++;
                return _orig.apply(window, args);
            };
            return 'spy installed';
        }""")
        assert confirm_calls == "spy installed"

        page.evaluate(f"location.hash = '#/project/{pid}/figure/{fid}'")
        page.wait_for_timeout(1500)

        n = page.evaluate("() => window._confirmCalls || 0")
        assert n == 0, \
            f"confirm() was called {n} time(s) -- route entry should skip it"
    finally:
        page.evaluate(f"""async () => {{
            await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                          {{method: 'DELETE'}});
        }}""")
        page.evaluate(
            "() => localStorage.removeItem('partStyles_siderail')")
