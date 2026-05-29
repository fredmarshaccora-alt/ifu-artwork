"""One-time backfill: generate tile-preview thumbnails for every view
(and its primary figure) that doesn't already have one.

Reuses the in-app capture path: navigate to each thumbnail-less view,
let it render + the capture-on-open hook fire, then verify the view's
thumbnail is now present.  Items whose source can't render server-side
(e.g. a source that isn't loaded) are skipped and reported -- they'll
get a preview the next time they're opened.

Run with the server up:
    python backfill_thumbnails.py [--limit N] [--all]

  --limit N   stop after N successful backfills (default: no limit)
  --all       also re-capture views that already have a thumbnail
"""
from __future__ import annotations
import sys
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
import time
import requests

SERVER = "http://127.0.0.1:5000"
SEL = ".svg-pane[data-view='__live__'] svg"


def _has_thumb(kind, _id):
    try:
        r = requests.get(f"{SERVER}/api/{kind}/{_id}/thumbnail", timeout=5)
        return r.status_code == 200 and len(r.content) > 100
    except Exception:
        return False


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    do_all = "--all" in sys.argv

    try:
        requests.get(f"{SERVER}/api/healthz", timeout=3)
    except Exception as e:
        print(f"server not reachable: {e}"); return 2

    projects = (requests.get(f"{SERVER}/api/projects", timeout=10)
                .json().get("projects") or [])
    # Build the work list: (project_id, view_id) for views w/o a thumb.
    work = []
    skipped_have = 0
    for p in projects:
        pid = p["id"]
        try:
            views = (requests.get(
                f"{SERVER}/api/projects/{pid}/views", timeout=10)
                .json().get("views") or [])
        except Exception:
            views = []
        for v in views:
            vid = v["id"]
            if not do_all and _has_thumb("views", vid):
                skipped_have += 1
                continue
            # Navigate straight to the view's first figure (avoids the
            # view->figure redirect race that blanks the pane mid-capture).
            figs = v.get("figure_ids") or []
            first_fig = figs[0] if figs else None
            work.append((pid, vid, first_fig))

    print(f"\n=== thumbnail backfill ===")
    print(f"  projects: {len(projects)}")
    print(f"  views needing a thumbnail: {len(work)}"
          + (f"  (+{skipped_have} already have one)" if skipped_have else ""))
    if limit:
        work = work[:limit]
        print(f"  (limited to {len(work)} this run)")
    if not work:
        print("  nothing to do."); return 0

    from playwright.sync_api import sync_playwright
    done, failed = 0, 0
    failed_views = []
    t_start = time.time()
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        for i, (pid, vid, first_fig) in enumerate(work, 1):
            if first_fig:
                url = f"{SERVER}/#/project/{pid}/view/{vid}/figure/{first_fig}"
            else:
                # No figures -> nothing to render -> can't preview.
                failed += 1; failed_views.append(vid)
                print(f"  [{i}/{len(work)}] view {vid[:12]} "
                      f"SKIP (no figures)")
                continue
            ok_svg = False
            try:
                # Force a clean SPA boot per view -- a hash-only change in
                # the reused page leaves the live pane in a transitional
                # state for some figures (capture then sees an empty pane).
                page.goto("about:blank", wait_until="load", timeout=10000)
                page.goto(url, wait_until="load", timeout=120000)
                deadline = time.time() + 100   # heavy assemblies (700+ solids)
                while time.time() < deadline:
                    n = page.eval_on_selector_all(
                        SEL, "els => els.map(el => el.outerHTML.length)")
                    if n and max(n) > 1024:
                        ok_svg = True; break
                    time.sleep(0.5)
            except Exception:
                ok_svg = False

            if not ok_svg:
                failed += 1; failed_views.append(vid)
                print(f"  [{i}/{len(work)}] view {vid[:12]} "
                      f"SKIP (no render — source not loaded?)")
                continue

            # Let capture-on-open fire (debounced 1.5s); then nudge it
            # explicitly with the ids the router set, to be deterministic.
            time.sleep(2.2)
            diag = None
            try:
                # Pass the KNOWN view id explicitly -- AppState.currentViewId
                # can be null after the view->figure redirect, which would
                # skip the view thumbnail PUT.  Return a diagnostic so we
                # can see WHY a capture yields nothing.
                diag = page.evaluate("""async (vid) => {
                  const A = (window.IFU_APP && window.IFU_APP.AppState) || {};
                  const ap = (typeof activePane === 'function')
                    ? activePane() : null;
                  const durl = (window._captureFigureThumbnail)
                    ? await window._captureFigureThumbnail() : null;
                  let put = null;
                  if (durl && window._captureAndUploadAll)
                    put = await window._captureAndUploadAll(
                      A.currentFigureId, vid);
                  return {
                    fid: A.currentFigureId || null,
                    activeView: ap && ap.dataset ? ap.dataset.view : null,
                    capLen: durl ? durl.length : 0,
                    put: put,
                  };
                }""", vid)
            except Exception as e:
                diag = {"error": str(e)[:120]}
            time.sleep(1.0)

            if _has_thumb("views", vid):
                done += 1
                print(f"  [{i}/{len(work)}] view {vid[:12]} ✓")
            else:
                failed += 1; failed_views.append(vid)
                print(f"  [{i}/{len(work)}] view {vid[:12]} "
                      f"FAIL  diag={diag}")
        b.close()

    dt = time.time() - t_start
    print(f"\n  backfilled {done}, skipped/failed {failed}, "
          f"in {dt:.0f}s")
    if failed_views:
        print(f"  not backfilled (will fill on next open): "
              f"{len(failed_views)} views")
    return 0


if __name__ == "__main__":
    sys.exit(main())
