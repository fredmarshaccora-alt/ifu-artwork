"""Verify generate-2D forks a NEW variant instead of overwriting.

Opens a figure in a view, calls the fork path (what the Generate button
now does), and asserts:
  - the view gains exactly one figure
  - the ORIGINAL figure still exists (not overwritten/deleted)
  - the URL navigated to a new figure id
  - the new variant inherited the original's selection/styles

Teardown deletes the forked variant.

Run with the server up:  python tests/smoke_generate_variant.py [proj/view/fig]
"""
from __future__ import annotations
import sys
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
import time
import requests

SERVER = "http://127.0.0.1:5000"
DEFAULT = "281cf2da3885/5653e1b90841/decfd8366514"
SEL = ".svg-pane[data-view='__live__'] svg"
CHECK = lambda ok, msg: print(f"  [{'OK' if ok else 'FAIL'}] {msg}")


def _view_fig_ids(view):
    try:
        d = requests.get(f"{SERVER}/api/views/{view}/figures", timeout=5).json()
        return [f["id"] for f in (d.get("figures") or [])]
    except Exception:
        return []


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    print(f"\n=== generate forks a new variant ===\n{url}\n")
    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        print(f"  server not reachable: {e}"); return 2

    before = _view_fig_ids(view)
    CHECK(fig in before, f"original figure is in the view ({len(before)} figs)")

    from playwright.sync_api import sync_playwright
    js_errors = []
    new_fid = None
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.goto(url, wait_until="load", timeout=60000)
        # wait for editor + AppState
        ok = False
        for _ in range(60):
            ok = page.evaluate("""() => {
              const A = (window.IFU_APP && window.IFU_APP.AppState) || {};
              return !!(A.currentViewId && A.currentProjectId
                        && A.currentFigureId
                        && typeof window._forkNewAngleVariant === 'function');
            }""")
            if ok: break
            time.sleep(0.5)
        CHECK(ok, "editor ready with view context + fork fn")
        if not ok: b.close(); return 1

        # Wait for the figure to FULLY load (live SVG rendered + selection
        # restored) before forking -- otherwise we fork an empty state.
        for _ in range(120):
            n = page.eval_on_selector_all(
                SEL, "els => els.map(el => el.outerHTML.length)")
            if n and max(n) > 1024:
                break
            time.sleep(0.5)
        time.sleep(2)

        old_hash = page.evaluate("() => location.hash")
        # Fire the fork (what Generate now does after orbiting)
        forked = page.evaluate(
            "() => window._forkNewAngleVariant()")
        CHECK(forked is True, "fork returned true (created a variant)")

        # wait for navigation to a new figure id
        for _ in range(40):
            h = page.evaluate("() => location.hash")
            if h != old_hash and "/figure/" in h:
                nf = h.split("/figure/")[-1].split("/")[0]
                if nf and nf != fig:
                    new_fid = nf; break
            time.sleep(0.5)
        CHECK(new_fid and new_fid != fig,
              f"navigated to new variant (fid={new_fid})")
        time.sleep(1)
        b.close()

    after = _view_fig_ids(view)
    CHECK(len(after) == len(before) + 1,
          f"view gained exactly one figure ({len(before)} -> {len(after)})")
    CHECK(fig in after, "ORIGINAL figure still exists (not overwritten)")
    if new_fid:
        rec = requests.get(f"{SERVER}/api/figures/{new_fid}", timeout=5).json()
        orig = requests.get(f"{SERVER}/api/figures/{fig}", timeout=5).json()
        CHECK((rec.get("selection") or []) == (orig.get("selection") or []),
              "new variant inherited the original's selection")
    if js_errors:
        print(f"  JS errors: {js_errors[:2]}")

    # teardown
    if new_fid:
        try:
            requests.delete(f"{SERVER}/api/figures/{new_fid}", timeout=5)
            print(f"  cleanup: deleted variant {new_fid}")
        except Exception as e:
            print(f"  cleanup failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
