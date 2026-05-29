"""Regression: creating a new variant must NOT inherit the previous
variant's per-part styles.

The previous behaviour was: per-part styles live in localStorage
keyed by `source_id`, not by figure_id.  When the user created a new
variant the server returned a clean record (empty styles_per_part),
but the client's _loadFigureIntoEditor skipped clearing localStorage,
so the old variant's styles persisted on screen.

Setup:
  1. Apply styles on the seed figure
  2. Click '+ new highlight variant' card
  3. Wait for editor to mount on the new figure
  4. Assert: localStorage's partStyles_<source> + the on-disk figure
     record both show zero styles
  5. Teardown: delete the variant we created

Run with the server up:
    python tests/smoke_variant_carryover.py [proj/view/fig]
"""
from __future__ import annotations
import sys
try: sys.stdout.reconfigure(encoding="utf-8")
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
    print(f"\n=== variant carry-over smoke ===\n{url}\n")

    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        print(f"  [FAIL] server not reachable: {e}"); return 2

    from playwright.sync_api import sync_playwright

    new_fig_id = None
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.goto(url, wait_until="load", timeout=60000)

        # SVG ready
        ok_svg = False
        for _ in range(120):
            n = page.eval_on_selector_all(
                ".svg-pane[data-view='__live__'] svg",
                "els => els.map(el => el.outerHTML.length)")
            if n and max(n) > 1024: ok_svg = True; break
            time.sleep(0.5)
        CHECK(ok_svg, "live SVG rendered")
        if not ok_svg: b.close(); return 1

        for _ in range(30):
            ok = page.evaluate("""() => {
                const svg = document.querySelector(
                    ".svg-pane[data-view='__live__'] svg");
                return !!(svg && svg.dataset.attached);
            }""")
            if ok: break
            time.sleep(0.3)
        time.sleep(2)   # let baseline settle

        # ---- 1) APPLY a style on the current variant ------------------
        # Pick a part, click it, click the Highlight preset.
        clicked = page.evaluate("""() => {
            const paths = document.querySelectorAll(
                ".svg-pane[data-view='__live__'] svg .part[data-part] path");
            for (const p of paths) {
                const r = p.getBoundingClientRect();
                if (r.width > 5 && r.height > 5) {
                    p.closest('.part[data-part]').dispatchEvent(
                        new MouseEvent('click', {bubbles: true,
                            clientX: r.left+r.width/2,
                            clientY: r.top+r.height/2}));
                    return true;
                }
            }
            return false;
        }""")
        CHECK(clicked, "clicked a part on the seed variant")
        time.sleep(0.5)
        preset_clicked = page.evaluate("""() => {
            const pills = document.querySelectorAll('#preset-row button');
            const t = [...pills].find(b =>
                /highlight/i.test(b.textContent || ''));
            if (!t) return false;
            t.click();
            return true;
        }""")
        CHECK(preset_clicked, "clicked Highlight preset on seed variant")
        time.sleep(1)
        # Force-flush auto-save so the seed figure's record is updated.
        page.evaluate(
            "() => window._flushAutoSave && window._flushAutoSave()")
        time.sleep(1)

        # Confirm styles ARE applied on the seed figure
        seed_styles = page.evaluate("""() => {
            const m = window._figStyles();
            return Object.keys(m).length;
        }""")
        CHECK(seed_styles >= 1,
              f"seed figure has at least 1 style ({seed_styles})")

        # ---- 2) CLICK '+ new highlight variant' -----------------------
        # Trigger via the variant strip add-card.  Then wait for the
        # hash to navigate to the new figure.
        old_hash = page.evaluate("() => location.hash")
        ok = page.evaluate("""() => {
            const add = document.querySelector('#variants-strip .variant-card.add');
            if (!add) return false;
            add.click();
            return true;
        }""")
        CHECK(ok, "clicked '+ new highlight variant'")

        # Wait for navigation (URL hash change to a different fig id)
        new_fid = None
        for _ in range(40):
            h = page.evaluate("() => location.hash")
            if h != old_hash and "/figure/" in h:
                # Parse figure id out of the URL
                new_fid = h.split("/figure/")[-1].split("/")[0]
                if new_fid and new_fid != fig:
                    break
            time.sleep(0.5)
        CHECK(new_fid and new_fid != fig,
              f"navigated to new variant (fid={new_fid})")
        new_fig_id = new_fid

        # Wait for new variant's SVG
        for _ in range(120):
            n = page.eval_on_selector_all(
                ".svg-pane[data-view='__live__'] svg",
                "els => els.map(el => el.outerHTML.length)")
            if n and max(n) > 1024: break
            time.sleep(0.5)
        time.sleep(2)   # baseline settle

        # ---- 3) ASSERT new variant has NO styles ----------------------
        new_styles_local = page.evaluate("""() => {
            const m = window._figStyles();
            return Object.keys(m).length;
        }""")
        CHECK(new_styles_local == 0,
              f"new variant has zero per-part styles in localStorage "
              f"({new_styles_local})")

        # On-disk record
        if new_fid:
            try:
                rec = requests.get(
                    f"{SERVER}/api/figures/{new_fid}",
                    timeout=3).json()
                disk_styles = len(rec.get("styles_per_part") or {})
                CHECK(disk_styles == 0,
                      f"new variant has zero styles on disk "
                      f"({disk_styles})")
            except Exception as e:
                print(f"  [FAIL] disk-record fetch: {e}")

        # ---- 4) Confirm the SEED variant's styles are untouched -------
        # Hop back to the original figure URL.
        page.goto(url, wait_until="load", timeout=30000)
        for _ in range(120):
            n = page.eval_on_selector_all(
                ".svg-pane[data-view='__live__'] svg",
                "els => els.map(el => el.outerHTML.length)")
            if n and max(n) > 1024: break
            time.sleep(0.5)
        time.sleep(2)
        seed_after = page.evaluate("""() => {
            const m = window._figStyles();
            return Object.keys(m).length;
        }""")
        CHECK(seed_after >= 1,
              f"seed variant's styles preserved on round trip "
              f"({seed_after})")

        b.close()

    # ---- 5) teardown: delete the variant we created -------------------
    if new_fig_id:
        try:
            requests.delete(f"{SERVER}/api/figures/{new_fig_id}", timeout=5)
            print(f"  cleanup: deleted variant {new_fig_id}")
        except Exception as e:
            print(f"  cleanup failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
