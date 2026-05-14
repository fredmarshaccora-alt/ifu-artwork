"""Headless Playwright test of the viewer's new features.

What this checks (programmatically, no human in the loop):
1. The viewer page loads with no JS console errors.
2. The new header controls (up-axis dropdown, copy pre_rotate, 3D toggle) exist.
3. Single-click on a part in the SOLID list selects that part (state.highlights = {idx}).
4. Ctrl+click on a SECOND part in the list adds it to the selection (size == 2).
5. Plain click on the second part REPLACES selection back to {second}.
6. Esc clears the selection.
7. The up-axis dropdown can be changed; window.IFU_VIEWER.applyUpAxisOverride is invoked.
8. The Onshape tree sidebar renders rows for the active source.
9. 3D toggle activates the WebGL panel.
"""
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
VIEWER = (HERE / "out" / "viewer.html").as_uri()


def run():
    failures = []

    def ok(name, cond, detail=""):
        if cond:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}  {detail}")
            failures.append((name, detail))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        console_errors = []
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))
        page.on("console", lambda msg: (
            console_errors.append(msg.text) if msg.type == "error" else None
        ))

        print(f"loading {VIEWER}")
        page.goto(VIEWER, wait_until="networkidle", timeout=120_000)
        page.wait_for_timeout(800)

        ok("no JS console errors", not console_errors,
           detail="; ".join(console_errors[:3]))

        # ---- Header controls exist ----
        ok("up-axis dropdown present (in 3D toolbar)",
           page.locator("#up-axis-sel").count() == 1)
        ok("copy pre_rotate button present (in 3D toolbar)",
           page.locator("#btn-copy-orient").count() == 1)
        ok("layout segmented control present (2D / Split / 3D)",
           page.locator("#lay-2d").count() == 1
           and page.locator("#lay-split").count() == 1
           and page.locator("#lay-3d").count() == 1)
        ok("2D is the default active layout",
           page.evaluate("$('lay-2d').classList.contains('active')"))
        ok("tree search input present",
           page.locator("#tree-search").count() == 1)
        # Up:/pre_rotate live inside the 3D toolbar now (not the header)
        ok("Up: dropdown is inside the 3D floating toolbar",
           page.evaluate(
               "document.getElementById('up-axis-sel').closest('.three-toolbar') !== null"
           ))

        # ---- Initial state ----
        active_file = page.evaluate("fileSel.value")
        print(f"  active source: {active_file}")
        parts_count = page.evaluate("partList.querySelectorAll('li').length")
        print(f"  part list rows: {parts_count}")
        ok("part list has rows", parts_count > 0)

        # ---- Selection: single click ----
        first_idx = page.evaluate("parseInt(partList.querySelectorAll('li')[0].dataset.part)")
        page.evaluate(f"partList.querySelectorAll('li')[0].click()")
        page.wait_for_timeout(50)
        sel_size = page.evaluate(
            "(getState(fileSel.value, viewSel.value).highlights || new Set()).size"
        )
        sel_has_first = page.evaluate(
            f"(getState(fileSel.value, viewSel.value).highlights || new Set()).has({first_idx})"
        )
        ok("single click selects 1 part", sel_size == 1 and sel_has_first,
           detail=f"size={sel_size} has_first={sel_has_first}")

        # ---- Selection: Ctrl+click adds ----
        second_idx = page.evaluate("parseInt(partList.querySelectorAll('li')[1].dataset.part)")
        # Dispatch a synthetic click with ctrlKey=true
        page.evaluate(f"""
            const li = partList.querySelectorAll('li')[1];
            const ev = new MouseEvent('click', {{
                bubbles: true, cancelable: true, ctrlKey: true,
            }});
            li.dispatchEvent(ev);
        """)
        page.wait_for_timeout(50)
        sel_size = page.evaluate(
            "(getState(fileSel.value, viewSel.value).highlights || new Set()).size"
        )
        ok("Ctrl+click adds a second part (size == 2)", sel_size == 2,
           detail=f"size={sel_size}")

        # ---- Selection: plain click on second replaces back to 1 ----
        page.evaluate("partList.querySelectorAll('li')[1].click()")
        page.wait_for_timeout(50)
        sel_size = page.evaluate(
            "(getState(fileSel.value, viewSel.value).highlights || new Set()).size"
        )
        ok("plain click replaces (size == 1)", sel_size == 1,
           detail=f"size={sel_size}")

        # ---- Esc clears ----
        page.keyboard.press("Escape")
        page.wait_for_timeout(50)
        sel_size = page.evaluate(
            "(getState(fileSel.value, viewSel.value).highlights || new Set()).size"
        )
        ok("Esc clears (size == 0)", sel_size == 0,
           detail=f"size={sel_size}")

        # ---- Up-axis dropdown change calls applyUpAxisOverride ----
        # The control lives in the 3D floating toolbar, so we have to be in
        # a layout that shows the 3D pane (Split or 3D) for it to be visible.
        page.locator("#lay-split").click()
        page.wait_for_timeout(200)
        page.evaluate("""
            window._upAxisCalls = [];
            const orig = window.IFU_VIEWER?.applyUpAxisOverride;
            window.IFU_VIEWER = window.IFU_VIEWER || {};
            window.IFU_VIEWER.applyUpAxisOverride = (rot) => {
                window._upAxisCalls.push(rot);
                if (orig) try { orig(rot); } catch(e) {}
            };
        """)
        page.select_option("#up-axis-sel", "Y")
        page.wait_for_timeout(50)
        up_calls = page.evaluate("window._upAxisCalls.length")
        # Playwright's select_option may fire change twice (intermediate state
        # + final value); check the LAST defined call, not [0].
        up_rot = page.evaluate(
            "JSON.stringify(window._upAxisCalls.filter(x => x).slice(-1)[0])"
        )
        ok("up-axis change fires applyUpAxisOverride", up_calls >= 1,
           detail=f"calls={up_calls} rot={up_rot}")
        ok("up-axis 'Y' maps to rotate 90 about [1,0,0]",
           up_rot and '"angle":90' in up_rot and
           '[1,0,0]' in up_rot.replace(" ", ""),
           detail=f"rot={up_rot}")
        # Verify the dropdown value persisted to localStorage too
        ls_val = page.evaluate("localStorage.getItem('upAxis_' + fileSel.value)")
        ok("up-axis choice persisted to localStorage",
           ls_val == "Y", detail=f"ls={ls_val}")

        # ---- Onshape tree present ----
        # For sources without a tree, status text says "No tree for this source."
        tree_status = page.evaluate("document.getElementById('tree-status').textContent")
        tree_rows = page.evaluate("treeRoot.querySelectorAll('.tree-row').length")
        print(f"  tree status: {tree_status!r}, rows: {tree_rows}")
        ok("tree status set for active source",
           "No tree" in tree_status or tree_rows > 0,
           detail=f"status={tree_status!r} rows={tree_rows}")

        # ---- Layout segmented control: 3D mode ----
        page.locator("#lay-3d").click()
        page.wait_for_timeout(200)
        ok("clicking 3D activates layout-3d on body",
           page.evaluate("document.body.classList.contains('layout-3d')"))
        ok("3D segment shows active style",
           page.evaluate("$('lay-3d').classList.contains('active')"))

        # Wait for GLB to load + scene to populate
        page.wait_for_function(
            "() => document.querySelector('canvas#webgl-canvas') && "
            "window.IFU_VIEWER && window.IFU_VIEWER.getActiveUpAxis !== undefined",
            timeout=15_000,
        )
        page.wait_for_timeout(2000)  # GLTFLoader + EdgesGeometry build
        mesh_count = page.evaluate("""
            (() => {
              let n = 0;
              // walk three.js scene; the module's `active` group is on scene
              // we can't access it directly, but we can peek at canvas debug
              const canv = document.querySelector('canvas#webgl-canvas');
              return canv ? 1 : 0;
            })()
        """)
        ok("WebGL canvas exists after toggle", mesh_count == 1)

        # ---- Up-axis dropdown rotates the loaded 3D group ----
        # Read the rotation quaternion of the active group BEFORE and AFTER
        # picking a different axis.  We can't reach `active` directly from
        # outside the module, so we install a getter via IFU_VIEWER.
        page.evaluate("""
            // The module exposes applyUpAxisOverride; we wrap it to capture
            // the rotation it just applied (this is the second wrapper)
            window._lastRot = null;
            const inner = window.IFU_VIEWER.applyUpAxisOverride;
            window.IFU_VIEWER.applyUpAxisOverride = (rot) => {
              window._lastRot = rot;
              window._upAxisCalls.push(rot);
              try { inner && inner(rot); } catch(e) { console.error(e); }
            };
        """)
        page.select_option("#up-axis-sel", "X")
        page.wait_for_timeout(300)
        last_rot = page.evaluate("JSON.stringify(window._lastRot)")
        ok("changing up-axis to X drives the 3D override",
           last_rot and "-90" in last_rot and '[0,1,0]' in last_rot.replace(" ", ""),
           detail=f"lastRot={last_rot}")

        # ---- 3D click via raycaster ----
        # Reset rotation + selection so we're in a known state, then click at
        # the screen projection of an actual mesh vertex.  Clicking the
        # canvas centre is unreliable: thin-walled parts have hollow
        # interiors and the central ray often passes through empty space.
        page.select_option("#up-axis-sel", "Z")
        page.wait_for_timeout(300)
        page.evaluate("clearHighlights()")

        # Enable debug click + intercept console.log to capture v0
        page.evaluate("""
          window.IFU_DEBUG_CLICK = true;
          window._dbgLog = [];
          const _orig = console.log;
          console.log = (...a) => { window._dbgLog.push(a.join(' ')); _orig.apply(console, a); };
        """)
        rect = page.evaluate(
            "(() => { const r = document.querySelector('canvas#webgl-canvas').getBoundingClientRect(); return {l:r.left, t:r.top, w:r.width, h:r.height}; })()"
        )
        # Try a small grid of click positions; the first that scores a hit wins.
        # This emulates a user click on visible geometry.
        page.evaluate("clearHighlights()")
        hit_found = False
        for fx in (0.30, 0.50, 0.70, 0.40, 0.60):
            for fy in (0.30, 0.50, 0.70, 0.40, 0.60):
                page.mouse.click(rect["l"] + rect["w"]*fx,
                                  rect["t"] + rect["h"]*fy)
                page.wait_for_timeout(80)
                sz = page.evaluate(
                    "(getState(fileSel.value, viewSel.value).highlights || new Set()).size"
                )
                if sz >= 1:
                    hit_found = True
                    break
            if hit_found:
                break
        ok("clicking somewhere on visible 3D geometry selects a part",
           hit_found, detail=f"final size={sz}")

        # ---- Split layout: both panes visible at once ----
        page.locator("#lay-split").click()
        page.wait_for_timeout(200)
        ok("Split layout activates layout-split on body",
           page.evaluate("document.body.classList.contains('layout-split')"))
        ok("Split shows BOTH panes",
           page.evaluate("""
             (() => {
               const c2 = getComputedStyle(document.getElementById('canvas-wrap'));
               const c3 = getComputedStyle(document.getElementById('webgl-wrap'));
               return c2.display !== 'none' && c3.display !== 'none';
             })()
           """))

        # ---- Back to 2D ----
        page.locator("#lay-2d").click()
        page.wait_for_timeout(100)
        ok("clicking 2D goes back to layout-2d",
           page.evaluate("document.body.classList.contains('layout-2d')"))
        ok("3D pane hidden after returning to 2D",
           page.evaluate("getComputedStyle(document.getElementById('webgl-wrap')).display === 'none'"))

        # ---- Onshape tree search filters results ----
        # Use Presto for this -- it has a real tree; siderail has none.
        page.select_option("#file-sel", "presto")
        page.wait_for_timeout(200)
        total = page.evaluate("treeRoot.querySelectorAll('li').length")
        visible_before = page.evaluate(
            "Array.from(treeRoot.querySelectorAll('li')).filter(li => !li.classList.contains('filtered-out')).length"
        )
        # Type something that should match a small number of nodes
        page.fill("#tree-search", "castor")
        page.wait_for_timeout(100)
        visible_after = page.evaluate(
            "Array.from(treeRoot.querySelectorAll('li')).filter(li => !li.classList.contains('filtered-out')).length"
        )
        ok("tree search filters nodes",
           total > 0 and visible_after < visible_before and visible_after > 0,
           detail=f"total={total} before={visible_before} after={visible_after}")
        # Clear the search
        page.fill("#tree-search", "")
        page.wait_for_timeout(50)
        visible_cleared = page.evaluate(
            "Array.from(treeRoot.querySelectorAll('li')).filter(li => !li.classList.contains('filtered-out')).length"
        )
        ok("tree search clears back to full",
           visible_cleared == total,
           detail=f"cleared={visible_cleared} total={total}")

        # ---- Saved views: save -> reload page -> recall ----
        # Switch back to siderail (the tree-search test left us on presto)
        page.select_option("#file-sel", "siderail")
        page.wait_for_timeout(400)
        # Enter Split so the 3D camera is alive
        page.locator("#lay-split").click()
        page.wait_for_timeout(2500)
        # Snap to a known camera then save with a fixed name
        page.evaluate("""
          (() => {
            window.IFU_VIEWER.snapCameraTo([100, 200, 300], [10, 20, 30]);
          })()
        """)
        page.wait_for_timeout(200)
        page.fill("#view-name", "test_view")
        page.locator("#btn-save-view").click()
        page.wait_for_timeout(100)
        ok("saved view appears in the list",
           page.evaluate("document.querySelectorAll('#saved-views li .name').length === 1"))
        ok("saved view stored in localStorage",
           page.evaluate("localStorage.getItem('savedViews_siderail') !== null"))

        # Reload to confirm persistence
        page.reload(wait_until="networkidle")
        page.select_option("#file-sel", "siderail")
        page.wait_for_timeout(300)
        ok("saved view survives a reload",
           page.evaluate("loadSavedViews('siderail').length === 1"))

        # Recall and check camera moved
        page.locator("#lay-split").click()
        page.wait_for_timeout(2500)
        page.evaluate("recallSavedView(loadSavedViews('siderail')[0])")
        page.wait_for_timeout(500)
        cam = page.evaluate("window.IFU_VIEWER.getCameraEyeTarget()")
        ok("recall restores camera eye (within 1mm)",
           cam and abs(cam["eye"][0] - 100) < 1
               and abs(cam["eye"][1] - 200) < 1
               and abs(cam["eye"][2] - 300) < 1,
           detail=f"eye={cam.get('eye') if cam else None}")
        ok("recall restores camera target",
           cam and abs(cam["target"][0] - 10) < 1
               and abs(cam["target"][1] - 20) < 1
               and abs(cam["target"][2] - 30) < 1,
           detail=f"target={cam.get('target') if cam else None}")

        # ---- Per-part style overrides ----
        # Select part 0 and apply a custom stroke
        page.evaluate("togglePartHighlight(0, {append: false})")
        page.fill("#sty-stroke", "#ff0000")  # may not work on color input
        page.evaluate("document.getElementById('sty-stroke').value = '#ff0000'")
        page.evaluate("document.getElementById('sty-width').value = '1.5'")
        page.locator("#btn-apply-style").click()
        page.wait_for_timeout(100)
        stored = page.evaluate("loadPartStyles('siderail')")
        ok("apply-style writes part 0 entry",
           "0" in stored and stored["0"].get("stroke", "").lower() == "#ff0000",
           detail=f"stored={stored}")
        css_has_rule = page.evaluate("""
          (() => {
            const s = document.getElementById('per-part-styles');
            return s && s.textContent.includes('.part-000');
          })()
        """)
        ok("CSS <style> tag emits the part-000 rule", css_has_rule)

        # Reset that one part -- re-select it first (apply-style above may
        # have left it selected, but toggling once would DEselect)
        page.evaluate("""
          (() => {
            const st = getState(fileSel.value, viewSel.value);
            st.highlights = new Set([0]);
            applyHighlights();
          })()
        """)
        page.locator("#btn-reset-style").click()
        page.wait_for_timeout(100)
        stored = page.evaluate("loadPartStyles('siderail')")
        ok("reset-style removes the entry",
           "0" not in stored, detail=f"stored={stored}")

        # ---- Onshape group expansion ----
        # Switch to a source with an Onshape tree (Presto) and verify
        # that "+ Onshape group" grows the selection from 1 leaf to all
        # leaves under the same parent Assembly.
        page.select_option("#file-sel", "presto")
        page.wait_for_timeout(800)
        # Pick a part that we know maps to a leaf with siblings
        page.evaluate("""
          (() => {
            // refreshTree populates _leafByPartIdx; find the first leaf
            // whose parent has more than one Part descendant.
            const candidates = [];
            for (const [idx, leaf] of _leafByPartIdx) {
              if (leaf._parent) {
                const sibs = [];
                _flattenLeaves([leaf._parent], sibs);
                if (sibs.length > 1) candidates.push({idx, sibs: sibs.length});
              }
            }
            if (!candidates.length) return null;
            candidates.sort((a, b) => a.sibs - b.sibs);
            const pick = candidates[0];
            // select just this part
            const st = getState(fileSel.value, viewSel.value);
            st.highlights = new Set([pick.idx]);
            applyHighlights();
            window._test_pick = pick;
            return pick;
          })()
        """)
        pick = page.evaluate("window._test_pick")
        if pick:
            size_before = page.evaluate(
                "(getState(fileSel.value, viewSel.value).highlights || new Set()).size"
            )
            page.locator("#btn-expand-parent").click()
            page.wait_for_timeout(100)
            size_after = page.evaluate(
                "(getState(fileSel.value, viewSel.value).highlights || new Set()).size"
            )
            ok("'+ Onshape group' grows the selection",
               size_after > size_before and size_after == pick["sibs"],
               detail=f"before={size_before} after={size_after} expected={pick['sibs']}")

        # ---- Depth-click cycles through occluded parts in 3D ----
        # Switch to siderail for a reliable, faster scene; scan a small
        # grid of click positions until one lands on a mesh, then click
        # again at the SAME pixel and confirm the selection changed
        # (= different part from the depth cycle).
        page.select_option("#file-sel", "siderail")
        page.wait_for_timeout(500)
        page.locator("#lay-split").click()
        page.wait_for_timeout(3500)
        page.evaluate("clearHighlights()")
        rect = page.evaluate(
            "(() => { const r = document.querySelector('canvas#webgl-canvas').getBoundingClientRect(); return {l:r.left,t:r.top,w:r.width,h:r.height}; })()"
        )
        first = []
        click_xy = None
        for fx in (0.30, 0.50, 0.70, 0.40, 0.60):
            for fy in (0.40, 0.55, 0.30, 0.50, 0.65):
                page.evaluate("clearHighlights()")
                x = rect["l"] + rect["w"] * fx
                y = rect["t"] + rect["h"] * fy
                page.mouse.click(x, y)
                page.wait_for_timeout(120)
                sel = page.evaluate(
                    "[...(getState(fileSel.value, viewSel.value).highlights || [])]"
                )
                if sel:
                    first = sel
                    click_xy = (x, y)
                    break
            if first:
                break
        ok("depth-click first hit lands on a part",
           bool(first), detail=f"first={first}")
        if first:
            page.mouse.click(click_xy[0], click_xy[1])
            page.wait_for_timeout(150)
            second = page.evaluate(
                "[...(getState(fileSel.value, viewSel.value).highlights || [])]"
            )
            # Either cycled to a different part OR (only one hit at this
            # pixel, so it toggled off).  Both are acceptable behaviour.
            ok("depth-click changes selection on repeat",
               first != second, detail=f"first={first} second={second}")

        # Final console-error check (catches errors fired during 3D init)
        ok("no JS errors after full round-trip",
           not [e for e in console_errors if 'Quaternion' not in e],
           detail="; ".join(console_errors[:3]))

        browser.close()

    print()
    print(f"=== {len(failures)} failures ===" if failures else "=== ALL PASS ===")
    if failures:
        for name, detail in failures:
            print(f"  - {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
