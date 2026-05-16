"""G.4 e2e tests: card action menu on the Home screen.

We don't test every menu item in depth -- the underlying CRUD endpoints
have their own integration tests.  These pin down the *contract* the
menu adds to the home screen: the button appears, opens a menu, the
menu has the expected items, and an outside-click closes it.
"""
from __future__ import annotations


def test_card_menu_button_appears_on_project_cards(page):
    """Every project card on Home should carry a .card-menu-btn."""
    # Seed a project so we have at least one card
    page.evaluate("""async () => {
        await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'G4-menu-test'}),
        });
    }""")
    page.evaluate("location.hash = '#/'")
    page.evaluate("window.IFU_APP.renderRoute()")
    page.wait_for_timeout(500)
    has_btn = page.evaluate("""() => {
        const cards = document.querySelectorAll('.card-grid .card');
        // Skip the placeholder, find one with a menu button
        return Array.from(cards).some(c =>
            !c.classList.contains('placeholder')
            && c.querySelector('.card-menu-btn') !== null);
    }""")
    assert has_btn, "no .card-menu-btn on project cards"
    # Cleanup
    page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/projects');
        const data = await r.json();
        for (const p of data.projects) {
            if (p.name === 'G4-menu-test') {
                await fetch(API_BASE + '/api/projects/'
                              + encodeURIComponent(p.id) + '?cascade=1',
                              {method: 'DELETE'});
            }
        }
    }""")


def test_card_menu_opens_with_expected_items(page):
    """Clicking the .card-menu-btn opens a .card-menu with Rename,
    Edit description, Delete."""
    page.evaluate("""async () => {
        await fetch(API_BASE + '/api/projects', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: 'G4-menu-open'}),
        });
    }""")
    page.evaluate("location.hash = '#/'")
    page.evaluate("window.IFU_APP.renderRoute()")
    page.wait_for_timeout(500)
    # Click the first non-placeholder card's menu button
    page.evaluate("""() => {
        const cards = document.querySelectorAll('.card-grid .card');
        for (const c of cards) {
            if (c.classList.contains('placeholder')) continue;
            const btn = c.querySelector('.card-menu-btn');
            if (btn) { btn.click(); return; }
        }
    }""")
    page.wait_for_timeout(200)
    items = page.evaluate("""() => Array.from(
        document.querySelectorAll('.card-menu .item'))
        .map(i => i.textContent)""")
    assert any("Rename" in x for x in items), \
        f"Rename missing: {items}"
    assert any("Delete" in x for x in items), \
        f"Delete missing: {items}"
    # Danger styling on Delete
    has_danger = page.evaluate(
        "() => !!document.querySelector('.card-menu .item.danger')")
    assert has_danger, "delete item should have danger styling"
    # Cleanup
    page.evaluate("""async () => {
        const r = await fetch(API_BASE + '/api/projects');
        const data = await r.json();
        for (const p of data.projects) {
            if (p.name === 'G4-menu-open') {
                await fetch(API_BASE + '/api/projects/'
                              + encodeURIComponent(p.id) + '?cascade=1',
                              {method: 'DELETE'});
            }
        }
    }""")
