"""Verify WYSIWYG line weights on the viewer: visible strokes render
with non-scaling-stroke (matching export), while the padded click-target
hit layers are EXCLUDED so selection still works.

Run with the server up:  python tests/smoke_wysiwyg.py [proj/view/fig]
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
    print(f"\n=== WYSIWYG line weights ===\n{url}\n")
    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        print(f"  server not reachable: {e}"); return 2

    from playwright.sync_api import sync_playwright
    js_errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(str(e)))
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
        time.sleep(1.0)

        r = page.evaluate(r"""() => {
          const sel = ".svg-pane[data-view='__live__'] svg";
          const ve = el => el ? getComputedStyle(el).vectorEffect : '(none)';
          const visible = document.querySelector(
            sel+" .layer-outline_v .part path, "
            +sel+" .layer-sharp_v .part path");
          const hit = document.querySelector(
            sel+" .layer-hit-hull .part path");
          return {
            bodyClass: document.body.classList.contains('wysiwyg-weights'),
            flag: window._nonScalingStroke,
            visibleVE: ve(visible),
            hasVisible: !!visible,
            hitVE: ve(hit),
            hasHit: !!hit,
            toggleFn: typeof window._setNonScalingStroke,
          };
        }""")
        CHECK(r["bodyClass"] is True, "body has wysiwyg-weights class")
        CHECK(r["flag"] is True, "window._nonScalingStroke is true")
        CHECK(r["hasVisible"] and r["visibleVE"] == "non-scaling-stroke",
              f"visible line-art stroke is non-scaling ({r['visibleVE']})")
        if r["hasHit"]:
            CHECK(r["hitVE"] != "non-scaling-stroke",
                  f"hit-hull layer is EXCLUDED ({r['hitVE']}) -> click "
                  f"targets preserved")
        CHECK(r["toggleFn"] == "function",
              "window._setNonScalingStroke toggle exposed")

        # clicking still selects a part (hit targets intact)
        time.sleep(0.5)
        pick = page.evaluate(r"""() => {
          const sel = ".svg-pane[data-view='__live__'] svg";
          for (const p of document.querySelectorAll(
              sel+" .layer-outline_v .part[data-part] path")) {
            const r = p.getBoundingClientRect();
            if (r.width>40 && r.height>40)
              return {x:r.left+r.width/2, y:r.top+r.height/2};
          }
          return null;
        }""")
        if pick:
            page.mouse.click(pick["x"], pick["y"])
            time.sleep(0.6)
            nhl = page.evaluate(
                "()=>document.querySelectorAll(\""+SEL
                +" .part.highlight[data-part]\").length")
            CHECK(nhl >= 1, f"clicking still selects a part ({nhl})")
        if js_errors:
            print(f"  JS errors: {js_errors[:2]}")
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
