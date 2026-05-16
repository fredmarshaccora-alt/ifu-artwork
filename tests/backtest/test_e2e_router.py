"""F.2 router infrastructure smoke tests.

Verifies the hash router + AppState + h() helper load cleanly and
behave correctly for the cases the screen modules will rely on.

The actual SCREENS (Home/Project/Editor/Settings) ship in F.3+; this
suite just protects the foundation.
"""
from __future__ import annotations


def test_app_shell_exposes_router_api(page):
    """window.IFU_APP exposes AppState, h, registerRoute, renderRoute."""
    info = page.evaluate("""() => ({
        has_appstate: !!(window.IFU_APP && window.IFU_APP.AppState),
        has_h: typeof (window.IFU_APP && window.IFU_APP.h) === 'function',
        has_register: typeof (window.IFU_APP && window.IFU_APP.registerRoute) === 'function',
        has_render: typeof (window.IFU_APP && window.IFU_APP.renderRoute) === 'function',
    })""")
    assert info["has_appstate"], "AppState missing"
    assert info["has_h"], "h() helper missing"
    assert info["has_register"], "registerRoute missing"
    assert info["has_render"], "renderRoute missing"


def test_empty_hash_shows_legacy_editor(page):
    """No hash falls through to the legacy header+main.  The Home screen
    is opt-in via the logo link (or explicit '#/' URL).  Once the
    editor is fully migrated, we'll flip the default."""
    page.evaluate("location.hash = ''")
    page.wait_for_timeout(300)
    state = page.evaluate("""() => ({
        header_visible: getComputedStyle(document.querySelector('header')).display !== 'none',
        main_visible: getComputedStyle(document.querySelector('main')).display !== 'none',
        app_root_visible: getComputedStyle(document.getElementById('app-root')).display !== 'none',
    })""")
    assert state["header_visible"]
    assert state["main_visible"]
    assert not state["app_root_visible"]


def test_unknown_route_shows_stub_and_hides_legacy(page):
    """A hash that doesn't match any registered route shows the
    "unknown route" placeholder so we don't corrupt state."""
    page.evaluate("location.hash = '#/no-such-route'")
    page.wait_for_timeout(150)
    state = page.evaluate("""() => ({
        header_visible: getComputedStyle(document.querySelector('header')).display !== 'none',
        app_text: document.getElementById('app-root').textContent,
    })""")
    assert not state["header_visible"], "legacy header should be hidden for unknown route"
    assert "Unknown route" in state["app_text"], \
        f"expected 'Unknown route' stub, got {state['app_text']!r}"


def test_h_helper_creates_element_with_class_and_id(page):
    """h('div.card#hello') -> <div id='hello' class='card'>."""
    info = page.evaluate("""() => {
        const el = window.IFU_APP.h('div.card#hello');
        return { tag: el.tagName.toLowerCase(),
                  id: el.id, cls: el.getAttribute('class') };
    }""")
    assert info["tag"] == "div"
    assert info["id"] == "hello"
    assert info["cls"] == "card"


def test_register_route_mounts_screen(page):
    """Register a route at runtime, navigate to it, verify the mount
    function got called with the matched params."""
    out = page.evaluate("""() => {
        let called = null;
        window.IFU_APP.registerRoute(
            /^#\\/test-route\\/([^/]+)$/,
            (container, params) => {
                called = params;
                container.appendChild(document.createTextNode('MOUNTED:' + params[0]));
            });
        location.hash = '#/test-route/hello';
        window.IFU_APP.renderRoute();
        return {
            params: called,
            text: document.getElementById('app-root').textContent,
        };
    }""")
    assert out["params"] == ["hello"], f"expected param 'hello', got {out['params']}"
    assert "MOUNTED:hello" in out["text"]
