"""Capture review-only UI screenshots without pixel-comparison assertions."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.a11y._screens import _sign_in_rest  # noqa: E402
from tests.integration.test_queue_screen import _Fixture  # noqa: E402

OUTPUT = ROOT / "var" / "snapshots" / "ui-redesign"
BASE_URL = "http://127.0.0.1:8001"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def _queue_html(theme: str) -> str:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("UI-REVIEW")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 31))
        borrower = fixture.borrower(
            portfolio,
            "B-UI-REVIEW",
            legal_name="Meridian Industrial Systems Private Limited",
        )
        version = fixture.covenant_version(borrower, "CV-UI-REVIEW")
        fixture.forecast(run, version, 30, crossing=date(2026, 10, 15))
        fixture.entry(run, borrower, rank=1, band="watch", worst_covenant_version_id=version.id)
        with fixture.client() as client:
            client.cookies.set("covenant_radar_theme", theme)
            response = client.get("/")
            response.raise_for_status()
            return response.text.replace("<head>", f'<head><base href="{BASE_URL}/">', 1)
    finally:
        fixture.close()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    queue_pages = {theme: _queue_html(theme) for theme in ("light", "dark")}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(CHROME))

        sign_in = browser.new_page(viewport={"width": 1440, "height": 900})
        sign_in_html = _sign_in_rest("light").replace(
            "<head>", f'<head><base href="{BASE_URL}/">', 1
        )
        sign_in.set_content(sign_in_html, wait_until="networkidle")
        sign_in.evaluate("document.documentElement.classList.replace('no-js', 'js')")
        sign_in.screenshot(path=OUTPUT / "sign-in-light-1440x900.png", full_page=True)

        for theme, html in queue_pages.items():
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(html, wait_until="networkidle")
            page.evaluate("document.documentElement.classList.replace('no-js', 'js')")
            page.screenshot(path=OUTPUT / f"queue-{theme}-1440x900.png", full_page=True)
            page.close()

        tablet = browser.new_page(viewport={"width": 1024, "height": 768})
        tablet.set_content(queue_pages["light"], wait_until="networkidle")
        tablet.evaluate("document.documentElement.classList.replace('no-js', 'js')")
        tablet.screenshot(path=OUTPUT / "queue-light-1024x768.png", full_page=True)
        tablet.close()

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.set_content(queue_pages["light"], wait_until="networkidle")
        mobile.evaluate("document.documentElement.classList.replace('no-js', 'js')")
        mobile.screenshot(path=OUTPUT / "queue-light-390x844.png", full_page=True)
        mobile.close()

        browser.close()


if __name__ == "__main__":
    main()
