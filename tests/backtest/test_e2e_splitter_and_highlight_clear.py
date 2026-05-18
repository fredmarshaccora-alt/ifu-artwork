"""Two contracts:
  1. The split-view splitter element is present, draggable, and the
     CSS variables that drive the 2D/3D column widths update when it
     moves.  Double-click resets to 50/50.
  2. clicking "generate 2D" (the user-driven new-angle action) wipes
     the selection state, so highlights don't persist across camera
     angles of the same source.
"""
from __future__ import annotations


def _enter_editor_split(page):
    """Seed a project + view + figure, navigate into the editor so
    the legacy <main> is visible, then click the lay-split button."""
    seed = page.evaluate("""async () => {
        const pr = await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'splitter-test',
                                    primary_source_id: 'siderail'}),
        });
        const proj = await pr.json();
        const vr = await fetch(API_BASE + '/api/views', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project_id: proj.id,
                                    source_id: 'siderail',
                                    name: 'V',
                                    camera: {eye:[1000,1000,800],target:[0,0,0]}}),
        });
        const view = await vr.json();
        const fr = await fetch(API_BASE + '/api/figures', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'F',
                                    source_id: 'siderail',
                                    project_id: proj.id,
                                    view_id: view.id,
                                    camera: view.camera}),
        });
        const fig = await fr.json();
        await fetch(API_BASE + '/api/views/' + view.id
                      + '/figures/' + fig.id, {method: 'POST'});
        return {pid: proj.id, vid: view.id, fid: fig.id};
    }""")
    page.evaluate(
        f"location.hash = "
        f"'#/project/{seed['pid']}/view/{seed['vid']}/figure/{seed['fid']}'")
    page.wait_for_timeout(1500)   # let route mount + load figure
    page.evaluate("""() => {
        const btn = document.getElementById('lay-split');
        if (btn) btn.click();
    }""")
    page.wait_for_timeout(300)
    return seed


def _cleanup(page, pid):
    page.evaluate(f"""async () => {{
        await fetch(API_BASE + '/api/projects/{pid}?cascade=1',
                      {{method: 'DELETE'}});
    }}""")


def test_splitter_visible_only_in_split_layout(page):
    seed = _enter_editor_split(page)
    try:
        visible = page.evaluate("""() => {
            const el = document.getElementById('pane-splitter');
            return el && getComputedStyle(el).display !== 'none';
        }""")
        assert visible, "splitter should be visible in split layout"

        # Switch to 2D layout -- splitter must disappear
        page.evaluate("document.getElementById('lay-2d').click()")
        page.wait_for_timeout(200)
        hidden = page.evaluate("""() => {
            const el = document.getElementById('pane-splitter');
            return el && getComputedStyle(el).display === 'none';
        }""")
        assert hidden, "splitter should hide outside split layout"
    finally:
        _cleanup(page, seed['pid'])


def test_splitter_drag_resizes_panes(page):
    seed = _enter_editor_split(page)
    try:
        start = page.evaluate("""() => ({
            a: document.body.style.getPropertyValue('--split-2d'),
            b: document.body.style.getPropertyValue('--split-3d'),
        })""")
        page.evaluate("""() => {
            const el = document.getElementById('pane-splitter');
            const r = el.getBoundingClientRect();
            const cx = r.x + r.width / 2;
            const cy = r.y + r.height / 2;
            const mk = (type, x) => new PointerEvent(type, {
                bubbles: true, cancelable: true,
                pointerId: 1, pointerType: 'mouse',
                clientX: x, clientY: cy,
            });
            el.dispatchEvent(mk('pointerdown', cx));
            // The pointermove + pointerup listeners are on `document`,
            // not the splitter itself, so dispatch there too.
            document.dispatchEvent(mk('pointermove', cx - 120));
            document.dispatchEvent(mk('pointerup', cx - 120));
        }""")
        page.wait_for_timeout(150)
        end = page.evaluate("""() => ({
            a: document.body.style.getPropertyValue('--split-2d'),
            b: document.body.style.getPropertyValue('--split-3d'),
        })""")
        assert end['a'] != start['a'] or end['b'] != start['b'], \
            f"splitter drag didn't move the columns: start={start} end={end}"
    finally:
        _cleanup(page, seed['pid'])


def test_splitter_double_click_resets(page):
    seed = _enter_editor_split(page)
    try:
        # Move first via synthetic drag
        page.evaluate("""() => {
            const el = document.getElementById('pane-splitter');
            const r = el.getBoundingClientRect();
            const cx = r.x + r.width / 2;
            const cy = r.y + r.height / 2;
            const mk = (type, x) => new PointerEvent(type, {
                bubbles: true, cancelable: true,
                pointerId: 1, pointerType: 'mouse',
                clientX: x, clientY: cy,
            });
            el.dispatchEvent(mk('pointerdown', cx));
            document.dispatchEvent(mk('pointermove', cx + 80));
            document.dispatchEvent(mk('pointerup', cx + 80));
        }""")
        page.wait_for_timeout(100)
        # Now double-click to reset
        page.evaluate("""() => {
            const el = document.getElementById('pane-splitter');
            el.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
        }""")
        page.wait_for_timeout(100)
        reset = page.evaluate(
            "document.body.style.getPropertyValue('--split-2d')")
        assert '0.5' in reset, \
            f"after double-click reset expected '0.5...fr', got {reset!r}"
    finally:
        _cleanup(page, seed['pid'])


def test_generate_2d_clears_highlights(page):
    """The user-driven "generate 2D" action wipes the highlight set
    so selections don't bleed across camera angles of the same
    source.  Loading a saved figure (a different code path)
    intentionally preserves highlights."""
    seed = _enter_editor_split(page)
    try:
        # Wait until three.js has booted -- generateLiveSVG bails out
        # of `if (!camera || !controls) return` if it hasn't.
        for _ in range(60):
            page.wait_for_timeout(250)
            ready = page.evaluate("""() => {
                const v = window.IFU_VIEWER || {};
                return typeof v.getCameraEyeTarget === 'function'
                         && !!v.getCameraEyeTarget();
            }""")
            if ready: break
        assert ready, "three.js camera never initialised"

        # Plant a selection on the CURRENT (file, view).  Auto-render
        # has already pushed viewSel to '__live__' by now.
        info = page.evaluate("""() => {
            const fs = document.getElementById('file-sel');
            const vs = document.getElementById('view-sel');
            const st = getState(fs.value, vs.value);
            st.highlights = new Set([0, 1, 2]);
            return {fid: fs.value, vid: vs.value, size: st.highlights.size};
        }""")
        assert info['size'] == 3, f"setup: expected 3 selected, got {info}"

        # The auto-render started by _enter_editor_split disables
        # btn-generate while in flight.  Wait for it to re-enable.
        for _ in range(60):
            page.wait_for_timeout(300)
            enabled = page.evaluate(
                "() => { const b = document.getElementById('btn-generate'); "
                "return b && !b.disabled; }")
            if enabled: break
        assert enabled, "btn-generate stayed disabled -- auto-render hung"

        page.evaluate("document.getElementById('btn-generate').click()")
        page.wait_for_timeout(300)
        sz2 = page.evaluate(f"""() => {{
            const st = getState('{info['fid']}', '{info['vid']}');
            return st.highlights ? st.highlights.size : 0;
        }}""")
        assert sz2 == 0, \
            f"highlights should clear when user fires generate-2D; size now {sz2}"
    finally:
        _cleanup(page, seed['pid'])
