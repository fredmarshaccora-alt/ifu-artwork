"""Shared fixtures for the backtest harness.

The harness has three tiers of test:

  unit         -- pure-Python, no STEP files, no server.  Run in
                  milliseconds; gate every push.
  integration  -- loads at least one STEP file, may run the actual
                  HLR pipeline.  Tens of seconds; gate every push.
  e2e          -- needs the Flask server running on http://127.0.0.1:5000
                  AND a Playwright browser.  Tens of minutes total;
                  gate on PR + main.

Mark tests with @pytest.mark.unit/integration/e2e so CI can pick the
right tier.  Tests that need a STEP file should pull it through the
`siderail_step` / `presto_step` / `contesa_step` fixtures, which skip
gracefully if the file isn't on disk.
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Pre-flight: force UTF-8 stdout so emoji in test names doesn't crash
# Windows cp1252 (bug #10 -- protected from regressing).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def pytest_collection_modifyitems(config, items):
    """Auto-mark tests based on file naming so we don't have to repeat
    the @pytest.mark.X dance everywhere."""
    for item in items:
        path = str(item.fspath)
        if "test_e2e_" in path:
            item.add_marker(pytest.mark.e2e)
        elif "test_integration_" in path or "test_hlr" in path \
                or "test_api_" in path or "test_tagging" in path \
                or "test_footprint" in path or "test_step_tree" in path:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)


# ---- STEP file fixtures -------------------------------------------------

@pytest.fixture(scope="session")
def step_root() -> Path:
    """Where to look for STEP files.  We use the same paths the prototype
    SOURCES list uses; integration tests skip if they're absent."""
    return ROOT


@pytest.fixture(scope="session")
def siderail_step(step_root) -> Path:
    p = Path(r"C:\Users\FredMarshAccora\Downloads\P194-03-00 Folding siderail ASSE.STEP")
    if not p.exists():
        pytest.skip(f"siderail STEP not at {p}")
    return p


@pytest.fixture(scope="session")
def contesa_step(step_root) -> Path:
    p = step_root / "contesa_top_level.step"
    if not p.exists():
        pytest.skip(f"contesa STEP not at {p}")
    return p


@pytest.fixture(scope="session")
def presto_step(step_root) -> Path:
    p = step_root.parent / "step_lineart_test" / "presto_top_level.step"
    if not p.exists():
        pytest.skip(f"presto STEP not at {p}")
    return p


# ---- Server fixture -----------------------------------------------------

@pytest.fixture(scope="session")
def server_url() -> str:
    """E2E + integration tests assume the Flask server is up locally.
    Set IFU_SERVER=http://host:port to override, or skip when unreachable."""
    url = os.environ.get("IFU_SERVER", "http://127.0.0.1:5000")
    try:
        import urllib.request
        urllib.request.urlopen(url + "/api/healthz", timeout=3).read()
    except Exception as exc:
        pytest.skip(f"server not reachable at {url}: {exc}")
    return url


# ---- Playwright fixture (for E2E) --------------------------------------

@pytest.fixture(scope="session")
def playwright_browser():
    """One browser instance per test session.  Skip when Playwright isn't
    installed -- E2E tests are gated separately."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture
def page(playwright_browser, server_url):
    """Per-test page (clean context, no cookie sharing)."""
    ctx = playwright_browser.new_context(
        viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    pg.goto(server_url + "/?dbg=1", timeout=60_000)
    pg.wait_for_function(
        "document.querySelector('#file-sel') && "
        "document.querySelector('#file-sel').options.length > 0",
        timeout=60_000)
    yield pg
    ctx.close()
