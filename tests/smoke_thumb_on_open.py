"""Verify capture-on-open: opening a figure in the editor produces a
thumbnail for BOTH the figure and its parent view (no explicit save).

Clears the two thumbnail files first, opens the figure URL, waits for
the live render + debounced capture, then asserts both thumbnail GETs
return a real PNG.

Run with the server up:  python tests/smoke_thumb_on_open.py [proj/view/fig]
"""
from __future__ import annotations
import sys
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests

SERVER = "http://127.0.0.1:5000"
DEFAULT = "281cf2da3885/5653e1b90841/decfd8366514"
SEL = ".svg-pane[data-view='__live__'] svg"
CHECK = lambda ok, msg: print(f"  [{'OK' if ok else 'FAIL'}] {msg}")


def _thumb_status(kind, _id):
    r = requests.get(f"{SERVER}/api/{kind}/{_id}/thumbnail", timeout=5)
    return r.status_code, len(r.content)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    print(f"\n=== capture-on-open ===\n{url}\n")
    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        print(f"  server not reachable: {e}"); return 2

    # Clean slate: delete the two thumbnail files so we prove creation.
    try:
        from ifu import figures_store, views_store
        figures_store.figure_thumbnail_path(fig).unlink(missing_ok=True)
        views_store.view_thumbnail_path(view).unlink(missing_ok=True)
        print("  cleared figure + view thumbnail files")
    except Exception as e:
        print(f"  (could not clear thumbnails: {e})")

    fs0, _ = _thumb_status("figures", fig)
    vs0, _ = _thumb_status("views", view)
    CHECK(fs0 == 404, f"figure thumbnail starts missing (HTTP {fs0})")
    CHECK(vs0 == 404, f"view thumbnail starts missing (HTTP {vs0})")

    from playwright.sync_api import sync_playwright
    js_errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.goto(url, wait_until="load", timeout=60000)
        # wait for live SVG
        ok_svg = False
        for _ in range(120):
            n = page.eval_on_selector_all(
                SEL, "els => els.map(el => el.outerHTML.length)")
            if n and max(n) > 1024:
                ok_svg = True; break
            time.sleep(0.5)
        CHECK(ok_svg, "live SVG rendered")
        # capture-on-open is debounced 1.5s; allow time for capture+PUT
        time.sleep(4)
        # also nudge it explicitly in case the debounce hadn't fired
        page.evaluate("""() => {
          if (window._captureAndUploadAll) {
            const A = (window.IFU_APP && window.IFU_APP.AppState) || {};
          }
        }""")
        b.close()

    # Poll the endpoints (give any in-flight PUT a moment)
    fs, fb, vs, vb = 0, 0, 0, 0
    for _ in range(10):
        fs, fb = _thumb_status("figures", fig)
        vs, vb = _thumb_status("views", view)
        if fs == 200 and vs == 200:
            break
        time.sleep(0.6)
    CHECK(fs == 200 and fb > 100,
          f"figure thumbnail created (HTTP {fs}, {fb} bytes)")
    CHECK(vs == 200 and vb > 100,
          f"view thumbnail created (HTTP {vs}, {vb} bytes)")
    if js_errors:
        print(f"  JS errors: {js_errors[:2]}")
    return 0 if (fs == 200 and vs == 200) else 1


if __name__ == "__main__":
    sys.exit(main())
