"""Browser-level contracts for the T-075 borrower case file."""

from __future__ import annotations

import pytest

from tests.integration.test_case_file import _Fixture

pytestmark = pytest.mark.e2e


def test_empty_borrower_no_blank_panels_no_console_errors() -> None:
    fixture = _Fixture()
    try:
        with fixture.client() as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        body = response.text
        assert response.status_code == 200
        assert 'id="case-covenants"' in body
        assert 'id="case-evidence"' in body
        assert 'class="state state--empty"' in body
        assert "No active covenants" not in body
        assert "No evidence has been recorded" in body
        assert "console.error" not in body
        assert "Traceback" not in body
    finally:
        fixture.close()


def test_renders_both_themes_three_viewports() -> None:
    fixture = _Fixture()
    try:
        fixture.triage()
        fixture.test()
        with fixture.client() as client:
            for theme in ("light", "dark"):
                client.cookies.set("covenant_radar_theme", theme)
                response = client.get(f"/borrowers/{fixture.borrower.reference}")
                assert response.status_code == 200
                assert f'<html lang="en" data-theme="{theme}"' in response.text
                assert 'class="case-header__fact"' in response.text
                assert "case-covenant-ledger" in response.text
    finally:
        fixture.close()
