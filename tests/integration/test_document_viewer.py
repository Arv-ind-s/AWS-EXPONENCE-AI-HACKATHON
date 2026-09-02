"""Integration coverage for T-087 document classification and the viewer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from covenant_radar.asgi import create_app
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.document import Document, DocumentPage
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.documents.classify import DEFAULT_CONFIDENCE_FLOOR, UNCLASSIFIED_DOC_TYPE
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.security.uploads import PDF_MIME
from covenant_radar.web.routes.documents import create_documents_router
from tests.integration.test_document_upload import _Fixture

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def _add_page(
    fixture: _Fixture,
    document: Document,
    page_number: int,
    text: str,
    *,
    needs_review: bool = False,
) -> DocumentPage:
    page = DocumentPage(
        id=new_id(),
        document_id=document.id,
        page_number=page_number,
        text=text,
        ocr_confidence=None,
        needs_review=needs_review,
        width=612,
        height=792,
        created_at=_NOW,
        updated_at=_NOW,
        created_by_id=fixture.principal.id,
        updated_by_id=fixture.principal.id,
        request_id="rq-viewer-test-page",
    )
    fixture.session.add(page)
    fixture.session.flush()
    return page


def _reviewer(fixture: _Fixture) -> Principal:
    return Principal.user(
        fixture.principal.id,
        (Permission.VIEW_DOCUMENT, Permission.UPLOAD_DOCUMENT, Permission.CORRECT_SOURCE_DATA),
    )


def _app_client(fixture: _Fixture, *, principal: Principal | None = None) -> TestClient:
    fixture.service.scope_resolver = lambda _principal: fixture.scope
    app = create_app(
        routers=(create_documents_router(fixture.service),),
        principal_resolver=lambda _request: principal or fixture.principal,
    )
    return TestClient(app)


def test_low_confidence_classification_unclassified_not_guessed(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload()
        _add_page(
            fixture,
            document,
            1,
            "Please find enclosed the requested documents for your records. "
            "Kindly process at the earliest convenience.",
        )

        result = fixture.service.classify_document(
            fixture.principal, document.id, scope=fixture.scope
        )

        assert result.doc_type == UNCLASSIFIED_DOC_TYPE
        assert result.confidence < DEFAULT_CONFIDENCE_FLOOR
        assert result.matched_rules == ()
    finally:
        fixture.close()


def test_manual_override_recorded_alongside_automatic(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload()
        _add_page(
            fixture,
            document,
            1,
            "Monthly stock statement showing drawing power against hypothecated "
            "stock and outstanding book debts as on 31 March 2026.",
        )
        reviewer = _reviewer(fixture)

        automatic = fixture.service.classify_document(reviewer, document.id, scope=fixture.scope)
        assert automatic.doc_type == "stock_statement"

        record = fixture.service.override_classification(
            reviewer,
            document.id,
            "compliance_certificate",
            "Reviewer disagrees; this is actually a compliance certificate.",
            scope=fixture.scope,
        )

        assert record.user_value == {"doc_type": "compliance_certificate"}
        assert record.shown == {
            "doc_type": "stock_statement",
            "confidence": str(automatic.confidence),
        }
        assert record.actor_id == reviewer.id
        assert record.reason == "Reviewer disagrees; this is actually a compliance certificate."

        still_automatic = fixture.service.classify_document(
            reviewer, document.id, scope=fixture.scope
        )
        assert still_automatic.doc_type == "stock_statement"

        latest_override = fixture.service.get_classification_override(
            reviewer, document.id, scope=fixture.scope
        )
        assert latest_override is not None
        assert latest_override.id == record.id
        assert fixture.audit.events[-1][0] == "document_classification_overridden"
    finally:
        fixture.close()


def test_span_url_opens_correct_page_and_highlight(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload()
        text = "Sanctioned limit is INR 10 crore for the cash credit facility."
        _add_page(fixture, document, 1, text)
        start = text.index("cash credit facility")
        end = start + len("cash credit facility")

        with _app_client(fixture) as client:
            response = client.get(
                f"/documents/{document.id}/view",
                params={"page": 1, "start": start, "end": end},
            )

        assert response.status_code == 200
        assert "Page: 1" in response.text
        assert "cash credit facility" in response.text
        assert 'data-span-highlight="true"' in response.text
    finally:
        fixture.close()


def test_corrected_page_noted(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload()
        _add_page(fixture, document, 1, "OCR garble not usable.", needs_review=True)
        reviewer = _reviewer(fixture)
        fixture.service.correct_page(
            reviewer,
            document.id,
            1,
            "Corrected page text mentioning a stock statement.",
            expected_version=document.version,
            scope=fixture.scope,
        )

        with _app_client(fixture, principal=reviewer) as client:
            response = client.get(f"/documents/{document.id}/view", params={"page": 1})

        assert response.status_code == 200
        assert "Corrected page text mentioning a stock statement." in response.text
        assert 'data-page-corrected="true"' in response.text
    finally:
        fixture.close()


def test_out_of_scope_404(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        hidden_portfolio = Portfolio.create(
            code="DOCS-HIDDEN",
            name="Hidden Documents",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-viewer-test-hidden-portfolio",
        )
        hidden_borrower = Borrower(
            id=uuid4(),
            reference="B-DOC-HIDDEN",
            legal_name="Hidden Document Borrower",
            portfolio_id=hidden_portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-viewer-test-hidden-borrower",
        )
        fixture.session.add_all([hidden_portfolio, hidden_borrower])
        fixture.session.flush()
        hidden_scope = Scope.from_paths(fixture.principal.id, [hidden_portfolio.path])
        hidden_document = fixture.service.upload_document(
            fixture.principal,
            borrower_ref=hidden_borrower.reference,
            filename="hidden.pdf",
            content_type=PDF_MIME,
            data=b"%PDF-1.7\nhidden",
            doc_type="sanction_letter",
            scope=hidden_scope,
        )
        _add_page(fixture, hidden_document, 1, "Hidden page text.")

        with _app_client(fixture) as client:
            response = client.get(f"/documents/{hidden_document.id}/view", params={"page": 1})

        assert response.status_code == 404
    finally:
        fixture.close()


def test_no_javascript_quotes_span_text(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        document = fixture.upload()
        text = "Compliance certificate confirms all covenant compliance for the quarter."
        _add_page(fixture, document, 1, text)
        start = text.index("covenant compliance")
        end = start + len("covenant compliance")

        with _app_client(fixture) as client:
            response = client.get(
                f"/documents/{document.id}/view",
                params={"page": 1, "start": start, "end": end},
            )

        assert response.status_code == 200
        assert '<blockquote class="document-viewer__quote"' in response.text
        assert "covenant compliance" in response.text
    finally:
        fixture.close()
