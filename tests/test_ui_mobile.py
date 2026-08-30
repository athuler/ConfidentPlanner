"""Responsive layout checks in a real (headless) browser.

Needs a running server and Playwright:  CP_BASE_URL=http://127.0.0.1:5000 python -m pytest tests/test_ui_mobile.py
Skipped otherwise, so the default `pytest tests` stays server-free.
"""
from __future__ import annotations

import os

import pytest

BASE = os.environ.get("CP_BASE_URL")
playwright = pytest.importorskip("playwright.sync_api") if BASE else None
pytestmark = pytest.mark.skipif(not BASE, reason="set CP_BASE_URL to run browser tests")


def _box(page, sel):
    return page.evaluate(
        "s => { const e = document.querySelector(s); const b = e.getBoundingClientRect();"
        " return {x: b.x, y: b.y, w: b.width, h: b.height, bottom: b.bottom, hidden: e.hidden, display: getComputedStyle(e).display}; }",
        sel,
    )


def _ready(page):
    page.goto(BASE)
    page.wait_for_function("document.querySelectorAll('#months input').length == 12")
    page.wait_for_selector("#loading", state="hidden")
    page.wait_for_timeout(1500)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def test_phone_layout(browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    _ready(page)
    assert _box(page, "#map")["w"] == 390, "map must fill the phone width"
    assert page.evaluate("document.documentElement.scrollWidth") == 390, "no horizontal page scroll"
    sb = _box(page, "#sidebar")
    assert sb["w"] == 390 and sb["y"] >= 844 * 0.7, f"drawer should be collapsed to a peek bar, got {sb}"
    page.tap("#mobile-bar")
    page.wait_for_timeout(400)
    assert _box(page, "#sidebar")["y"] <= 844 * 0.2, "drawer should slide up"
    page.tap("#mobile-bar")
    page.wait_for_timeout(400)

    x, y = page.evaluate(
        "(() => { const l = boroughLayer.getLayers().find(l => l.feature.properties.name === 'Camden');"
        " const p = map.latLngToContainerPoint(l.getBounds().getCenter()); return [p.x, p.y]; })()"
    )
    page.touchscreen.tap(x, y)
    page.wait_for_selector("#loading", state="hidden")
    page.wait_for_timeout(800)
    assert page.evaluate("currentBorough") == "Camden"
    sheet = _box(page, "#point-box")
    # bottom sheet: full width, sitting on the 56 px peek bar, at most half the screen
    assert not sheet["hidden"] and sheet["w"] == 390 and abs(sheet["bottom"] - (844 - 56)) < 1, f"details should be a bottom sheet, got {sheet}"
    assert sheet["h"] <= 844 * 0.5 + 1
    assert not _box(page, "#map-back")["hidden"] and _box(page, "#map-back")["display"] != "none"
    assert not page.evaluate("document.body.classList.contains('drawer-open')")

    page.tap("#map-back")
    page.wait_for_selector("#loading", state="hidden")
    page.wait_for_timeout(600)
    assert page.evaluate("currentBorough") is None
    assert _box(page, "#map-back")["hidden"] and _box(page, "#point-box")["hidden"]
    assert not errors, errors
    ctx.close()


def test_desktop_layout_unchanged(browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    _ready(page)
    sb, mp = _box(page, "#sidebar"), _box(page, "#map")
    assert sb["x"] == 0 and sb["w"] == 340 and mp["x"] == 340 and mp["w"] == 1060
    assert _box(page, "#mobile-bar")["display"] == "none"
    assert _box(page, "#map-back")["display"] == "none"
    page.close()
