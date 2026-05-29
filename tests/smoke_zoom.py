"""Verify 2D zoom is anchored to the cursor (no 'all over the place').

Positions the cursor over a specific part, records which part is under
that screen pixel, zooms in several notches at that pixel, and asserts
the SAME part is still under the pixel afterwards (zoom-to-cursor keeps
the point fixed).  The old bug mixed pixel and viewBox-unit coordinates,
so zooming flung the drawing away and a different part (or nothing)
ended up under the cursor.

Run with the server up:  python tests/smoke_zoom.py [proj/view/fig]
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


def _part_at(page, x, y):
    return page.evaluate(r"""
    (pt) => {
      const els = document.elementsFromPoint(pt.x, pt.y);
      for (const el of els) {
        const g = el.closest && el.closest('.part[data-part]');
        if (g) return g.dataset.part;
      }
      return null;
    }""", {"x": x, "y": y})


def _scale(page):
    return page.evaluate(r"""
    () => {
      const g = document.querySelector(
        ".svg-pane[data-view='__live__'] svg g.view-transform");
      if (!g) return null;
      const t = g.getAttribute('transform') || '';
      const m = t.match(/scale\(([-0-9.]+)/);
      return m ? parseFloat(m[1]) : null;
    }""")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    print(f"\n=== 2D zoom-to-cursor ===\n{url}\n")
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
        for _ in range(30):
            if page.evaluate("()=>{const s=document.querySelector(\""+SEL
                             +"\");return !!(s&&s.dataset.attached);}"): break
            time.sleep(0.3)
        time.sleep(1.5)

        # Pick a part with a solid centre to aim at.
        pick = page.evaluate(r"""
        () => {
          const sel = ".svg-pane[data-view='__live__'] svg";
          const paths = document.querySelectorAll(
            sel+" .layer-outline_v .part[data-part] path,"
            +sel+" .layer-sharp_v .part[data-part] path");
          for (const p of paths) {
            const r = p.getBoundingClientRect();
            if (r.width > 40 && r.height > 40)
              return {x: r.left + r.width/2, y: r.top + r.height/2};
          }
          return null;
        }""")
        CHECK(bool(pick), "found a target point in the drawing")
        if not pick: b.close(); return 1
        cx, cy = pick["x"], pick["y"]

        page.mouse.move(cx, cy)
        before = _part_at(page, cx, cy)
        s0 = _scale(page)
        # zoom IN several notches at the cursor
        for _ in range(5):
            page.mouse.wheel(0, -120)
            time.sleep(0.05)
        time.sleep(0.2)
        afterIn = _part_at(page, cx, cy)
        s1 = _scale(page)
        # zoom back OUT
        for _ in range(5):
            page.mouse.wheel(0, 120)
            time.sleep(0.05)
        time.sleep(0.2)
        afterOut = _part_at(page, cx, cy)

        print(f"  part under cursor: before={before} afterIn={afterIn} "
              f"afterOut={afterOut}  scale {s0}->{s1}")
        CHECK(s1 and s0 and s1 > s0 * 1.5, "zoom-in increased scale")
        CHECK(before is not None and afterIn == before,
              "same part stays under the cursor after zoom-IN (anchored)")
        CHECK(before is not None and afterOut == before,
              "same part stays under the cursor after zoom-OUT (anchored)")
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
