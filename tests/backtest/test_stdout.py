"""Backtest for bug #10 (UTF-8 stdout on Windows).

Scripts print emoji (⚡, ✓) which crash on cp1252.  Every entry-point
script must call sys.stdout.reconfigure(encoding='utf-8') or set
PYTHONIOENCODING=utf-8.  We verify the actual stdout encoding here.
"""
from __future__ import annotations
import sys


def test_stdout_is_utf8():
    """conftest forces utf-8; this confirms the protection is in place."""
    # If conftest's reconfigure failed, this would be cp1252 on Windows.
    enc = (sys.stdout.encoding or "").lower()
    assert enc in ("utf-8", "utf8"), \
        f"stdout encoding {enc!r} -- emoji print() will crash"


def test_emoji_printable():
    """Round-trip an emoji through stdout's encoding."""
    s = "⚡✓⭐"
    enc = sys.stdout.encoding or "utf-8"
    s.encode(enc)  # must not raise
