"""Integration coverage for the scope-safe global search resource."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.asgi import create_app
from covenant_radar.db.base import Base
from covenant_radar.db.models import (
    AuditEvent,
    Borrower,
    Case,
    Covenant,
    Document,
    DocumentPage,
    Facility,
    Memo,
    Portfolio,
)
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.search import create_search_router

pytestmark = pytest.mark.integration

_SEARCH_PERMISSIONS = (
    Permission.VIEW_QUEUE,
    Permission.VIEW_BORROWER,
    Permission.VIEW_CASE,
    Permission.VIEW_MEMO,
    Permission.VIEW_COVENANT,
    Permission.VIEW_DOCUMENT,
    Permission.VIEW_AUDIT,
)
_TOKEN = "global-search-token"
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _AuditSpy:
    def __init__(self) -> None:
        self.records: list[tuple[object, object, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del actor, request_id
        self.records.append((event_type, subject, payload))
        return object()


class _SearchBundle:
    def __init__(self, *, populate: bool = True) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.principal = Principal.user(uuid4(), _SEARCH_PERMISSIONS)
        self.audit = _AuditSpy()
        self.portfolio = Portfolio.create(
            code="SEARCH",
            name="Search portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="search-fixture",
        )
        self.session.add(self.portfolio)
        self.session.flush()
        self.borrower: Borrower | None = None
        if populate:
            self.populate()
        self.session.commit()

    def populate(self) -> None:
        borrower = Borrower(
            id=uuid4(),
            reference="BR-SEARCH",
            legal_name=f"Aster {_TOKEN}",
            portfolio_id=self.portfolio.id,
            industry_code=None,
            constitution="company",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="search-fixture",
        )
        facility = Facility(
            id=uuid4(),
            reference="FAC-SEARCH",
            borrower_id=borrower.id,
            facility_type=_TOKEN,
            sanctioned_limit=Decimal("100"),
            currency="INR",
            security_type="secured",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW + timedelta(seconds=1),
            updated_at=_NOW + timedelta(seconds=1),
            request_id="search-fixture",
        )
        covenant = Covenant(
            id=uuid4(),
            reference="COV-SEARCH",
            facility_id=facility.id,
            name=_TOKEN,
            covenant_class="financial",
            created_at=_NOW + timedelta(seconds=2),
            updated_at=_NOW + timedelta(seconds=2),
            request_id="search-fixture",
        )
        document = Document(
            id=uuid4(),
            borrower_id=borrower.id,
            doc_type="sanction_letter",
            filename=f"{_TOKEN}.pdf",
            content_hash="search-document-hash",
            byte_size=100,
            mime_type="application/pdf",
            storage_key="search/document.pdf",
            uploaded_by_id=self.principal.id,
            created_at=_NOW + timedelta(seconds=3),
            updated_at=_NOW + timedelta(seconds=3),
            request_id="search-fixture",
        )
        page = DocumentPage(
            id=uuid4(),
            document_id=document.id,
            page_number=1,
            text=f"Body contains {_TOKEN} and only scoped content.",
            created_at=_NOW + timedelta(seconds=4),
            updated_at=_NOW + timedelta(seconds=4),
            request_id="search-fixture",
        )
        memo = Memo(
            id=uuid4(),
            borrower_id=borrower.id,
            template_version="search-v1",
            slots={"token": _TOKEN},
            drafted_text=f"Memo body {_TOKEN}",
            created_at=_NOW + timedelta(seconds=5),
            updated_at=_NOW + timedelta(seconds=5),
            request_id="search-fixture",
        )
        case = Case(
            id=uuid4(),
            reference="CASE-SEARCH",
            borrower_id=borrower.id,
            state="open",
            closure_note=_TOKEN,
            created_at=_NOW + timedelta(seconds=6),
            updated_at=_NOW + timedelta(seconds=6),
            request_id="search-fixture",
        )
        audit_event = AuditEvent(
            id=uuid4(),
            sequence=1,
            occurred_at=_NOW + timedelta(seconds=7),
            event_type=f"{_TOKEN}-event",
            subject_type="borrower",
            subject_id=borrower.id,
            payload={"message": _TOKEN},
            hash="search-audit-hash",
            prev_hash=None,
            created_at=_NOW + timedelta(seconds=7),
            updated_at=_NOW + timedelta(seconds=7),
            request_id="search-fixture",
        )
        self.borrower = borrower
        self.session.add_all(
            [borrower, facility, covenant, document, page, memo, case, audit_event]
        )

    def client(
        self,
        principal: Principal | None = None,
        *,
        fingerprinter: object | None = None,
    ) -> TestClient:
        current = principal or self.principal
        router = create_search_router(
            self.session,
            audit_writer=self.audit,
            fingerprinter=fingerprinter,  # type: ignore[arg-type]
            scope_resolver=lambda _principal: Scope.from_paths(current.id, [self.portfolio.path]),
        )
        app = create_app(
            routers=(router,),
            principal_resolver=lambda _request: current,
        )
        return TestClient(app)

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_all_entity_types_searchable() -> None:
    bundle = _SearchBundle()
    try:
        with bundle.client() as client:
            response = client.get(f"/search?q={_TOKEN}&type=")

        assert response.status_code == 200
        assert "7 matching results" in response.text
        result_list = response.text.split("<ol>", maxsplit=1)[1].split("</ol>", maxsplit=1)[0]
        assert result_list.count("<li>") == 7
        assert all(
            value in result_list
            for value in (
                "FAC-SEARCH",
                ".pdf",
                "CASE-SEARCH",
                "event",
            )
        )
        assert "Aster" in result_list
        assert {
            name
            for name in (
                "Borrower",
                "Facility",
                "Covenant",
                "Document",
                "Memo",
                "Case",
                "Audit event",
            )
            if name in response.text
        } == {
            "Borrower",
            "Facility",
            "Covenant",
            "Document",
            "Memo",
            "Case",
            "Audit event",
        }
    finally:
        bundle.close()


def test_empty_query_shows_recent() -> None:
    bundle = _SearchBundle()
    try:
        with bundle.client() as client:
            response = client.get("/search")

        assert response.status_code == 200
        assert "Recent items" in response.text
        assert "7 recent items" in response.text
        assert response.text.index("CASE-SEARCH") < response.text.index("BR-SEARCH")
    finally:
        bundle.close()


def test_snippets_permission_aware() -> None:
    bundle = _SearchBundle()
    try:
        hidden_portfolio = Portfolio.create(
            code="HIDDEN-DOC",
            name="Hidden document portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="search-hidden-doc",
        )
        hidden_borrower = Borrower(
            id=uuid4(),
            reference="BR-HIDDEN-DOC",
            legal_name=f"Hidden {_TOKEN}",
            portfolio_id=hidden_portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="search-hidden-doc",
        )
        hidden_document = Document(
            id=uuid4(),
            borrower_id=hidden_borrower.id,
            doc_type="hidden_letter",
            filename="hidden-search-document.pdf",
            content_hash="hidden-search-document-hash",
            byte_size=100,
            mime_type="application/pdf",
            storage_key="hidden/document.pdf",
            uploaded_by_id=bundle.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="search-hidden-doc",
        )
        hidden_page = DocumentPage(
            id=uuid4(),
            document_id=hidden_document.id,
            page_number=1,
            text=f"hidden-only-body {_TOKEN}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="search-hidden-doc",
        )
        bundle.session.add_all([hidden_portfolio, hidden_borrower, hidden_document, hidden_page])
        bundle.session.commit()
        with bundle.client() as client:
            response = client.get(f"/search?q={_TOKEN}&type=document")

        assert response.status_code == 200
        assert "Body contains" in response.text
        assert _TOKEN in response.text
        assert "only scoped content" in response.text
        assert "hidden-only-body" not in response.text
    finally:
        bundle.close()


def test_within_latency_budget() -> None:
    bundle = _SearchBundle(populate=False)
    try:
        for index in range(100):
            bundle.session.add(
                Borrower(
                    id=uuid4(),
                    reference=f"BR-{index:03d}",
                    legal_name=f"Borrower {_TOKEN} {index}",
                    portfolio_id=bundle.portfolio.id,
                    created_at=_NOW + timedelta(seconds=index),
                    updated_at=_NOW + timedelta(seconds=index),
                    request_id="search-fixture",
                )
            )
        bundle.session.commit()
        started = perf_counter()
        with bundle.client() as client:
            response = client.get(f"/search?q={_TOKEN}&page_size=100")
        elapsed = perf_counter() - started

        assert response.status_code == 200
        assert "100 matching results" in response.text
        assert elapsed < 2.0
    finally:
        bundle.close()


def test_search_live_results_match_the_canonical_page() -> None:
    bundle = _SearchBundle()
    try:
        with bundle.client() as client:
            full = client.get(f"/search?q={_TOKEN}")
            fragment = client.get(
                f"/search?q={_TOKEN}",
                headers={"HX-Request": "true", "HX-Target": "search-results"},
            )

        assert full.status_code == fragment.status_code == 200
        assert "7 matching results" in full.text
        assert "7 matching results" in fragment.text
        assert 'id="search-results"' in fragment.text
        assert "<html" not in fragment.text
        assert 'hx-swap="outerHTML transition:true"' in full.text
        assert "data-submit-on-change" in full.text
        assert 'hx-sync="this:replace"' in full.text
        assert fragment.headers["vary"] == "HX-Request, HX-Target"
    finally:
        bundle.close()


__all__ = ["_NOW", "_SEARCH_PERMISSIONS", "_SearchBundle", "_TOKEN"]
