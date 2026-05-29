"""Deeper smoke: exercise the editor's actual interactions.

  - Load a figure via URL
  - Wait for SVG render
  - Click a part in the SVG to highlight it
  - Click a preset pill (Highlight)
  - Wait for auto-save to fire (PUT /api/figures/<fid>)
  - Click 'export SVG' and assert a download attempt

If any of these steps fails or fires a JS error, the test prints
where it failed.  Standalone -- run with the server up:

    python tests/smoke_editor_flow.py [proj/view/fig]
"""
from __future__ import annotations
import sys
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass
import time
import requests

SERVER = "http://127.0.0.1:5000"
DEFAULT = "281cf2da3885/5653e1b90841/decfd8366514"

CHECK = lambda ok, msg: print(f"  [{'OK' if ok else 'FAIL'}] {msg}")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    proj, view, fig = target.split("/")
    url = f"{SERVER}/#/project/{proj}/view/{view}/figure/{fig}"
    print(f"\n=== editor flow smoke ===\n{url}\n")

    seq_before = (requests.get(f"{SERVER}/api/debug/log", timeout=3)
                    .json().get("latest_seq", 0))

    from playwright.sync_api import sync_playwright
    js_errors: list[str] = []
    console: list[str] = []

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.on("console",   lambda m: console.append(
            f"{m.type}: {m.text}"))

        # ---- load + wait for SVG ----------------------------------------
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            print(f"  goto fail: {e}")
            b.close()
            return 2

        svg_ok = False
        deadline = time.time() + 90
        while time.time() < deadline:
            n = page.eval_on_selector_all(
                ".svg-pane[data-view='__live__'] svg",
                "els => els.map(el => el.outerHTML.length)")
            if n and max(n) > 1024:
                svg_ok = True
                break
            time.sleep(0.5)
        CHECK(svg_ok, "live SVG rendered")
        if not svg_ok:
            b.close()
            return 1

        # Wait for baseline to settle (auto-save baseline is set
        # 800 ms after _loadFigureIntoEditor; without this delay our
        # clicks happen before the baseline is recorded and auto-save
        # never sees a "dirty" state).
        time.sleep(2)

        # ---- assert the editor's right sidebar mounted ------------------
        sidebar = page.evaluate("""() => {
            const r = {
                drawingHeader: !!document.querySelector(
                    '[data-ed-section=\"drawing\"] h2'),
                sliderCount: document.querySelectorAll(
                    '.draw-row input[type=range]').length,
                disclosureClosed: (() => {
                    const d = document.querySelector(
                        '[data-ed-section=\"drawing\"] details.ed-disclosure');
                    return d && !d.open;
                })(),
                presetPillCount: document.querySelectorAll(
                    '#preset-row button').length,
                exportBtn: !!document.getElementById('btn-export'),
                pngBtn:    !!document.getElementById('btn-screenshot'),
                logBtn:    !!document.getElementById('btn-server-log'),
            };
            return r;
        }""")
        CHECK(sidebar["drawingHeader"], "Drawing section present")
        CHECK(sidebar["sliderCount"] >= 5,
              f"at least 5 draw sliders ({sidebar['sliderCount']})")
        CHECK(sidebar["disclosureClosed"] is True,
              "Drawing 'more' disclosure collapsed by default")
        CHECK(sidebar["presetPillCount"] >= 5,
              f"preset pills present ({sidebar['presetPillCount']})")
        CHECK(sidebar["exportBtn"],   "export SVG button present")
        CHECK(sidebar["pngBtn"],      "PNG button present")

        # ---- wait for SVG interactivity to attach + then click part ----
        # The editor's attachInteractivity wires the SVG-level click
        # handler asynchronously; without this poll we sometimes click
        # before the handler exists and the highlight never registers.
        interactive_deadline = time.time() + 15
        while time.time() < interactive_deadline:
            ok = page.evaluate("""() => {
                const svg = document.querySelector(
                    ".svg-pane[data-view='__live__'] svg");
                return !!(svg && svg.dataset.attached);
            }""")
            if ok: break
            time.sleep(0.5)

        clicked = page.evaluate("""() => {
            const part = document.querySelector(
                ".svg-pane[data-view='__live__'] svg .part[data-part]");
            if (!part) return false;
            const r = part.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top  + r.height / 2;
            const ev = new MouseEvent('click', {
                bubbles: true, clientX: cx, clientY: cy
            });
            part.dispatchEvent(ev);
            return true;
        }""")
        CHECK(clicked, "clicked a part in the SVG")

        # Wait briefly for click to register
        time.sleep(1)

        highlights_set = page.evaluate("""() => {
            const root = document.querySelector(
                ".svg-pane[data-view='__live__'] svg");
            return root
                ? root.querySelectorAll('.part.highlight').length
                : 0;
        }""")
        CHECK(highlights_set >= 1,
              f"highlight class applied ({highlights_set} parts)")

        # ---- click the Highlight preset pill ----------------------------
        preset_clicked = page.evaluate("""() => {
            const pills = document.querySelectorAll('#preset-row button');
            const target = [...pills].find(b =>
                /highlight/i.test(b.textContent || ''));
            if (!target) return false;
            target.click();
            return true;
        }""")
        CHECK(preset_clicked, "clicked Highlight preset")

        # ---- wait for auto-save to fire (PUT /api/figures/<fid>) --------
        # Debounce is 1.8 s + flush; allow generous budget.
        # Diagnostic: dump the auto-save indicator + dirty state
        dbg = page.evaluate("""() => {
            const el = document.getElementById('fig-save-status');
            // AppState is a classic-script const, not on window;
            // window.IFU_APP exposes a reference instead.
            const A = (window.IFU_APP || {}).AppState || {};
            return {
                indicator: el ? el.textContent : null,
                currentFigureId:  A.currentFigureId,
                currentProjectId: A.currentProjectId,
                stylesCount: Object.keys(
                    (window._figStyles && window._figStyles()) || {}).length,
                flushAvail:   typeof window._flushAutoSave === 'function',
            };
        }""")
        print(f"    [dbg] indicator={dbg.get('indicator')!r}")
        print(f"    [dbg] currentFigureId={dbg.get('currentFigureId')} "
              f"projectId={dbg.get('currentProjectId')} "
              f"stylesCount={dbg.get('stylesCount')}")
        # Force-flush whatever auto-save is pending so we don't depend
        # on the polling timer firing inside Playwright's frame loop.
        try:
            page.evaluate(
                "() => window._flushAutoSave && window._flushAutoSave()")
        except Exception as e:
            print(f"    [dbg] flush failed: {e}")
        save_seen = False
        deadline = time.time() + 10
        while time.time() < deadline:
            evs = (requests.get(
                f"{SERVER}/api/debug/log",
                params={"since": seq_before}, timeout=3
            ).json().get("events", []))
            puts = [e for e in evs
                    if e.get("method") == "PUT"
                    and f"/api/figures/{fig}" in (e.get("path") or "")
                    and e.get("status") == 200]
            if puts:
                save_seen = True
                break
            time.sleep(0.5)
        CHECK(save_seen, "auto-save PUT /api/figures/<fid> succeeded")

        # ---- click export SVG and check that the download was triggered
        download_seen = {"hit": False}
        page.on("download", lambda d: download_seen.update(hit=True,
                                                            name=d.suggested_filename))
        page.evaluate(
            "() => document.getElementById('btn-export').click()")
        time.sleep(2)
        CHECK(download_seen["hit"], "export SVG triggered a download")

        b.close()

    # ---- look at any JS errors during the run --------------------------
    real_errors = [e for e in js_errors
                   if 'favicon.ico' not in e]
    bad_console = [c for c in console
                   if c.startswith('error:')
                   and 'favicon' not in c
                   and 'WebGL' not in c
                   and 'GL_CLOSE_PATH' not in c
                   and '/thumbnail' not in c]
    CHECK(not real_errors,
          f"no pageerrors ({len(real_errors)})")
    CHECK(not bad_console,
          f"no console errors ({len(bad_console)})")
    if real_errors:
        print("  pageerrors:")
        for e in real_errors[:5]:
            print(f"    {e[:300]}")
    if bad_console:
        print("  console errors:")
        for c in bad_console[:5]:
            print(f"    {c[:300]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
