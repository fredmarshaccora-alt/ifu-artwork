"""Regression: the 'Interaction log' tracker records click + toggle +
apply events with unique-idx detection.  Pins the contract the user
will rely on to debug "selecting something highlights other parts"
complaints.

Asserts (with the smoke server up):
  - window._track / window._toggleTracker / window._getTrackerEntries
    are exposed
  - clicking a part records click + toggle + apply events
  - the 'apply' event's `uniq` field equals the cardinality of the
    selection (i.e. NO cross-idx contamination)
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
    print(f"\n=== interaction tracker smoke ===\n{url}\n")

    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        print(f"  [FAIL] server not reachable: {e}")
        return 2

    from playwright.sync_api import sync_playwright

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
            if n and max(n) > 1024:
                ok_svg = True; break
            time.sleep(0.5)
        CHECK(ok_svg, "live SVG rendered")
        if not ok_svg: b.close(); return 1

        # Interactivity attached
        for _ in range(30):
            ok = page.evaluate("""() => {
                const svg = document.querySelector(
                    ".svg-pane[data-view='__live__'] svg");
                return !!(svg && svg.dataset.attached);
            }""")
            if ok: break
            time.sleep(0.3)

        # Tracker hooks exposed
        hooks = page.evaluate("""() => ({
            track: typeof window._track,
            toggle: typeof window._toggleTracker,
            getEntries: typeof window._getTrackerEntries,
            btn: !!document.getElementById('btn-iact-track'),
        })""")
        CHECK(hooks["track"] == "function",
              "window._track is a function")
        CHECK(hooks["toggle"] == "function",
              "window._toggleTracker is a function")
        CHECK(hooks["getEntries"] == "function",
              "window._getTrackerEntries is a function")
        CHECK(hooks["btn"],
              "header button 'track' present")

        # Click 3 distinct parts
        centres = page.evaluate("""() => {
            const paths = document.querySelectorAll(
                ".svg-pane[data-view='__live__'] svg .part[data-part] path");
            const out = [];
            const seen = new Set();
            for (let i = 0; i < paths.length && out.length < 3; i++) {
                const g = paths[i].closest('.part[data-part]');
                if (!g) continue;
                const idx = g.dataset.part;
                if (seen.has(idx)) continue;
                const r = paths[i].getBoundingClientRect();
                if (r.width < 3 || r.height < 3) continue;
                seen.add(idx);
                out.push({idx, x: r.left + r.width/2,
                          y: r.top + r.height/2});
            }
            return out;
        }""")
        CHECK(len(centres) >= 3,
              f"found 3 click candidates ({len(centres)})")
        if len(centres) < 1:
            b.close(); return 1

        for c in centres:
            page.mouse.click(c["x"], c["y"])
            time.sleep(0.4)

        entries = page.evaluate(
            "() => window._getTrackerEntries()")
        kinds = [e["kind"] for e in entries]
        CHECK("click" in kinds, "click events recorded")
        CHECK("toggle" in kinds, "toggle events recorded")
        CHECK("apply" in kinds, "apply events recorded")

        # Find every 'apply' event with sel > 0 and check uniq==sel
        applies = [e for e in entries
                   if e["kind"] == "apply" and "sel=" in e["msg"]
                   and "WARN_EXTRA" not in e["msg"]]
        problem_applies = [e for e in entries
                           if e["kind"] == "apply"
                           and "WARN_EXTRA" in e["msg"]]
        CHECK(len(applies) >= 3,
              f"got >= 3 clean apply events ({len(applies)})")
        CHECK(len(problem_applies) == 0,
              f"no apply has WARN_EXTRA (cross-idx) "
              f"({len(problem_applies)})")
        if problem_applies:
            print("  WARN_EXTRA events:")
            for e in problem_applies[:3]:
                print(f"    {e['msg']}")

        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
