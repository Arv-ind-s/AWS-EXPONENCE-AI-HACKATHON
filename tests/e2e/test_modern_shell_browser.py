"""Real-browser smoke coverage for the responsive enterprise shell."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, sync_playwright

from tests.a11y._screens import _queue_rest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "src" / "covenant_radar" / "web" / "static"
ORIGIN = "http://assets.test"
LIVE_UPDATES_PATH = "/live/updates"
CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)


def _browser_executable() -> Path:
    executable = next((path for path in CHROME_CANDIDATES if path.is_file()), None)
    if executable is None:
        pytest.skip("A local Chromium browser is required for shell smoke coverage.")
    return executable


def _install_asset_host(page: Page, html: str) -> None:
    def respond(route: Route) -> None:
        path = urlparse(route.request.url).path
        if path == "/":
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                headers={
                    "Content-Security-Policy": (
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "font-src 'self'; img-src 'self' data:; connect-src 'self'"
                    )
                },
                body=html,
            )
            return
        if path.startswith("/static/"):
            relative = path.removeprefix("/static/")
            asset = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() in asset.parents and asset.is_file():
                media_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                route.fulfill(status=200, content_type=media_type, body=asset.read_bytes())
                return
        if path == LIVE_UPDATES_PATH:
            # The shell renders with the live workspace enabled, so its script
            # polls this endpoint as soon as the page loads.  This host serves
            # a statically rendered page, not the application, so it answers
            # with the real endpoint's quiet envelope; 404ing it made the
            # browser log a resource error that has nothing to do with the
            # shell layout this test is about.
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={"Cache-Control": "no-store"},
                body=json.dumps({"items": [], "cursor": "", "degraded": False}),
            )
            return
        route.fulfill(status=404, content_type="text/plain", body="Not found")

    page.route(f"{ORIGIN}/**", respond)


@pytest.mark.parametrize("width,height", ((1440, 900), (1024, 768), (390, 844)))
def test_shell_responsive_navigation_and_table_overflow(width: int, height: int) -> None:
    html = _queue_rest("light")
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(_browser_executable()))
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        _install_asset_host(page, html)
        page.goto(ORIGIN, wait_until="networkidle")

        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.locator(".ledger-table").evaluate(
            "element => element.scrollWidth >= element.clientWidth"
        )
        if width < 768:
            opener = page.locator("[data-sidebar-open]")
            assert opener.is_visible()
            opener.click()
            assert page.locator("html").get_attribute("data-mobile-nav") == "open"
            assert page.locator("#shell-sidebar").evaluate(
                "element => element.contains(document.activeElement)"
            )
            page.keyboard.press("Escape")
            assert opener.get_attribute("aria-expanded") == "false"
            assert opener.evaluate("element => element === document.activeElement")
        else:
            assert page.locator("#shell-sidebar").is_visible()
            assert not page.locator("[data-sidebar-open]").is_visible()
        assert not errors
        browser.close()


def test_sidebar_persistence_dark_theme_and_reduced_motion() -> None:
    html = _queue_rest("dark")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(_browser_executable()))
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            reduced_motion="reduce",
        )
        page = context.new_page()
        _install_asset_host(page, html)
        page.goto(ORIGIN, wait_until="networkidle")

        assert page.locator("html").get_attribute("data-theme") == "dark"
        assert page.locator(".page-heading").evaluate(
            "element => getComputedStyle(element).animationDuration === '0s'"
        )
        page.locator("[data-sidebar-toggle]").click()
        assert page.locator("html").get_attribute("data-sidebar") == "collapsed"
        assert page.evaluate("localStorage.getItem('covenant-radar-sidebar')") == "collapsed"
        page.reload(wait_until="networkidle")
        assert page.locator("html").get_attribute("data-sidebar") == "collapsed"
        browser.close()
