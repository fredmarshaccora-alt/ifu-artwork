"""Closed-loop visual debugger for the "I clicked one part and a bunch
lit up" complaint.

Unlike the smoke tests (which only assert DOM *counts*), this harness
produces actual PNG screenshots so we can look at PIXELS:

  - BEFORE/AFTER zoomed crops around the clicked part
  - a full-pane screenshot for context
  - a geometric analysis of the layer-silhouette overlay: every drawn
    path's stroke-width / dash / bbox, and which OTHER parts' bounding
    boxes the silhouette stroke physically overlaps (= visual bleed)

Two modes:

  probe (default)  Launch the live editor, click ONE part, screenshot +
                   analyse + persist the state via /api/debug/capture.
      python tests/debug_click_loop.py [proj/view/fig] [--idx N]

  replay <seq>     Re-render a capture the USER made in their browser
                   (via the "📸 capture state" button) and screenshot it.
      python tests/debug_click_loop.py --replay 3

  sweep <n>        Select n parts in turn (clean reload each) and report,
                   per part, whether the footprint silhouette sits on the
                   selected part.  Fast text verdict across the model.
      python tests/debug_click_loop.py --sweep 16

Verdict metric: CONTAINMENT = fraction of the silhouette footprint that
lies inside the selected part's edge bbox.  Robust to occlusion (a
part's visible footprint is smaller than its full edge bbox) and
z-stacking (a tiny part sits inside larger ones) -- both of which fool
naive IoU / sample-attribution.  containment >= 0.5 => footprint is on
the selected part (clean / coincident); < 0.5 => real index mismatch.

Output lands in tests/debug_shots/ ; the textual report prints to stdout
AND writes tests/debug_shots/report.txt.
"""
from __future__ import annotations
import sys
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
import json
import time
from pathlib import Path

import requests

SERVER = "http://127.0.0.1:5000"
DEFAULT = "281cf2da3885/5653e1b90841/decfd8366514"
SHOTS = Path(__file__).parent / "debug_shots"


def _iou_box(b1, b2):
    """Intersection-over-union of two {x,y,w,h} boxes."""
    if not b1 or not b2:
        return 0.0
    ix = max(b1["x"], b2["x"]); iy = max(b1["y"], b2["y"])
    ax = min(b1["x"]+b1["w"], b2["x"]+b2["w"])
    ay = min(b1["y"]+b1["h"], b2["y"]+b2["h"])
    iw = max(0, ax-ix); ih = max(0, ay-iy)
    inter = iw*ih
    uni = b1["w"]*b1["h"] + b2["w"]*b2["h"] - inter
    return inter/uni if uni > 0 else 0.0


def _containment(inner, outer):
    """Fraction of `inner` box area that lies inside `outer` box.
    Robust to occlusion (a part's visible silhouette is smaller than its
    full edge bbox) and z-stacking -- a footprint that sits INSIDE the
    selected part's region scores ~1.0 regardless of relative size.
    A real footprint<->index mismatch puts the silhouette OUTSIDE the
    selected part -> low containment."""
    if not inner or not outer:
        return 0.0
    ix = max(inner["x"], outer["x"]); iy = max(inner["y"], outer["y"])
    ax = min(inner["x"]+inner["w"], outer["x"]+outer["w"])
    ay = min(inner["y"]+inner["h"], outer["y"]+outer["h"])
    iw = max(0, ax-ix); ih = max(0, ay-iy)
    inner_area = inner["w"]*inner["h"]
    return (iw*ih)/inner_area if inner_area > 0 else 0.0

# ---- shared JS snippets -------------------------------------------------

