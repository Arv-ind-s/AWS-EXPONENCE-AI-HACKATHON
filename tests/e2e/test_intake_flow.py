"""Offline browser contracts for T-097's covenant-intake workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_intake_screen import _generator, _ScreenFixture

pytestmark = pytest.mark.e2e


def _proposed_page(tmp_path: Path) -> tuple[str, str]:
    fixture = _ScreenFixture(tmp_path)
    try:
        document = fixture.document()
        client = fixture.client(generator=_generator)
        response = client.post(
            "/intake/proposals",
            data={
                "document_id": str(document.id),
                "facility_ref": fixture.bundle.facility.reference,
            },
        )
        assert response.status_code == 200
        proposal = fixture.bundle.service.proposals_for_document(
            fixture.principal,
            document.id,
            scope=fixture.bundle.scope(),
        )[0]
        return response.text, str(proposal.row.id)
    finally:
        fixture.close()


def test_upload_to_live_covenant_flow(tmp_path: Path) -> None:
    body, proposal_id = _proposed_page(tmp_path)

    assert 'action="/documents"' in body
    assert 'enctype="multipart/form-data"' in body
    assert 'id="intake-proposals"' in body
    assert f'action="/intake/proposals/{proposal_id}/submit"' in body
    assert "Confirm covenant" in body
    assert 'data-bulk-included="true"' in body


def test_span_click_opens_viewer(tmp_path: Path) -> None:
    body, _proposal_id = _proposed_page(tmp_path)

    assert 'class="provenance-link" href="/documents/' in body
    assert "/view?page=1&amp;start=0&amp;end=" in body
    assert "Open source span" in body
