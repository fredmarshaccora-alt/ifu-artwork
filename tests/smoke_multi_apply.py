"""Reproduce the user's bug: apply Highlight to part A (works), then
select part B and apply Highlight -> B doesn't visibly highlight.

For each applied part we check BOTH:
  - it's in localStorage partStyles (logical apply worked)
  - the persistent-silhouette layer actually has ink overlapping that
    part's bbox (visible render worked)

If A renders but B doesn't, we've reproduced it.

Run with the server up:
    python tests/smoke_multi_apply.py [proj/view/fig] [--idxs A,B,C]
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


def _wait_svg(page, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = page.eval_on_selector_all(
            SEL, "els => els.map(el => el.outerHTML.length)")
        if n and max(n) > 1024:
            return True
        time.sleep(0.5)
    return False


def _point_on_part(page, idx):
    """Viewport point on part idx's longest visible edge."""
    return page.evaluate(r"""
    (idx) => {
      const sel = ".svg-pane[data-view='__live__'] svg";
      const groups = [...document.querySelectorAll(
        sel+" .layer-outline_v .part[data-part='"+idx+"'], "
        +sel+" .layer-sharp_v .part[data-part='"+idx+"'], "
        +sel+" .layer-smooth_v .part[data-part='"+idx+"']")];
      let best=null,bl=0;
      for (const g of groups) for (const p of g.querySelectorAll('path')) {
        const r=p.getBoundingClientRect();
        if (r.width<2&&r.height<2) continue;
        let L=0; try{L=p.getTotalLength();}catch(e){}
        if (L>bl){bl=L;best=p;}
      }
      if (!best) return null;
      try { const q=best.getPointAtLength(bl*0.5); const m=best.getScreenCTM();
        return {x:m.a*q.x+m.c*q.y+m.e, y:m.b*q.x+m.d*q.y+m.f}; }
      catch(e){ return null; }
    }""", str(idx))


def _persistent_hits(page):
    """For each part idx, does the persistent-silhouette layer draw ink
    overlapping that part's edge bbox?  Returns {idx: bool}."""
    return page.evaluate(r"""
    () => {
      const sel = ".svg-pane[data-view='__live__'] svg";
      const svg = document.querySelector(sel);
      const lay = svg && svg.querySelector('g.layer-persistent-silhouette');
      const bb = el => { const r = el.getBoundingClientRect();
        return {x:r.left,y:r.top,w:r.width,h:r.height}; };
      const ov = (a,b) => !(a.x+a.w<b.x||b.x+b.w<a.x||a.y+a.h<b.y||b.y+b.h<a.y);
      // persistent silhouette path bboxes
      const silBoxes = lay ? [...lay.querySelectorAll('path')].map(bb) : [];
      // part edge bboxes
      const partBox = {};
      document.querySelectorAll(sel+' .part[data-part]').forEach(g=>{
        if (g.closest('.layer-hit-hull')||g.closest('.layer-silhouette')
            ||g.closest('.layer-persistent-silhouette')) return;
        const i=g.dataset.part;
        for (const p of g.querySelectorAll('path')){
          const r=p.getBoundingClientRect();
          if (r.width<0.5&&r.height<0.5) continue;
          const cur=partBox[i];
          const box={x:r.left,y:r.top,w:r.width,h:r.height};
          if(!cur)partBox[i]=box; else partBox[i]={
            x:Math.min(cur.x,box.x),y:Math.min(cur.y,box.y),
            w:Math.max(cur.x+cur.w,box.x+box.w)-Math.min(cur.x,box.x),
            h:Math.max(cur.y+cur.h,box.y+box.h)-Math.min(cur.y,box.y)};
        }
      });
      const out = {silPathCount: silBoxes.length, hits: {}};
      for (const [i,box] of Object.entries(partBox))
        out.hits[i] = silBoxes.some(s => ov(s,box));
      return out;
    }""")


def _apply_highlight(page):
    return page.evaluate("""() => {
      const b = [...document.querySelectorAll('#preset-row .preset-btn,'
        +' #preset-row button')].find(x=>/highlight/i.test(x.textContent||''));
      if (!b) return false; b.click(); return true;
    }""")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else DEFAULT
    idxs = [5, 52, 30]
    if "--idxs" in sys.argv:
        idxs = [int(x) for x in sys.argv[sys.argv.index("--idxs")+1].split(",")]
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    print(f"\n=== multi-apply repro ===\n{url}\n  parts: {idxs}\n")
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
        if not _wait_svg(page):
            print("  FAIL no SVG"); b.close(); return 1
        for _ in range(30):
            if page.evaluate("()=>{const s=document.querySelector(\""+SEL+"\");"
                             "return !!(s&&s.dataset.attached);}"): break
            time.sleep(0.3)
        time.sleep(2)

        # Clean slate: clear any existing applied styles for this figure.
        page.evaluate("""() => {
          localStorage.setItem(window._figStyleKey(), '{}');
          if (window.applyStyleSheet) window.applyStyleSheet();
        }""")
        time.sleep(0.5)

        applied = []
        for n, idx in enumerate(idxs, 1):
            pt = _point_on_part(page, idx)
            if not pt:
                CHECK(False, f"part {idx}: no clickable edge point"); continue
            page.mouse.click(pt["x"], pt["y"])
            time.sleep(0.6)
            # ensure selected
            sel_ok = page.evaluate(
                "(i)=>!!document.querySelector(\""+SEL
                +" .part.highlight[data-part='\"+i+\"']\")", str(idx))
            if not sel_ok:
                page.mouse.click(pt["x"], pt["y"]); time.sleep(0.6)
            ok_apply = _apply_highlight(page)
            time.sleep(1.0)
            try:
                page.evaluate("()=>window._flushAutoSave&&window._flushAutoSave()")
            except Exception:
                pass
            time.sleep(0.5)
            applied.append(idx)

            keys = page.evaluate("() => Object.keys(window._figStyles())")
            pers = _persistent_hits(page)
            print(f"\n  -- after applying #{n} (part {idx}) --")
            print(f"     preset clicked: {ok_apply}")
            print(f"     localStorage styled keys: {sorted(keys, key=int)}")
            print(f"     persistent silhouette paths: {pers['silPathCount']}")
            for a in applied:
                in_ls = str(a) in keys
                visible = pers["hits"].get(str(a), False)
                CHECK(in_ls and visible,
                      f"part {a}: styled={in_ls} visible-persistent={visible}")

        if js_errors:
            print(f"\n  JS ERRORS: {js_errors[:3]}")
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