# For each highlighted part, the union bbox of its visible <path> elements
# (across all layer wrappers), in viewport px.  Plus the silhouette layer
# paths and which *other* parts overlap the silhouette ink.
ANALYSE_JS = r"""
(rootSel) => {
  const svg = document.querySelector(rootSel);
  if (!svg) return {error: 'no svg for ' + rootSel};

  // bbox helper in viewport coords
  const bb = el => { const r = el.getBoundingClientRect();
    return {x: r.left, y: r.top, w: r.width, h: r.height,
            cx: r.left + r.width/2, cy: r.top + r.height/2}; };
  const overlap = (a, b) => !(a.x + a.w < b.x || b.x + b.w < a.x ||
                              a.y + a.h < b.y || b.y + b.h < a.y);

  // Every logical part -> union bbox of its real edge paths.  Exclude
  // the hit-hull layer (invisible padded click targets) and the
  // silhouette layer (the overlay we're analysing).
  const partBoxes = {};
  document.querySelectorAll(rootSel + ' .part[data-part]').forEach(g => {
    if (g.closest('.layer-hit-hull')) return;
    if (g.closest('.layer-silhouette')) return;
    const idx = g.dataset.part;
    const paths = [...g.querySelectorAll('path')].filter(p => {
      const r = p.getBoundingClientRect();
      return r.width > 0.5 && r.height > 0.5;
    });
    if (!paths.length) return;
    let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
    paths.forEach(p => { const r = p.getBoundingClientRect();
      x0=Math.min(x0,r.left); y0=Math.min(y0,r.top);
      x1=Math.max(x1,r.right); y1=Math.max(y1,r.bottom); });
    const cur = partBoxes[idx];
    const box = {x:x0,y:y0,w:x1-x0,h:y1-y0};
    if (!cur) partBoxes[idx] = box;
    else partBoxes[idx] = {
      x: Math.min(cur.x, box.x), y: Math.min(cur.y, box.y),
      w: Math.max(cur.x+cur.w, box.x+box.w) - Math.min(cur.x, box.x),
      h: Math.max(cur.y+cur.h, box.y+box.h) - Math.min(cur.y, box.y)};
  });

  // Which parts carry the .highlight class (on any wrapper)?
  const highlighted = new Set();
  document.querySelectorAll(rootSel + ' .part.highlight[data-part]')
    .forEach(g => highlighted.add(g.dataset.part));

  // The silhouette overlay paths.
  const sil = svg.querySelector('g.layer-silhouette');
  const silPaths = sil ? [...sil.querySelectorAll('path')].map(p => ({
    role: p.getAttribute('fill') !== 'none' ? 'fill' : 'stroke',
    stroke: p.getAttribute('stroke'),
    width:  p.getAttribute('stroke-width'),
    dash:   p.getAttribute('stroke-dasharray'),
    fill:   p.getAttribute('fill'),
    fillOpacity: p.getAttribute('fill-opacity'),
    bbox: bb(p),
  })) : [];

  // Union bbox of all silhouette ink (what visually changed).
  let silUnion = null;
  if (silPaths.length) {
    let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
    silPaths.forEach(s => { const r=s.bbox;
      x0=Math.min(x0,r.x); y0=Math.min(y0,r.y);
      x1=Math.max(x1,r.x+r.w); y1=Math.max(y1,r.y+r.h); });
    silUnion = {x:x0,y:y0,w:x1-x0,h:y1-y0};
  }

  // Which NON-highlighted parts does the silhouette ink physically
  // overlap?  These are the parts that LOOK selected but aren't.
  const bledOnto = [];
  if (silUnion) {
    for (const [idx, box] of Object.entries(partBoxes)) {
      if (highlighted.has(idx)) continue;
      if (overlap(silUnion, box)) {
        // fraction of the part's area covered by the silhouette union
        const ix = Math.max(silUnion.x, box.x);
        const iy = Math.max(silUnion.y, box.y);
        const iw = Math.min(silUnion.x+silUnion.w, box.x+box.w) - ix;
        const ih = Math.min(silUnion.y+silUnion.h, box.y+box.h) - iy;
        const frac = (iw>0&&ih>0) ? (iw*ih)/(box.w*box.h) : 0;
        bledOnto.push({idx, frac: Math.round(frac*100)/100});
      }
    }
    bledOnto.sort((a,b) => b.frac - a.frac);
  }

  // Sample points ALONG each silhouette path and, for each sample, find
  // which part's *edge* geometry sits under it.  This tells us whether
  // the footprint overlay traces the SAME part that's highlighted, or a
  // DIFFERENT one (index-mapping bug).
  const samplePartHits = {};
  if (sil) {
    const paths = [...sil.querySelectorAll('path')];
    for (const p of paths) {
      let L = 0; try { L = p.getTotalLength(); } catch(e){}
      if (!L) continue;
      const N = 40;
      for (let k = 0; k <= N; k++) {
        let pt; try { pt = p.getPointAtLength(L*k/N); } catch(e){ continue; }
        const m = p.getScreenCTM(); if (!m) continue;
        const x = m.a*pt.x + m.c*pt.y + m.e;
        const y = m.b*pt.x + m.d*pt.y + m.f;
        const els = document.elementsFromPoint(x, y);
        for (const el of els) {
          const g = el.closest && el.closest('.part[data-part]');
          if (g && !g.closest('.layer-hit-hull')
                && !g.closest('.layer-silhouette')) {
            const i = g.dataset.part;
            samplePartHits[i] = (samplePartHits[i]||0) + 1;
            break;
          }
        }
      }
    }
  }

  return {
    highlighted: [...highlighted].sort((a,b)=>a-b),
    partCount: Object.keys(partBoxes).length,
    silPathCount: silPaths.length,
    silPaths,
    silUnion,
    selectedBoxes: [...highlighted].map(i => ({idx:i, box:partBoxes[i]})),
    bledOnto,
    samplePartHits,   // part_idx -> how many silhouette samples sit on it
    partBoxes,        // every part's union bbox (for cross-checking)
  };
}
"""


