"""Backend smoke: load a figure URL via Playwright headless, watch
the server log, and assert that:
  - no client-side JS error fires (via /api/debug/client_log)
  - POST /api/render is invoked
  - the live <svg> ends up in the DOM with > 1 KB of content

Standalone script (not pytest-collected) so it's quick to invoke and
the verdict is loud.  Run while the server is up:

    python tests/smoke_figure_render.py <project_id>/<view_id>/<figure_id>

Defaults match the user's running figure if no arg given.
"""
from __future__ import annotations
import sys
import time
import requests
from urllib.parse import quote


SERVER = "http://127.0.0.1:5000"
DEFAULT_TARGET = "281cf2da3885/5653e1b90841/decfd8366514"

CHECK = lambda ok, msg: print(f"  [{'OK' if ok else 'FAIL'}] {msg}")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    proj, view, fig = target.split("/")
    url = (f"{SERVER}/#/project/{quote(proj)}/view/{quote(view)}"
           f"/figure/{quote(fig)}")
    print(f"\n=== smoke test ===\nURL: {url}")

    # Confirm the server is up
    try:
        r = requests.get(f"{SERVER}/api/healthz", timeout=3)
        assert r.status_code == 200
    except Exception as e:
        print(f"  [FAIL] server not reachable: {e}")
        return 2

    # Snapshot the log's latest_seq so we only count NEW events
    try:
        seq_before = requests.get(f"{SERVER}/api/debug/log",
                                    timeout=3).json().get("latest_seq", 0)
    except Exception as e:
        print(f"  [FAIL] couldn't read log: {e}")
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [FAIL] playwright not installed; pip install playwright")
        return 2

    js_console: list[str] = []
    js_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: js_console.append(
            f"  [console {m.type}] {m.text}"))
        page.on("pageerror", lambda e: js_errors.append(
            f"  [pageerror] {e}"))

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            print(f"  [FAIL] page.goto: {e}")
            browser.close()
            return 2

        # Give the editor up to 120 s to render the SVG.  Chair-class
        # assemblies take 30-60 s on a cold first render; subsequent
        # ones hit the cache in < 1 s.
        svg_seen = False
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                # The live SVG ends up inside .svg-pane[data-view=__live__]
                content = page.eval_on_selector_all(
                    ".svg-pane[data-view='__live__'] svg",
                    "els => els.map(el => el.outerHTML.length)"
                )
                if content and max(content) > 1024:
                    svg_seen = True
                    print(f"\n  [OK] live SVG present, "
                          f"max size {max(content)} bytes")
                    break
            except Exception:
                pass
            time.sleep(0.5)

        # Capture the final DOM state for diagnostics
        try:
            url_after = page.url
            has_app_root = page.eval_on_selector(
                "#app-root", "el => !!el && el.children.length > 0")
            live_panes = page.eval_on_selector_all(
                ".svg-pane[data-view='__live__']",
                "els => els.length")
        except Exception:
            url_after = "?"
            has_app_root = "?"
            live_panes = "?"

        browser.close()

    # Pull the server log since we started
    try:
        log = requests.get(f"{SERVER}/api/debug/log",
                            params={"since": seq_before},
                            timeout=3).json()
        events = log.get("events", [])
    except Exception as e:
        print(f"  [FAIL] log fetch: {e}")
        return 2

    # Inspect what happened server-side
    render_posts = [e for e in events
                    if e.get("method") == "POST"
                    and e.get("path") == "/api/render"
                    and e.get("status") == 200]
    client_errors = [e for e in events
                     if e.get("op") in ("window.error",
                                         "unhandledrejection")]
    err_paths = [e for e in events if e.get("level") == "err"
                 and e.get("path", "").startswith("/api/")
                 and not e.get("path", "").endswith("/thumbnail")]

    print("\n=== verdict ===")
    CHECK(svg_seen,            "live SVG appears in DOM")
    CHECK(len(render_posts) >= 1,
                                f"POST /api/render fired ({len(render_posts)})")
    CHECK(len(client_errors) == 0,
                                f"no JS-side errors ({len(client_errors)})")
    CHECK(len(err_paths) == 0,
                                f"no server API errors ({len(err_paths)})")
    print(f"  url_after:    {url_after}")
    print(f"  app_root_ok:  {has_app_root}")
    print(f"  live_panes:   {live_panes}")

    if client_errors:
        print("\nclient errors:")
        for e in client_errors[:5]:
            print(f"  - {e.get('op')}: {e.get('msg','')[:200]}")
            stack = e.get("stack", "")
            if stack:
                for ln in stack.split("\n")[:5]:
                    print(f"      {ln.strip()}")
    if err_paths:
        print("\nserver errors:")
        for e in err_paths[:5]:
            print(f"  - {e.get('method')} {e.get('path')} "
                  f"-> {e.get('status')} {e.get('error','')[:120]}")

    if js_errors:
        print("\nplaywright pageerrors:")
        for ln in js_errors[:5]:
            print(ln)
    if js_console:
        print("\nplaywright console (first 30):")
        for ln in js_console[:30]:
            print(ln)

    ok = svg_seen and len(render_posts) >= 1 and len(client_errors) == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
