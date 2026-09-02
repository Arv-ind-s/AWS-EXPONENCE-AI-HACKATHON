"""Security acceptance tests for T-137's scope and personal-data boundary."""

from __future__ import annotations

from uuid import uuid4

import pytest

from covenant_radar.audit.events import AuditEventType
from covenant_radar.security.crypto import HMACFingerprinter
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from tests.integration.test_search import _SearchBundle

pytestmark = pytest.mark.security


def test_out_of_scope_absent_from_results_and_count() -> None:
    bundle = _SearchBundle()
    try:
        from covenant_radar.db.models import Borrower, Portfolio

        hidden_portfolio = Portfolio.create(
            code="HIDDEN",
            name="Hidden portfolio",
            created_at=bundle.portfolio.created_at,
            updated_at=bundle.portfolio.updated_at,
            request_id="search-hidden",
        )
        hidden_borrower = Borrower(
            id=uuid4(),
            reference="BR-HIDDEN",
            legal_name="Hidden global-search-token borrower",
            portfolio_id=hidden_portfolio.id,
            created_at=bundle.portfolio.created_at,
            updated_at=bundle.portfolio.updated_at,
            request_id="search-hidden",
        )
        bundle.session.add_all([hidden_portfolio, hidden_borrower])
        bundle.session.commit()

        with bundle.client() as client:
            response = client.get("/search?q=global-search-token&type=borrower")

        assert response.status_code == 200
        assert "BR-SEARCH" in response.text
        assert "BR-HIDDEN" not in response.text
        assert "1 matching results" in response.text
    finally:
        bundle.close()


def test_personal_match_requires_permission_and_is_logged() -> None:
    bundle = _SearchBundle()
    fingerprinter = HMACFingerprinter(b"p" * 32)
    try:
        assert bundle.borrower is not None
        personal_value = "U123456789"
        bundle.borrower.cin_fingerprint = fingerprinter.fingerprint(personal_value)
        bundle.session.commit()

        basic_principal = Principal.user(
            uuid4(),
            (Permission.VIEW_QUEUE, Permission.VIEW_BORROWER),
        )
        with bundle.client(basic_principal, fingerprinter=fingerprinter) as client:
            denied = client.get(f"/search?q={personal_value}&type=borrower")

        assert denied.status_code == 200
        assert "BR-SEARCH" not in denied.text
        assert "0 matching results" in denied.text
        assert bundle.audit.records == []

        privileged = Principal.user(
            uuid4(),
            (Permission.VIEW_QUEUE, Permission.VIEW_BORROWER, Permission.READ_PERSONAL_DATA),
        )
        with bundle.client(privileged, fingerprinter=fingerprinter) as client:
            allowed = client.get(f"/search?q={personal_value}&type=borrower")

        assert allowed.status_code == 200
        assert "BR-SEARCH" in allowed.text
        assert len(bundle.audit.records) == 1
        event_type, subject, payload = bundle.audit.records[0]
        assert event_type == AuditEventType.MASTER_DATA_PERSONAL_DATA_ACCESSED.value
        assert subject == ("search", privileged.id)
        assert payload["action"] == "search_personal_data_accessed"
        assert payload["query_sha256"] != personal_value
        assert personal_value not in str(payload)
    finally:
        bundle.close()