def _wait_svg(page, sel, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = page.eval_on_selector_all(
            sel, "els => els.map(el => el.outerHTML.length)")
        if n and max(n) > 1024:
            return True
        time.sleep(0.5)
    return False


def _wait_attached(page, sel, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = page.evaluate(
            "(s) => { const e = document.querySelector(s); "
            "return !!(e && e.dataset.attached); }", sel)
        if ok:
            return True
        time.sleep(0.3)
    return False


def _crop_for(page, box, pad=130):
    """Clip dict around a viewport bbox, clamped to the page."""
    vp = page.viewport_size
    x = max(0, box["x"] - pad)
    y = max(0, box["y"] - pad)
    w = min(vp["width"] - x, box["w"] + 2 * pad)
    h = min(vp["height"] - y, box["h"] + 2 * pad)
    if w <= 4 or h <= 4:
        return None
    return {"x": x, "y": y, "width": w, "height": h}


def probe(target, idx_arg):
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    sel = ".svg-pane[data-view='__live__'] svg"
    SHOTS.mkdir(parents=True, exist_ok=True)
    report = []
    R = lambda s: (report.append(s), print(s))[1]

    R(f"\n=== closed-loop click probe ===\n{url}\n")
    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        R(f"  server not reachable: {e}"); return 2

    from playwright.sync_api import sync_playwright
    js_errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080},
                            device_scale_factor=2)
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.goto(url, wait_until="load", timeout=60000)

        if not _wait_svg(page, sel):
            R("  FAIL live SVG never rendered"); b.close(); return 1
        _wait_attached(page, sel)
        time.sleep(2)   # baseline settle

        # Pick the part to click.  If --idx given, target that data-part;
        # else auto-pick a mid-sized part near the centre so the crop has
        # neighbours around it.
        pick = page.evaluate(r"""
        (arg) => {
          const sel = ".svg-pane[data-view='__live__'] svg";
          // Only VISIBLE edge layers -- click a line the user can see
          // (matches the resolver's nearest-edge metric).
          const groups = [...document.querySelectorAll(
            sel + " .layer-outline_v .part[data-part], "
            + sel + " .layer-sharp_v .part[data-part], "
            + sel + " .layer-smooth_v .part[data-part]")];
          // For each part keep its longest visible path so we can click a
          // point that's actually ON the ink (getPointAtLength midpoint),
          // not the bbox centre -- the bbox centre of a ring/diagonal
          // part lands in empty space and routes the click elsewhere.
          const byIdx = {};
          for (const g of groups) {
            const i = g.dataset.part;
            for (const p of g.querySelectorAll('path')) {
              const r = p.getBoundingClientRect();
              if (r.width < 3 || r.height < 3) continue;
              let len = 0;
              try { len = p.getTotalLength(); } catch(e) {}
              if (!byIdx[i] || len > byIdx[i]._len) {
                byIdx[i] = {idx:i, _len:len, _path:p,
                  bx:r.left+r.width/2, by:r.top+r.height/2,
                  w:r.width, h:r.height};
              }
            }
          }
          const finish = (t) => {
            if (!t) return null;
            let x = t.bx, y = t.by;
            try {
              const pt = t._path.getPointAtLength(t._len * 0.5);
              // map SVG user-space point to viewport px via CTM
              const m = t._path.getScreenCTM();
              if (m) { x = m.a*pt.x + m.c*pt.y + m.e;
                       y = m.b*pt.x + m.d*pt.y + m.f; }
            } catch(e) {}
            return {idx:t.idx, x:x, y:y, w:t.w, h:t.h};
          };
          const list = Object.values(byIdx);
          if (!list.length) return null;
          if (arg.idx != null) return finish(byIdx[String(arg.idx)]);
          list.sort((a,b)=>a.w*a.h - b.w*b.h);
          return finish(list[Math.floor(list.length/2)]);
        }
        """, {"idx": idx_arg})
        if not pick:
            R("  FAIL no clickable part found"); b.close(); return 1
        R(f"  target part idx={pick['idx']} "
          f"size={pick['w']:.0f}x{pick['h']:.0f}px "
          f"at ({pick['x']:.0f},{pick['y']:.0f})")

        # Which parts' click targets are STACKED under this pixel?  If
        # several hit-hulls overlap here, the topmost one wins -- that's
        # how "I clicked part A but part B highlighted" happens.
        stack = page.evaluate(r"""
        (pt) => {
          const els = document.elementsFromPoint(pt.x, pt.y);
          const out = [];
          for (const el of els) {
            const g = el.closest && el.closest('.part[data-part]');
            if (!g) continue;
            const layer = (g.closest('g[class*="layer-"]')||{});
            out.push({idx: g.dataset.part,
              layer: (layer.getAttribute &&
                      layer.getAttribute('class')) || '?',
              tag: el.tagName});
          }
          return out;
        }
        """, {"x": pick["x"], "y": pick["y"]})
        seen = []
        for s in stack:
            tag = f"{s['idx']}({s['layer']})"
            if tag not in seen:
                seen.append(tag)
        R(f"  click-point element stack (top->bottom): {seen[:8]}")

        # Replicate _resolvePartClick's math so we can see candidates +
        # per-candidate nearest-edge distance (debug the resolver).
        dbg = page.evaluate(r"""
        (pt) => {
          const svg = document.querySelector(
            ".svg-pane[data-view='__live__'] svg");
          const stack = document.elementsFromPoint(pt.x, pt.y);
          const cands = [];
          for (const el of stack) {
            const g = el.closest && el.closest('.part[data-part]');
            if (g && !g.closest('.layer-silhouette')) {
              const i = parseInt(g.dataset.part);
              if (!Number.isNaN(i) && !cands.includes(i)) cands.push(i);
            }
          }
          // edge polylines per part (visible layers) -- mirror the real
          // resolver: point-to-SEGMENT distance, not point-to-vertex.
          const m = new Map();
          ['.layer-outline_v','.layer-sharp_v','.layer-smooth_v'].forEach(s=>{
            svg.querySelectorAll(s+' .part[data-part]').forEach(g=>{
              const i=parseInt(g.dataset.part); if(Number.isNaN(i))return;
              let a=m.get(i); if(!a){a=[];m.set(i,a);}
              g.querySelectorAll('path').forEach(p=>{
                const t=(p.getAttribute('d')||'').match(/-?\d+(?:\.\d+)?/g);
                if(!t||t.length<2)return; const pl=[];
                for(let k=0;k+1<t.length;k+=2)
                  pl.push([parseFloat(t[k]),parseFloat(t[k+1])]);
                if(pl.length)a.push(pl);
              });
            });
          });
          const seg2=(px,py,ax,ay,bx,by)=>{
            const vx=bx-ax,vy=by-ay,wx=px-ax,wy=py-ay;
            const c1=vx*wx+vy*wy; if(c1<=0)return wx*wx+wy*wy;
            const c2=vx*vx+vy*vy; if(c2<=c1){const dx=px-bx,dy=py-by;
              return dx*dx+dy*dy;}
            const t=c1/c2,dx=px-(ax+t*vx),dy=py-(ay+t*vy);
            return dx*dx+dy*dy;
          };
          const ap = svg.querySelector('.layer-hit-hull path')
                  || svg.querySelector('.layer-outline_v path');
          let cx=pt.x, cy=pt.y;
          try { const ctm=ap.getScreenCTM();
            const q=new DOMPoint(pt.x,pt.y).matrixTransform(ctm.inverse());
            cx=q.x; cy=q.y; } catch(e){}
          const out = [];
          for (const i of cands) {
            const polys=m.get(i); let d=Infinity, nv=0;
            if(polys)for(const pl of polys){nv+=pl.length;
              for(let k=0;k+1<pl.length;k++){
                const dd=seg2(cx,cy,pl[k][0],pl[k][1],pl[k+1][0],pl[k+1][1]);
                if(dd<d)d=dd;}}
            out.push({idx:i, dist:Math.sqrt(d), nverts:nv});
          }
          out.sort((a,b)=>a.dist-b.dist);
          return {cands, dists: out, userPt:[cx,cy],
                  winner: out.length?out[0].idx:null};
        }
        """, {"x": pick["x"], "y": pick["y"]})
        R(f"  resolver candidates: {dbg.get('cands')} "
          f"(nearest-edge winner = {dbg.get('winner')})")
        for d in dbg.get("dists", []):
            R(f"      part {d['idx']}: nearest-edge(segment) dist="
              f"{d['dist']:.1f} (nverts={d['nverts']})")
        hull_parts = sorted({s['idx'] for s in stack
                             if 'hit-hull' in s['layer']})
        if len(hull_parts) > 1:
            R(f"  ⚠ {len(hull_parts)} part hit-hulls overlap this pixel: "
              f"{hull_parts} -- topmost wins the click")

        # BEFORE crop
        crop = _crop_for(page, {"x": pick["x"]-pick["w"]/2,
                                 "y": pick["y"]-pick["h"]/2,
                                 "w": pick["w"], "h": pick["h"]})
        before = SHOTS / f"idx{pick['idx']}_before.png"
        if crop:
            page.screenshot(path=str(before), clip=crop)
            R(f"  saved {before.name}")

        # CLICK it.  Ensure the target ends up SELECTED (clicking a
        # part that's already selected toggles it off -- if that happens
        # we click again so the run is deterministic regardless of any
        # persisted selection state).
        def _is_highlighted(idx):
            return page.evaluate(
                "(i)=>!!document.querySelector(\".svg-pane"
                "[data-view='__live__'] svg .part.highlight[data-part='\"+i+\"']\")",
                str(idx))
        _hl_set = lambda: page.evaluate(
            "()=>[...document.querySelectorAll(\".svg-pane[data-view="
            "'__live__'] svg .part.highlight[data-part]\")]"
            ".map(g=>g.dataset.part).filter((v,i,a)=>a.indexOf(v)===i)")
        page.mouse.click(pick["x"], pick["y"])
        time.sleep(0.8)
        R(f"  after 1 click, highlighted set = {sorted(_hl_set())} "
          f"(intended target = {pick['idx']})")
        if str(pick["idx"]) not in _hl_set():
            R(f"  ⚠ clicking ON part {pick['idx']} did NOT select it -- "
              f"a different part's hit-hull intercepted the click")
            R(f"  (clicking again to force the run deterministic)")
            page.mouse.click(pick["x"], pick["y"])
            time.sleep(0.8)
        R(f"  target idx {pick['idx']} highlighted: "
          f"{_is_highlighted(pick['idx'])}")

        # AFTER crop (same region) -- this is the IMMEDIATE click
        # feedback, before the async footprint silhouette arrives.
        after = SHOTS / f"idx{pick['idx']}_after.png"
        if crop:
            page.screenshot(path=str(after), clip=crop)
            R(f"  saved {after.name}  (immediate click feedback)")

        # Now WAIT for the async footprint silhouette to actually draw
        # (server rasters the assembly the first time; can take 3-15s).
        sil_n = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            sil_n = page.evaluate(
                "(s)=>{const g=document.querySelector(s+' g.layer-silhouette');"
                "return g?g.querySelectorAll('path').length:0;}", sel)
            if sil_n > 0:
                break
            time.sleep(0.5)
        time.sleep(0.4)
        R(f"  silhouette paths after wait: {sil_n}")
        after_sil = SHOTS / f"idx{pick['idx']}_after_silhouette.png"
        if crop:
            page.screenshot(path=str(after_sil), clip=crop)
            R(f"  saved {after_sil.name}  (with footprint silhouette)")

        # full pane for context
        full = SHOTS / f"idx{pick['idx']}_full.png"
        pane = page.query_selector(".svg-pane[data-view='__live__']")
        if pane:
            pane.screenshot(path=str(full))
            R(f"  saved {full.name}")

        # ---- analysis -------------------------------------------------
        a = page.evaluate(ANALYSE_JS, sel)
        R("\n  --- analysis ---")
        R(f"  highlighted parts (.highlight class): {a.get('highlighted')}")
        R(f"  total parts in view: {a.get('partCount')}")
        R(f"  silhouette paths drawn: {a.get('silPathCount')}")
        for i, sp in enumerate(a.get("silPaths", [])):
            R(f"    [{i}] {sp['role']:6} stroke={sp['stroke']} "
              f"w={sp['width']} dash={sp['dash']} "
              f"fill={sp['fill']} fop={sp['fillOpacity']}")
        # Numeric ground truth (no pixel interpretation).
        su = a.get("silUnion")
        if su:
            R(f"  silhouette union bbox: x={su['x']:.0f} y={su['y']:.0f} "
              f"w={su['w']:.0f} h={su['h']:.0f}")
        hbox = None
        for sb in a.get("selectedBoxes", []):
            bx = sb.get("box")
            if bx:
                if hbox is None:
                    hbox = bx
                R(f"  highlighted part {sb['idx']} bbox: x={bx['x']:.0f} "
                  f"y={bx['y']:.0f} w={bx['w']:.0f} h={bx['h']:.0f}")
        # THE decisive metric: which parts do the silhouette samples
        # actually sit on?  If samples sit mostly on the highlighted
        # part -> footprint is correct.  If they sit on OTHER parts ->
        # footprint<->part index mismatch (the real bug).
        sph = a.get("samplePartHits", {})
        if sph:
            ranked = sorted(sph.items(), key=lambda kv: -kv[1])
            R(f"  silhouette samples sit on parts (idx:count): "
              f"{ranked[:8]}")
            pboxes = a.get("partBoxes", {})
            for idx, cnt in ranked[:5]:
                bx = pboxes.get(idx)
                if bx:
                    R(f"      part {idx} (x{cnt}) bbox: x={bx['x']:.0f} "
                      f"y={bx['y']:.0f} w={bx['w']:.0f} h={bx['h']:.0f}")
            hl = set(a.get("highlighted", []))
            on_hl = sum(c for i, c in sph.items() if i in hl)
            on_other = sum(c for i, c in sph.items() if i not in hl)
            R(f"  -> {on_hl} samples on highlighted part(s), "
              f"{on_other} on OTHER parts")
            # RELIABLE signal: how much of the silhouette footprint lies
            # INSIDE the selected part's edge bbox (containment).  This is
            # robust to occlusion (visible footprint << full edge bbox,
            # e.g. part 24) and z-stacking (tiny part over big parts,
            # e.g. part 30) -- both confound IoU and sample-attribution.
            contain = _containment(su, hbox) if (su and hbox) else 0.0
            R(f"  containment(silhouette inside selected part bbox) "
              f"= {contain:.2f}")
            if contain >= 0.5:
                R("  ✓ silhouette footprint sits on the selected part "
                  "(no index mismatch)")
                pboxes = a.get("partBoxes", {})
                coinc = [i for i in sph
                         if i not in hl
                         and _iou_box(hbox, pboxes.get(i)) >= 0.5]
                if coinc:
                    R(f"  ℹ overlapping/coincident solids share this exact "
                      f"region: {coinc} -- highlighting one visually traces "
                      f"the others (source-model trait, not a bug)")
            elif su and hbox:
                R(f"  ⚠⚠ silhouette footprint is OUTSIDE the selected part "
                  f"(containment={contain:.2f}) -- possible footprint<->"
                  f"index MISMATCH; inspect "
                  f"idx{pick['idx']}_silhouette_only.png")

        bled = a.get("bledOnto", [])
        if bled:
            R(f"  (union-bbox overlaps {len(bled)} non-selected part "
              f"bboxes -- expected for large/diagonal parts)")

        # persist via the real capture endpoint FIRST (clean state,
        # before we pollute the SVG with region-debug inline styles).
        cap = page.evaluate(
            "() => window._captureState && window._captureState("
            "'probe idx=" + str(pick['idx']) + "')")
        if cap and cap.get("seq"):
            R(f"\n  persisted capture seq={cap['seq']} "
              f"-> {cap.get('svg_path')}")

        # ---- region-ownership screenshot -----------------------------
        # Colour every part uniquely so we can SEE which pixels belong to
        # the clicked part vs its neighbours.  This is what decides
        # "decomposition (one big weldment)" vs "real bleed onto other
        # parts".  Also isolate the clicked part in solid red.
        page.evaluate(r"""
        (idx) => {
          const PAL = ['#ef4444','#f59e0b','#84cc16','#10b981','#06b6d4',
            '#6366f1','#ec4899','#0ea5e9','#a855f7','#f43f5e','#22c55e',
            '#eab308','#14b8a6','#3b82f6','#d946ef'];
          const sel = ".svg-pane[data-view='__live__'] svg";
          // hide the silhouette overlay so it doesn't repaint over us
          const g = document.querySelector(sel + ' g.layer-silhouette');
          if (g) g.style.display = 'none';
          document.querySelectorAll(sel + ' .part[data-part]').forEach(grp => {
            if (grp.closest('.layer-hit-hull')) return;
            const i = parseInt(grp.dataset.part);
            const isTarget = String(i) === String(idx);
            grp.querySelectorAll('path').forEach(p => {
              if (isTarget) { p.style.stroke = '#ff0000';
                p.style.strokeWidth = '2px'; p.style.opacity = '1'; }
              else { p.style.stroke = PAL[i % PAL.length];
                p.style.opacity = '0.85'; }
            });
          });
        }
        """, pick["idx"])
        time.sleep(0.4)
        reg = SHOTS / f"idx{pick['idx']}_regions.png"
        if crop:
            page.screenshot(path=str(reg), clip=crop)
            R(f"  saved {reg.name}  (clicked part RED, others by colour)")
        regfull = SHOTS / f"idx{pick['idx']}_regions_full.png"
        if pane:
            pane.screenshot(path=str(regfull))
            R(f"  saved {regfull.name}")

        # ---- silhouette-ONLY render ----------------------------------
        # Hide every edge layer, show ONLY layer-silhouette, so we can
        # see exactly where the footprint overlay draws.  If it traces a
        # DIFFERENT part than the red one above, the footprint->part_idx
        # mapping is wrong (a real bug).  Also report which part the
        # silhouette centre lands on.
        silinfo = page.evaluate(r"""
        () => {
          const sel = ".svg-pane[data-view='__live__'] svg";
          const svg = document.querySelector(sel);
          // show silhouette, grey-ghost everything else
          svg.querySelectorAll(':scope > g > g, :scope > g').forEach(()=>{});
          const sg = svg.querySelector('g.layer-silhouette');
          if (sg) sg.style.display = '';
          // dim all part edges to faint grey
          svg.querySelectorAll(sel + ' .part[data-part] path').forEach(p=>{
            if (p.closest('.layer-silhouette')) return;
            p.style.stroke = '#e5e5e5'; p.style.opacity = '0.5';
          });
          // recolour silhouette bright magenta so it pops
          let cx=0, cy=0, n=0;
          if (sg) sg.querySelectorAll('path').forEach(p=>{
            p.setAttribute('stroke', '#ff00ff');
            p.setAttribute('stroke-width', '1.2');
            p.removeAttribute('stroke-dasharray');
            const r = p.getBoundingClientRect();
            cx += r.left + r.width/2; cy += r.top + r.height/2; n++;
          });
          return n ? {cx: cx/n, cy: cy/n} : null;
        }
        """)
        time.sleep(0.3)
        silonly = SHOTS / f"idx{pick['idx']}_silhouette_only.png"
        if pane:
            pane.screenshot(path=str(silonly))
            R(f"  saved {silonly.name}  (footprint overlay in magenta)")
        if silinfo:
            # which part bbox contains the silhouette centre?
            land = page.evaluate(r"""
            (pt) => {
              const els = document.elementsFromPoint(pt.cx, pt.cy);
              for (const el of els) {
                const g = el.closest && el.closest('.part[data-part]');
                if (g && !g.closest('.layer-hit-hull')
                      && !g.closest('.layer-silhouette'))
                  return g.dataset.part;
              }
              return null;
            }""", silinfo)
            R(f"  silhouette ink centre lands on part: {land} "
              f"(clicked/red part = {pick['idx']})")

        if js_errors:
            R(f"\n  JS ERRORS: {js_errors[:3]}")
        b.close()

    (SHOTS / "report.txt").write_text("\n".join(report), encoding="utf-8")
    R(f"\n  report -> {SHOTS / 'report.txt'}")
    return 0


