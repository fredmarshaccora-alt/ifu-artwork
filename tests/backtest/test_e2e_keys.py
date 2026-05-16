"""Backtest for the P3 keyboard shortcuts (1/2/3/R/F/Esc)."""
from __future__ import annotations
import pytest


def test_digit_switches_layout(page):
    """Pressing 1, 2, 3 with no input focused switches layout."""
    page.evaluate("""() => {
        document.body.focus();
    }""")
    page.keyboard.press("2")
    page.wait_for_timeout(150)
    cls = page.evaluate("document.body.className")
    assert "layout-split" in cls, f"expected layout-split, got {cls!r}"
    page.keyboard.press("3")
    page.wait_for_timeout(150)
    cls = page.evaluate("document.body.className")
    assert "layout-3d" in cls, f"expected layout-3d, got {cls!r}"
    page.keyboard.press("1")
    page.wait_for_timeout(150)
    cls = page.evaluate("document.body.className")
    assert "layout-2d" in cls, f"expected layout-2d, got {cls!r}"


def test_shortcut_ignored_while_input_focused(page):
    """Typing 1/2/3 INSIDE an input field must NOT switch layout."""
    page.evaluate("""() => {
        const i = document.getElementById('view-name');
        if (i) i.focus();
    }""")
    initial = page.evaluate("document.body.className")
    page.keyboard.press("2")
    page.wait_for_timeout(150)
    after = page.evaluate("document.body.className")
    assert after == initial, \
        f"keyboard shortcut fired while input focused (was {initial!r}, now {after!r})"


def test_escape_clears_selection(page):
    """Esc with the canvas focused clears any active selection."""
    page.evaluate("""() => {
        const sel = document.getElementById('file-sel');
        sel.value = 'siderail';
        sel.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(800)
    page.evaluate("window.togglePartHighlight(7, {append:false})")
    page.wait_for_timeout(200)
    page.evaluate("document.body.focus()")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    n_hi = page.evaluate("""() => {
        const svg = document.querySelector('.svg-pane.active svg');
        return svg ? svg.querySelectorAll('.part.highlight').length : -1;
    }""")
    assert n_hi == 0, f"Esc didn't clear highlights, {n_hi} still highlighted"
