"""Browser-level contracts for the T-073 portfolio queue."""

from __future__ import annotations

import re
from datetime import date
from html import unescape

import pytest

from tests.integration.test_queue_screen import _Fixture

pytestmark = pytest.mark.e2e


def _row_links(body: str) -> dict[str, tuple[str, ...]]:
    """Return the native keyboard links emitted for each queue row."""
    rows = re.findall(
        r'<tr\s+class="ledger-row"\s+id="(queue-row-[^"]+)"[^>]*>(.*?)</tr>',
        body,
        flags=re.DOTALL,
    )
    return {
        row_id: tuple(unescape(href) for href in re.findall(r'<a\s+[^>]*href="([^"]+)"', row_body))
        for row_id, row_body in rows
    }


def test_keyboard_reaches_and_opens_a_row() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("KEYBOARD")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-KEYBOARD")
        fixture.entry(run, borrower, 1, worst_covenant_version_id=None)

        with fixture.client() as client:
            response = client.get("/")

        assert response.status_code == 200
        links = _row_links(response.text)
        assert links == {
            f"queue-row-{borrower.id}": (
                f"/borrowers/{borrower.reference}",
                f"/why/borrower/{borrower.id}",
            )
        }
        # The borrower and in-context Why controls are both native anchors,
        # so keyboard review can open the case or its reconstructable trail.
        row_markup = re.search(
            rf'<tr\s+class="ledger-row"\s+id="queue-row-{re.escape(str(borrower.id))}"[^>]*>.*?</tr>',
            response.text,
            flags=re.DOTALL,
        )
        assert row_markup is not None
        assert 'tabindex="-1"' not in row_markup.group(0)
        assert 'role="button"' not in row_markup.group(0)
    finally:
        fixture.close()


def test_renders_both_themes() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("THEMES")
        fixture.grant_scope(portfolio)
        run = fixture.run(date(2026, 8, 30))
        borrower = fixture.borrower(portfolio, "B-THEMES")
        fixture.entry(run, borrower, 1, worst_covenant_version_id=None)

        with fixture.client() as client:
            for theme in ("light", "dark"):
                client.cookies.set("covenant_radar_theme", theme)
                response = client.get("/")
                assert response.status_code == 200
                assert f'<html lang="en" data-theme="{theme}"' in response.text
    finally:
        fixture.close()