def replay(seq):
    """Re-render a user capture standalone + screenshot/analyse it."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    report = []
    R = lambda s: (report.append(s), print(s))[1]
    R(f"\n=== replay capture #{seq} ===\n")

    meta = requests.get(f"{SERVER}/api/debug/captures/{seq}",
                        timeout=5).json()
    if not meta or meta.get("ok") is False:
        R(f"  capture {seq} not found"); return 1
    svg_file = meta.get("svg_file")
    # read svg straight off disk via the server's capture dir listing
    lst = requests.get(f"{SERVER}/api/debug/captures", timeout=5).json()
    cap_dir = Path(lst.get("dir"))
    svg_path = cap_dir / svg_file
    if not svg_path.exists():
        R(f"  svg file missing: {svg_path}"); return 1
    svg = svg_path.read_text(encoding="utf-8")
    R(f"  note={meta.get('note')!r} selection={meta.get('selection')} "
      f"svg={len(svg)//1024}KB")

    # Wrap standalone.  White background so the line art is visible.
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<style>html,body{margin:0;background:#fff}"
            "svg{width:100vw;height:100vh}</style></head>"
            "<body>" + svg + "</body></html>")
    wrap = SHOTS / f"replay_{seq:04d}.html"
    wrap.write_text(html, encoding="utf-8")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 1200},
                            device_scale_factor=2)
        page = ctx.new_page()
        page.goto(wrap.as_uri(), wait_until="load", timeout=30000)
        time.sleep(0.6)
        full = SHOTS / f"replay_{seq:04d}_full.png"
        page.screenshot(path=str(full))
        R(f"  saved {full.name}")

        a = page.evaluate(ANALYSE_JS, "svg")
        R(f"  highlighted: {a.get('highlighted')}  "
          f"silPaths: {a.get('silPathCount')}")
        for i, sp in enumerate(a.get("silPaths", [])):
            R(f"    [{i}] {sp['role']:6} w={sp['width']} "
              f"dash={sp['dash']} fill={sp['fill']}")
        for s in a.get("selectedBoxes", []):
            if not s.get("box"):
                continue
            crop = _crop_for(page, s["box"])
            if crop:
                shot = SHOTS / f"replay_{seq:04d}_idx{s['idx']}.png"
                page.screenshot(path=str(shot), clip=crop)
                R(f"  saved {shot.name} (zoom on selected idx {s['idx']})")
        # Containment verdict on the CAPTURED state (what the user saw).
        su = a.get("silUnion")
        pboxes = a.get("partBoxes", {})
        hl = a.get("highlighted", [])
        hbox = pboxes.get(hl[0]) if hl else None
        if su:
            R(f"  silhouette union bbox: x={su['x']:.0f} y={su['y']:.0f} "
              f"w={su['w']:.0f} h={su['h']:.0f}")
        if hbox:
            R(f"  highlighted part {hl[0]} EDGE bbox: x={hbox['x']:.0f} "
              f"y={hbox['y']:.0f} w={hbox['w']:.0f} h={hbox['h']:.0f}")
        sph = a.get("samplePartHits", {})
        if sph:
            ranked = sorted(sph.items(), key=lambda kv: -kv[1])
            R(f"  silhouette samples sit on parts: {ranked[:6]}")
        contain = _containment(su, hbox) if (su and hbox) else 0.0
        R(f"  containment(silhouette inside part {hl[0] if hl else '?'} "
          f"edge bbox) = {contain:.2f}")
        if su and hbox and contain < 0.5:
            R("  ⚠⚠ in THIS captured state, the silhouette does NOT sit on "
              "the highlighted part's own edges -- 2D footprint and 2D "
              "edges disagree about which part is #" + str(hl[0]))
        b.close()

    (SHOTS / f"report_replay_{seq}.txt").write_text(
        "\n".join(report), encoding="utf-8")
    return 0


def sweep(target, n):
    """Select many parts in turn and report, per part, whether the
    footprint silhouette matches the selected part's region (coincident
    overlap is fine) or lands on a DISJOINT part (a real index bug).
    No screenshots -- fast text verdict across the model."""
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    sel = ".svg-pane[data-view='__live__'] svg"
    print(f"\n=== highlight sweep ({n} parts) ===\n{url}\n")

    def _iou(b1, b2):
        if not b1 or not b2: return 0.0
        ix=max(b1["x"],b2["x"]); iy=max(b1["y"],b2["y"])
        ax=min(b1["x"]+b1["w"],b2["x"]+b2["w"])
        ay=min(b1["y"]+b1["h"],b2["y"]+b2["h"])
        iw=max(0,ax-ix); ih=max(0,ay-iy); inter=iw*ih
        uni=b1["w"]*b1["h"]+b2["w"]*b2["h"]-inter
        return inter/uni if uni>0 else 0.0

    from playwright.sync_api import sync_playwright
    mismatches, coincident, clean = [], [], []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.goto(url, wait_until="load", timeout=60000)
        if not _wait_svg(page, sel):
            print("  FAIL no SVG"); b.close(); return 1
        _wait_attached(page, sel)
        time.sleep(2)

        # candidate part idxs: spread across the available parts
        idxs = page.evaluate(
            "(s)=>[...new Set([...document.querySelectorAll("
            "s+' .part[data-part]')].filter(g=>!g.closest('.layer-hit-hull'))"
            ".map(g=>g.dataset.part))]", sel)
        idxs = sorted(idxs, key=lambda v: int(v))
        step = max(1, len(idxs)//n)
        chosen = idxs[::step][:n]
        print(f"  {len(idxs)} parts in view; testing {chosen}\n")

        for idx in chosen:
            # Reload to a CLEAN state before each part so a prior
            # selection can't leak across iterations (which would make
            # the next click look like a deselect).  Server footprint
            # cache stays warm, so the silhouette still draws fast.
            page.goto(url, wait_until="load", timeout=60000)
            if not _wait_svg(page, sel):
                print(f"  part {idx:>3}: SVG didn't render (skip)")
                continue
            _wait_attached(page, sel)
            time.sleep(1.2)
            # point on this part's longest path
            pt = page.evaluate(r"""
            (a) => {
              const sel = a.sel;
              // a part lives in MANY layer wrappers; scan the VISIBLE
              // edge layers and take the longest path across them.
              const groups = [...document.querySelectorAll(
                sel+" .layer-outline_v .part[data-part='"+a.idx+"'], "
                +sel+" .layer-sharp_v .part[data-part='"+a.idx+"'], "
                +sel+" .layer-smooth_v .part[data-part='"+a.idx+"']")];
              let best=null,bl=0;
              for (const g of groups) {
                for (const p of g.querySelectorAll('path')) {
                  const r=p.getBoundingClientRect();
                  if (r.width<2&&r.height<2) continue;
                  let L=0; try{L=p.getTotalLength();}catch(e){}
                  if (L>bl){bl=L;best=p;}
                }
              }
              if (!best) return null;
              try {
                const q=best.getPointAtLength(bl*0.5);
                const m=best.getScreenCTM();
                return {x:m.a*q.x+m.c*q.y+m.e, y:m.b*q.x+m.d*q.y+m.f};
              } catch(e){ return null; }
            }""", {"sel": sel, "idx": idx})
            if not pt:
                print(f"  part {idx:>3}: no clickable point found (skip)")
                continue
            if not (0 <= pt["x"] <= 1920 and 0 <= pt["y"] <= 1080):
                print(f"  part {idx:>3}: point off-screen "
                      f"({pt['x']:.0f},{pt['y']:.0f}) (skip)")
                continue
            page.mouse.click(pt["x"], pt["y"])
            time.sleep(0.5)
            hl = page.evaluate(
                "(i)=>!!document.querySelector(\".svg-pane[data-view="
                "'__live__'] svg .part.highlight[data-part='\"+i+\"']\")",
                str(idx))
            if not hl:
                page.mouse.click(pt["x"], pt["y"]); time.sleep(0.5)
            # wait silhouette (cache warm -> fast)
            for _ in range(20):
                sn = page.evaluate(
                    "(s)=>{const g=document.querySelector(s+"
                    "' g.layer-silhouette');return g?g.querySelectorAll("
                    "'path').length:0;}", sel)
                if sn: break
                time.sleep(0.3)
            a = page.evaluate(ANALYSE_JS, sel)
            sph = a.get("samplePartHits", {})
            hlset = set(a.get("highlighted", []))
            pboxes = a.get("partBoxes", {})
            su = a.get("silUnion")
            hbox = pboxes.get(next(iter(hlset), None)) if hlset else None
            # Reliable signal: containment of silhouette inside the
            # selected part's bbox (robust to occlusion + z-stacking;
            # see probe() notes).
            contain = _containment(su, hbox) if (su and hbox) else 0.0
            coinc = [i for i in sph
                     if i not in hlset and hbox
                     and _iou_box(hbox, pboxes.get(i)) >= 0.5]
            if not hlset or not su:
                verdict = "no-select"   # click didn't take / no silhouette
            elif contain >= 0.5:
                if coinc:
                    verdict = "coincident"; coincident.append(idx)
                else:
                    verdict = "clean"; clean.append(idx)
            else:
                verdict = "MISMATCH"; mismatches.append(idx)
            print(f"  part {idx:>3}: highlighted={sorted(hlset)} "
                  f"sil={a.get('silPathCount')} contain={contain:.2f} "
                  f"{('coinc='+','.join(coinc)) if coinc else ''}"
                  f"  -> {verdict}")
        b.close()

    print(f"\n  SUMMARY: clean={len(clean)} coincident={len(coincident)} "
          f"mismatch={len(mismatches)}")
    if mismatches:
        print(f"  ⚠⚠ REAL index mismatches on parts: {mismatches}")
    else:
        print("  ✓ no real (disjoint) footprint<->index mismatches found")
    return 0


def main():
    argv = sys.argv[1:]
    if "--replay" in argv:
        i = argv.index("--replay")
        seq = int(argv[i+1])
        return replay(seq)
    if "--sweep" in argv:
        i = argv.index("--sweep")
        n = int(argv[i+1]) if i+1 < len(argv) and argv[i+1].isdigit() else 10
        rest = [x for j, x in enumerate(argv) if j not in (i, i+1)]
        tgt = rest[0] if rest else DEFAULT
        return sweep(tgt, n)
    idx_arg = None
    if "--idx" in argv:
        i = argv.index("--idx")
        idx_arg = int(argv[i+1])
        argv = argv[:i] + argv[i+2:]
    target = argv[0] if argv else DEFAULT
    return probe(target, idx_arg)


if __name__ == "__main__":
    sys.exit(main())
