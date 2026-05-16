"""F.6 Settings screen smoke tests."""
from __future__ import annotations


def test_settings_screen_loads(page):
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_timeout(400)
    info = page.evaluate("""() => ({
        h1: document.querySelector('.app-topbar .crumbs .current')?.textContent,
        has_detail: !!document.querySelector('.app-main select'),
        h2_count: document.querySelectorAll('.section-title').length,
    })""")
    assert info["h1"] and "Settings" in info["h1"]
    assert info["has_detail"], "no detail-level select"
    assert info["h2_count"] >= 3, \
        f"expected >= 3 section headings, got {info['h2_count']}"


def test_changing_default_detail_persists(page):
    """Pick a different detail level; reload settings; the value sticks."""
    # Reset first for a clean baseline
    page.evaluate("""async () => {
        await fetch(API_BASE + '/api/settings/reset', {method: 'POST'});
    }""")
    page.evaluate("location.hash = '#/settings'")
    page.evaluate("window.IFU_APP.renderRoute()")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
        const sel = document.querySelector('.app-main select');
        sel.value = 'fine';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(400)
    server_value = page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/settings');
        const s = await r.json();
        return s.default_detail;
    }""")
    assert server_value == "fine", \
        f"server default_detail should be 'fine', got {server_value!r}"
    # Restore default
    page.evaluate("""async () => {
        await fetch(API_BASE + '/api/settings/reset', {method: 'POST'});
    }""")
