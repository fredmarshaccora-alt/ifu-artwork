"""Verify exported SVGs carry vector-effect:non-scaling-stroke so line
weights render at a constant thickness regardless of how each view is
placed in an IFU (the 'same thickness across views' requirement).

Captures the blob the export produces (by wrapping URL.createObjectURL)
and asserts the non-scaling-stroke rule + attribute are present.

Run with the server up:  python tests/smoke_line_weight.py [proj/view/fig]
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


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    print(f"\n=== export line-weight (non-scaling-stroke) ===\n{url}\n")
    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        print(f"  server not reachable: {e}"); return 2

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.goto(url, wait_until="load", timeout=60000)
        ok = False
        for _ in range(120):
            n = page.eval_on_selector_all(
                SEL, "els => els.map(el => el.outerHTML.length)")
            if n and max(n) > 1024: ok = True; break
            time.sleep(0.5)
        CHECK(ok, "live SVG rendered")
        if not ok: b.close(); return 1
        time.sleep(1.5)

        # Wrap createObjectURL to capture blob texts.  NOTE: capture-on-
        # open ALSO makes SVG blobs via createObjectURL, so collect all
        # and pick the EXPORT one (it carries the <?xml prolog the
        # thumbnail capture lacks).
        page.evaluate(r"""() => {
          window.__blobs = [];
          const orig = URL.createObjectURL.bind(URL);
          URL.createObjectURL = (blob) => {
            try { blob.text().then(t => { window.__blobs.push(t); }); } catch(e){}
            return orig(blob);
          };
        }""")
        # Trigger export
        page.evaluate("() => document.getElementById('btn-export').click()")
        cap = None
        for _ in range(25):
            blobs = page.evaluate("() => window.__blobs || []")
            # the export blob carries the <?xml prolog; thumbnail blobs don't
            cap = next((t for t in blobs if "<?xml" in t and "<svg" in t), None)
            if cap: break
            time.sleep(0.3)
        CHECK(bool(cap), "captured exported SVG")
        if not cap: b.close(); return 1

        has_rule = "non-scaling-stroke" in cap
        has_attr = 'vector-effect="non-scaling-stroke"' in cap
        n_attr = cap.count('vector-effect="non-scaling-stroke"')
        CHECK(has_rule, "exported SVG contains a non-scaling-stroke CSS rule")
        CHECK(has_attr and n_attr > 0,
              f"stroked paths carry the attribute ({n_attr} paths)")
        # sanity: it's a real SVG with line art
        CHECK("<path" in cap and "<svg" in cap,
              "export is a valid SVG with paths")
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
