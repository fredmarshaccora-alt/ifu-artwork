"""Backtest for bugs #12 + #21 (API base + silhouette fetch firing).

#12: `const API_BASE` doesn't attach to window.  All client fetchers
     that referenced `window.API_BASE` returned early -- the silhouette
     and footprint endpoints were never hit.
#21: as a consequence, the silhouette fetch never fired on shade toggle.
"""
from __future__ import annotations
import pytest


def test_api_base_resolvable(page):
    """The page must expose `API_BASE` as a string identifier that the
    fetch functions can use."""
    val = page.evaluate("typeof API_BASE")
    assert val == "string", f"API_BASE not resolvable (typeof = {val})"


def test_silhouette_fetch_fires_when_shade_on(page, server_url):
    """Bug #21: turning shade on triggers /api/part_silhouettes."""
    page.evaluate("""() => {
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(1500)
    # Listen for the network request
    fired = {"value": False}
    page.on("request",
            lambda req: fired.update(value=True)
            if "/api/part_silhouettes" in req.url else None)
    page.evaluate("""() => {
        window.togglePartHighlight(5, {append:false});
        document.getElementById('sty-fill-on').checked = true;
        document.getElementById('sty-fill-on').dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(2500)
    assert fired["value"], \
        "shade-on did not trigger /api/part_silhouettes (bug #21)"
