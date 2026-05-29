"""UI audit: visit every route via headless Playwright, capture
screenshots + DOM snapshots, list every bug found.

Run while the server is up:
    python tests/ui_audit.py [out_dir]

Outputs:
    audit/<route_name>.png          one screenshot per screen
    audit/<route_name>.json         DOM structural summary
    audit/report.txt                human-readable findings
"""
from __future__ import annotations
import json
import os
import sys
import time
import shutil
from pathlib import Path

import requests


SERVER = "http://127.0.0.1:5000"


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1
                    else Path(__file__).parent / "audit")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clean files (not dir) so OneDrive doesn't fight us
    for p in out_dir.iterdir():
        try: p.unlink()
        except OSError: pass

    # Probe for live data we can navigate into.
    try:
        projects = (requests.get(f"{SERVER}/api/projects",
                                   timeout=3).json()
                    .get("projects") or [])
        first_pid = projects[0]["id"] if projects else None
    except Exception as e:
        print(f"server probe failed: {e}")
        return 2
    try:
        if first_pid:
            views = (requests.get(
                f"{SERVER}/api/projects/{first_pid}/views",
                timeout=3).json().get("views") or [])
            first_vid = views[0]["id"] if views else None
        else:
            first_vid = None
    except Exception:
        first_vid = None

    try:
        if first_vid:
            figs = (requests.get(
                f"{SERVER}/api/views/{first_vid}/figures",
                timeout=3).json().get("figures") or [])
            first_fid = figs[0]["id"] if figs else None
        else:
            first_fid = None
    except Exception:
        first_fid = None

    routes = [
        ("home",      "#/"),
        ("settings",  "#/settings"),
    ]
    if first_pid:
        routes.append(("project", f"#/project/{first_pid}"))
    if first_pid and first_vid:
        routes.append((
            "view", f"#/project/{first_pid}/view/{first_vid}"))
    if first_pid and first_vid and first_fid:
        routes.append((
            "editor",
            f"#/project/{first_pid}/view/{first_vid}"
            f"/figure/{first_fid}"))

    from playwright.sync_api import sync_playwright
    findings = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # Wider viewport so the editor (left-strip + 2D + 3D + right-
        # sidebar) doesn't get H-scrolled and produce an unreadable
        # screenshot.
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        console_msgs: list[str] = []
        page_errors:  list[str] = []
        # Include the source URL in the console message text so the
        # later filter can drop expected 404s (thumbnails) accurately
        # even though Chromium's "Failed to load resource" message
        # itself doesn't include the URL.
        def _on_console(m):
            loc = ""
            try:
                lo = m.location
                if isinstance(lo, dict) and lo.get("url"):
                    loc = " @" + str(lo["url"])
            except Exception:
                pass
            console_msgs.append(f"{m.type}: {m.text}{loc}")
        page.on("console",   _on_console)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        for name, hash_ in routes:
            console_msgs.clear()
            page_errors.clear()

            url = f"{SERVER}/{hash_}"
            print(f"\n=== {name} === {url}")
            try:
                page.goto(url, wait_until="domcontentloaded",
                           timeout=15000)
                # Wait for either app-root or main to settle
                time.sleep(2)
            except Exception as e:
                findings.append(f"[{name}] page.goto failed: {e}")
                continue

            # Capture screenshot — viewport only for screens where
            # the legacy editor's wide horizontal layout would crush
            # the full-page screenshot into an unreadable thumbnail.
            shot = out_dir / f"{name}.png"
            try:
                full = name in ("home", "project", "settings")
                page.screenshot(path=str(shot), full_page=full)
                print(f"  saved {shot}")
            except Exception as e:
                findings.append(f"[{name}] screenshot failed: {e}")

            # Structural snapshot
            try:
                snapshot = page.evaluate("""() => {
                    const $$ = sel => document.querySelectorAll(sel);
                    const appRoot = document.getElementById('app-root');
                    const header = document.querySelector('header');
                    const main = document.querySelector('main');
                    const visible = el => {
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        return s.display !== 'none'
                            && s.visibility !== 'hidden'
                            && el.offsetParent !== null;
                    };
                    const findEmptyContainers = root => {
                        if (!root) return 0;
                        let n = 0;
                        root.querySelectorAll('section, .card, .ed-section')
                          .forEach(el => {
                            if (visible(el)
                                && el.textContent.trim() === ''
                                && el.children.length === 0) n++;
                          });
                        return n;
                    };
                    const buttons = [...$$('button')].filter(visible);
                    return {
                        url:        location.href,
                        hash:       location.hash,
                        title:      document.title,
                        appRoot:    {
                            visible:        visible(appRoot),
                            children:       appRoot ? appRoot.children.length : 0,
                            textPreview:    appRoot
                                ? appRoot.textContent.trim().slice(0, 200)
                                : '',
                        },
                        header:     {
                            visible:   visible(header),
                            html:      header
                                ? header.innerHTML.length : 0,
                        },
                        main:       {
                            visible:   visible(main),
                            html:      main ? main.innerHTML.length : 0,
                        },
                        legacyEditor: {
                            buttons:      [...$$('button')].length,
                            visibleBtns:  buttons.length,
                            selectsCount: [...$$('select')].length,
                            inputsCount:  [...$$('input')].length,
                            sectionsTotal:    [...$$('.ed-section')].length,
                            sectionsVisible:  [...$$('.ed-section')]
                                .filter(visible).length,
                        },
                        cardCount:    [...$$('.card')].length,
                        emptyContainers: findEmptyContainers(document),
                        hasErrorCard: [...document.querySelectorAll(
                            'div, p')].some(e =>
                            /could.?n.?t load|not found|something went wrong/i
                              .test(e.textContent || '')),
                        // Visible buttons -> label text, for catalog
                        visibleButtonLabels: buttons
                            .map(b => (b.textContent || '').trim())
                            .filter(t => t.length > 0)
                            .slice(0, 40),
                        // Detect overflow / scrollbars
                        hasHScroll: document.documentElement.scrollWidth
                            > document.documentElement.clientWidth,
                    };
                }""")
            except Exception as e:
                findings.append(f"[{name}] DOM snapshot failed: {e}")
                snapshot = None

            snap_path = out_dir / f"{name}.json"
            if snapshot is not None:
                snap_path.write_text(
                    json.dumps({
                        "snapshot":    snapshot,
                        "console":     console_msgs[:60],
                        "pageErrors":  page_errors[:10],
                    }, indent=2),
                    encoding="utf-8")
                # Pull immediate findings
                # The 'view' route auto-redirects into the editor for
                # the view's first figure -- the legacy editor's main
                # is then visible, app-root hidden.  That's by design,
                # so check whether main is visible instead.
                if not snapshot.get("appRoot", {}).get("visible") \
                        and not snapshot.get("main", {}).get("visible") \
                        and name not in ("editor", "view"):
                    findings.append(
                        f"[{name}] neither app-root nor main visible "
                        f"(route didn't mount?)")
                if snapshot.get("hasErrorCard"):
                    findings.append(f"[{name}] error card on screen")
                if snapshot.get("hasHScroll"):
                    findings.append(f"[{name}] horizontal scrollbar")
                if snapshot.get("emptyContainers", 0) > 0:
                    findings.append(
                        f"[{name}] {snapshot['emptyContainers']} empty "
                        f"container(s)")
                if name == "editor":
                    # The legacy editor lives in <main>; should show
                    if not snapshot.get("main", {}).get("visible"):
                        findings.append(
                            "[editor] legacy <main> not visible "
                            "(figure didn't mount?)")
                page_errors_clean = [e for e in page_errors
                                      if 'favicon.ico' not in e]
                if page_errors_clean:
                    findings.append(
                        f"[{name}] {len(page_errors_clean)} page error(s): "
                        f"{page_errors_clean[0][:200]}")
                bad_console = [c for c in console_msgs
                               if c.startswith('error:')
                               and 'favicon.ico' not in c
                               and 'WebGL' not in c
                               and 'GL_CLOSE_PATH' not in c
                               # Thumbnail 404s are expected when a
                               # view hasn't been rendered yet; the JS
                               # already falls back to a monogram.
                               and '/thumbnail' not in c]
                if bad_console:
                    findings.append(
                        f"[{name}] console error: {bad_console[0][:200]}")
                # listeners reset implicitly when we move to next URL

        browser.close()

    # Write the report
    report = out_dir / "report.txt"
    if not findings:
        report.write_text("no findings\n", encoding="utf-8")
        print("\nNO FINDINGS")
    else:
        with open(report, "w", encoding="utf-8") as f:
            f.write("UI audit findings\n")
            f.write("=" * 50 + "\n\n")
            for line in findings:
                f.write(f"- {line}\n")
                print(f"  FINDING: {line}")
        print(f"\n{len(findings)} findings -> {report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
