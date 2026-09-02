"""Integration coverage for the T-023 master-data vertical slice."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.api.v1.routers.borrowers import create_borrowers_router
from covenant_radar.api.v1.routers.facilities import create_facilities_router
from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict, NotFound
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.security.crypto import FieldEncryptor, HMACFingerprinter
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.web.routes.borrower import create_borrower_router
from covenant_radar.web.routes.master_data import create_master_data_router

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object], object, str]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        self.events.append((event_type, subject, payload, actor, request_id))
        return object()


class _Bundle:
    def __init__(self, *, encrypted: bool = False) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(
            engine,
            tables=[Portfolio.__table__, Borrower.__table__, Facility.__table__],
        )
        self.session = Session(engine)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(),
            (Permission.VIEW_BORROWER, Permission.CORRECT_SOURCE_DATA),
        )
        self.scope: Scope | None = None
        encryptor = FieldEncryptor({"test": b"x" * 32}, "test") if encrypted else None
        fingerprinter = HMACFingerprinter(b"y" * 32) if encrypted else None
        self.service = MasterDataService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            encryptor=encryptor,
            fingerprinter=fingerprinter,
            scope_resolver=lambda _principal: self.scope or Scope.empty(self.principal.id),
            request_id="rq-t023-test-0001",
        )

    def close(self) -> None:
        self.session.close()

    def root(self, code: str = "ROOT") -> Portfolio:
        portfolio = self.service.create_portfolio(
            self.principal,
            code=code,
            name=code.title(),
            scope=Scope.empty(self.principal.id),
        )
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [portfolio.path])
        return portfolio

    def borrower(
        self, portfolio: Portfolio, reference: str = "B-000001", **values: object
    ) -> Borrower:
        return self.service.create_borrower(
            self.principal,
            reference=reference,
            legal_name=f"Borrower {reference}",
            portfolio_id=portfolio.id,
            scope=self.scope,
            **values,
        )

    def facility(self, borrower: Borrower, reference: str = "F-000001") -> Facility:
        return self.service.create_facility(
            self.principal,
            reference=reference,
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000.0000"),
            currency="INR",
            sanction_date=date(2026, 1, 1),
            effective_from=date(2026, 1, 1),
            scope=self.scope,
        )


def test_limit_change_creates_effective_dated_row() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        predecessor = bundle.facility(borrower)

        successor = bundle.service.update_facility(
            bundle.principal,
            predecessor.reference,
            expected_version=predecessor.version,
            sanctioned_limit=Decimal("2000.0000"),
            effective_from=date(2026, 2, 1),
            scope=bundle.scope,
        )

        assert successor.id != predecessor.id
        assert successor.sanctioned_limit == Decimal("2000.0000")
        assert predecessor.effective_to == date(2026, 2, 1)
        assert predecessor.superseded_by_id == successor.id
        assert successor.version == 1
    finally:
        bundle.close()


def test_dated_read_returns_prior_limit() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        predecessor = bundle.facility(borrower)
        successor = bundle.service.update_facility(
            bundle.principal,
            predecessor.reference,
            expected_version=predecessor.version,
            sanctioned_limit=Decimal("2000.0000"),
            effective_from=date(2026, 2, 1),
            scope=bundle.scope,
        )

        prior = bundle.service.get_facility_as_of(
            bundle.principal,
            borrower_reference=borrower.reference,
            as_of=date(2026, 1, 31),
            scope=bundle.scope,
        )
        current = bundle.service.get_facility_as_of(
            bundle.principal,
            borrower_reference=borrower.reference,
            as_of=date(2026, 2, 1),
            scope=bundle.scope,
        )

        assert prior.id == predecessor.id
        assert prior.sanctioned_limit == Decimal("1000.0000")
        assert current.id == successor.id
    finally:
        bundle.close()


def test_deactivate_with_live_facilities_refused() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        facility = bundle.facility(borrower)

        with pytest.raises(Conflict, match=facility.reference):
            bundle.service.deactivate_borrower(
                bundle.principal,
                borrower.reference,
                expected_version=borrower.version,
                scope=bundle.scope,
            )
    finally:
        bundle.close()


def test_duplicate_cin_refused_offers_existing() -> None:
    bundle = _Bundle(encrypted=True)
    try:
        portfolio = bundle.root()
        existing = bundle.borrower(portfolio, cin="U12345678901234567890")

        with pytest.raises(Conflict, match=existing.reference) as raised:
            bundle.borrower(
                portfolio,
                reference="B-000002",
                cin="U12345678901234567890",
            )
        assert raised.value.existing_reference == existing.reference
    finally:
        bundle.close()


def test_out_of_scope_returns_404() -> None:
    bundle = _Bundle()
    try:
        visible_portfolio = bundle.root("VISIBLE")
        visible = bundle.borrower(visible_portfolio, "B-VISIBLE")
        hidden_portfolio = bundle.service.create_portfolio(
            bundle.principal,
            code="HIDDEN",
            name="Hidden",
            scope=Scope.empty(bundle.principal.id),
        )
        hidden = bundle.service.create_borrower(
            bundle.principal,
            reference="B-HIDDEN",
            legal_name="Borrower B-HIDDEN",
            portfolio_id=hidden_portfolio.id,
            scope=Scope.from_paths(bundle.principal.id, [hidden_portfolio.path]),
        )

        with pytest.raises(NotFound, match="current scope"):
            bundle.service.get_borrower(
                bundle.principal,
                hidden.reference,
                scope=Scope.from_paths(bundle.principal.id, [visible_portfolio.path]),
            )
        assert visible.reference == "B-VISIBLE"
    finally:
        bundle.close()


def test_stale_version_conflict_names_change() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        bundle.service.update_borrower(
            bundle.principal,
            borrower.reference,
            expected_version=borrower.version,
            legal_name="Changed Borrower",
            scope=bundle.scope,
        )

        with pytest.raises(Conflict, match=r"Borrower B-000001.*changed.*by"):
            bundle.service.update_borrower(
                bundle.principal,
                borrower.reference,
                expected_version=1,
                legal_name="Stale Write",
                scope=bundle.scope,
            )
    finally:
        bundle.close()


def test_missing_field_422_with_path() -> None:
    bundle = _Bundle()
    try:
        bundle.root()
        app = create_app(
            routers=(create_borrowers_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/borrowers",
                json={"reference": "B-MISSING", "portfolio_id": str(bundle.scope.principal_id)},
            )
        assert response.status_code == 422
        assert any("legal_name" in str(item["loc"]) for item in response.json()["detail"])
    finally:
        bundle.close()


def test_every_change_audited() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        facility = bundle.facility(borrower)
        bundle.service.update_borrower(
            bundle.principal,
            borrower.reference,
            expected_version=borrower.version,
            legal_name="Updated Borrower",
            scope=bundle.scope,
        )
        successor = bundle.service.update_facility(
            bundle.principal,
            facility.reference,
            expected_version=facility.version,
            sanctioned_limit=Decimal("1200.0000"),
            effective_from=date(2026, 2, 1),
            scope=bundle.scope,
        )
        bundle.service.deactivate_facility(
            bundle.principal,
            successor.reference,
            expected_version=successor.version,
            effective_to=date(2026, 3, 1),
            scope=bundle.scope,
        )
        bundle.service.deactivate_borrower(
            bundle.principal,
            borrower.reference,
            expected_version=borrower.version,
            scope=bundle.scope,
        )

        events = {event[0] for event in bundle.audit.events}
        assert {
            "master_data_portfolio_created",
            "master_data_borrower_created",
            "master_data_borrower_updated",
            "master_data_facility_created",
            "master_data_facility_limit_changed",
            "master_data_facility_deactivated",
            "master_data_borrower_deactivated",
        } <= events
        assert len(bundle.audit.events) == 7
    finally:
        bundle.close()


def test_web_and_api_agree() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        app = create_app(
            routers=(
                create_borrowers_router(bundle.service),
                create_facilities_router(bundle.service),
                create_master_data_router(bundle.service),
            ),
            principal_resolver=lambda _request: bundle.principal,
        )
        with TestClient(app) as client:
            api_response = client.get("/api/v1/borrowers")
            web_response = client.get("/borrowers")

        assert api_response.status_code == 200
        assert web_response.status_code == 200
        api_row = next(row for row in api_response.json() if row["reference"] == borrower.reference)
        assert api_row["legal_name"] == borrower.legal_name
        assert borrower.reference in web_response.text
        assert borrower.legal_name in web_response.text
    finally:
        bundle.close()


def test_borrower_create_form_offers_only_scoped_portfolios() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root("VISIBLE")
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )
        with TestClient(app) as client:
            response = client.get("/borrowers/new")

        assert response.status_code == 200
        assert (
            '<select class="field__control" id="field-portfolio_id" name="portfolio_id"'
            in response.text
        )
        assert f'value="{portfolio.id}"' in response.text
        assert "VISIBLE — Visible" in response.text
    finally:
        bundle.close()


def test_web_create_borrower_persists_the_selected_portfolio() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root("CREATE")
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )
        with TestClient(app) as client:
            response = client.post(
                "/borrowers/new",
                data={
                    "reference": "B-WEB-CREATE",
                    "legal_name": "Web-created borrower",
                    "portfolio_id": str(portfolio.id),
                },
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/borrowers/B-WEB-CREATE/master-data"
        created = bundle.service.get_borrower(bundle.principal, "B-WEB-CREATE")
        assert created.portfolio_id == portfolio.id
        assert created.legal_name == "Web-created borrower"
    finally:
        bundle.close()


def test_borrower_master_filters_apply_in_sql_and_render_active_values() -> None:
    bundle = _Bundle()
    try:
        first_portfolio = bundle.root("FIRST")
        second_portfolio = bundle.service.create_portfolio(
            bundle.principal,
            code="SECOND",
            name="Second",
            scope=Scope.empty(bundle.principal.id),
        )
        bundle.session.flush()
        bundle.scope = Scope.from_paths(
            bundle.principal.id,
            [first_portfolio.path, second_portfolio.path],
        )
        matching = bundle.service.create_borrower(
            bundle.principal,
            reference="B-FILTER-MATCH",
            legal_name="Needle Engineering Private Limited",
            portfolio_id=first_portfolio.id,
            scope=bundle.scope,
        )
        wrong_portfolio = bundle.service.create_borrower(
            bundle.principal,
            reference="B-FILTER-OTHER",
            legal_name="Needle Trading Private Limited",
            portfolio_id=second_portfolio.id,
            scope=bundle.scope,
        )
        inactive = bundle.service.create_borrower(
            bundle.principal,
            reference="B-FILTER-INACTIVE",
            legal_name="Needle Inactive Private Limited",
            portfolio_id=first_portfolio.id,
            scope=bundle.scope,
        )
        bundle.service.deactivate_borrower(
            bundle.principal,
            inactive.reference,
            expected_version=inactive.version,
        )
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )

        with TestClient(app) as client:
            active = client.get(
                "/borrowers",
                params={
                    "q": "needle",
                    "portfolio": str(first_portfolio.id),
                    "status": "active",
                },
            )
            inactive_only = client.get("/borrowers", params={"status": "inactive"})

        assert active.status_code == 200
        assert matching.reference in active.text
        assert wrong_portfolio.reference not in active.text
        assert inactive.reference not in active.text
        assert 'id="field-q"' in active.text
        assert 'id="field-portfolio"' in active.text
        assert 'id="field-status"' in active.text
        assert 'href="/borrowers"' in active.text
        assert inactive_only.status_code == 200
        assert inactive.reference in inactive_only.text
        assert matching.reference not in inactive_only.text
    finally:
        bundle.close()


def test_master_detail_path_survives_the_case_file_route_and_hides_forbidden_actions() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        facility = bundle.facility(borrower)
        read_only = Principal.user(bundle.principal.id, (Permission.VIEW_BORROWER,))
        app = create_app(
            routers=(
                create_borrower_router(bundle.session),
                create_master_data_router(bundle.service),
            ),
            principal_resolver=lambda _request: read_only,
        )

        with TestClient(app) as client:
            borrower_list = client.get("/borrowers")
            borrower_detail = client.get(f"/borrowers/{borrower.reference}/master-data")
            facility_list = client.get("/facilities")
            facility_detail = client.get(f"/facilities/{facility.reference}")
            portfolio_list = client.get("/portfolios")
            portfolio_detail = client.get(f"/portfolios/{portfolio.id}")

        assert borrower_detail.status_code == 200
        assert 'id="borrower-summary"' in borrower_detail.text
        assert f'href="/borrowers/{borrower.reference}/master-data"' in borrower_list.text
        assert f'action="/borrowers/{borrower.reference}/edit"' not in borrower_detail.text
        assert 'href="/facilities/new"' not in facility_list.text
        assert f'action="/facilities/{facility.reference}/edit"' not in facility_detail.text
        assert 'href="/portfolios/new"' not in portfolio_list.text
        assert f'action="/portfolios/{portfolio.id}/edit"' not in portfolio_detail.text
    finally:
        bundle.close()


def _facility(
    bundle: _Bundle,
    borrower: Borrower,
    reference: str,
    *,
    facility_type: str = "term_loan",
    sanctioned_limit: str = "1000.0000",
    outstanding: str | None = None,
    maturity_date: date | None = None,
) -> Facility:
    return bundle.service.create_facility(
        bundle.principal,
        reference=reference,
        borrower_id=borrower.id,
        facility_type=facility_type,
        sanctioned_limit=Decimal(sanctioned_limit),
        currency="INR",
        outstanding=None if outstanding is None else Decimal(outstanding),
        sanction_date=date(2026, 1, 1),
        effective_from=date(2026, 1, 1),
        maturity_date=maturity_date,
        scope=bundle.scope,
    )


def test_facility_list_names_the_borrower_and_links_each_row() -> None:
    """The list must identify a facility by things a reader can act on.

    It previously printed the borrower's UUID and hung one stacked "Open"
    button per row underneath the table; both are regressions worth pinning.
    """
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        facility = _facility(
            bundle, borrower, "F-LIST-01", outstanding="950.0000", maturity_date=date(2027, 3, 31)
        )
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )

        with TestClient(app) as client:
            response = client.get("/facilities")

        assert response.status_code == 200
        assert str(borrower.id) not in response.text
        assert borrower.reference in response.text
        assert borrower.legal_name in response.text
        assert f'href="/facilities/{facility.reference}"' in response.text
        assert 'href="/facilities/insights"' in response.text
        # 1,000.00 sanctioned against 950.00 drawn is 95%, which is the band
        # the screen is meant to mark rather than leave as a bare number.
        assert "95.0%" in response.text
        assert "md-band--breach" in response.text
        assert "Showing 1–1 of 1 facilities" in response.text
    finally:
        bundle.close()


def test_facility_filters_apply_in_sql() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        needle = bundle.service.create_borrower(
            bundle.principal,
            reference="B-NEEDLE",
            legal_name="Needle Engineering Private Limited",
            portfolio_id=portfolio.id,
            scope=bundle.scope,
        )
        other = bundle.service.create_borrower(
            bundle.principal,
            reference="B-OTHER",
            legal_name="Haystack Trading Private Limited",
            portfolio_id=portfolio.id,
            scope=bundle.scope,
        )
        matching = _facility(bundle, needle, "F-MATCH-01", facility_type="cash_credit")
        wrong_type = _facility(bundle, needle, "F-MATCH-02", facility_type="term_loan")
        wrong_borrower = _facility(bundle, other, "F-OTHER-01", facility_type="cash_credit")
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )

        with TestClient(app) as client:
            filtered = client.get("/facilities", params={"q": "needle", "type": "cash_credit"})
            no_matches = client.get("/facilities", params={"q": "no-such-borrower"})

        assert filtered.status_code == 200
        assert matching.reference in filtered.text
        assert wrong_type.reference not in filtered.text
        assert wrong_borrower.reference not in filtered.text
        assert 'id="field-q"' in filtered.text
        assert 'id="field-type"' in filtered.text
        assert 'id="field-currency"' in filtered.text
        assert 'id="field-status"' in filtered.text
        assert "No facilities match the active filters." in no_matches.text
    finally:
        bundle.close()


def test_facility_status_filter_reaches_superseded_versions() -> None:
    """A superseded row is reachable, but never mixed into the default view."""
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        predecessor = _facility(bundle, borrower, "F-HIST-01")
        successor = bundle.service.update_facility(
            bundle.principal,
            predecessor.reference,
            expected_version=predecessor.version,
            sanctioned_limit=Decimal("2000.0000"),
            effective_from=date(2026, 2, 1),
            scope=bundle.scope,
        )
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )

        with TestClient(app) as client:
            default = client.get("/facilities")
            superseded = client.get("/facilities", params={"status": "superseded"})
            everything = client.get("/facilities", params={"status": "all"})
            unrecognised = client.get("/facilities", params={"status": "not-a-status"})

        assert successor.reference in default.text
        assert f'href="/facilities/{predecessor.reference}"' not in default.text
        assert f'href="/facilities/{predecessor.reference}"' in superseded.text
        assert f'href="/facilities/{successor.reference}"' not in superseded.text
        assert f'href="/facilities/{predecessor.reference}"' in everything.text
        assert f'href="/facilities/{successor.reference}"' in everything.text
        assert f'href="/facilities/{predecessor.reference}"' not in unrecognised.text
    finally:
        bundle.close()


def test_facility_detail_shows_the_record_and_its_limit_history() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        predecessor = _facility(
            bundle, borrower, "F-DETAIL-01", outstanding="400.0000", maturity_date=date(2029, 3, 31)
        )
        successor = bundle.service.update_facility(
            bundle.principal,
            predecessor.reference,
            expected_version=predecessor.version,
            sanctioned_limit=Decimal("2000.0000"),
            effective_from=date(2026, 2, 1),
            scope=bundle.scope,
        )
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )

        with TestClient(app) as client:
            response = client.get(f"/facilities/{successor.reference}")

        assert response.status_code == 200
        assert str(borrower.id) not in response.text
        assert f'href="/borrowers/{borrower.reference}/master-data"' in response.text
        assert "Term loan" in response.text
        assert "31 Mar 2029" in response.text
        # Both versions of the chain, and the size of the revision between them.
        assert predecessor.reference in response.text
        assert successor.reference in response.text
        assert "+1,000.00" in response.text
        assert "Superseded" in response.text
    finally:
        bundle.close()


def test_facility_insights_summarise_the_book() -> None:
    bundle = _Bundle()
    try:
        portfolio = bundle.root()
        borrower = bundle.borrower(portfolio)
        _facility(
            bundle,
            borrower,
            "F-BOOK-01",
            facility_type="cash_credit",
            sanctioned_limit="1000.0000",
            outstanding="600.0000",
            maturity_date=date(2027, 3, 31),
        )
        _facility(
            bundle,
            borrower,
            "F-BOOK-02",
            facility_type="term_loan",
            sanctioned_limit="3000.0000",
            outstanding="1200.0000",
            maturity_date=date(2030, 3, 31),
        )
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )

        with TestClient(app) as client:
            response = client.get("/facilities/insights")

        assert response.status_code == 200
        # 4,000.00 sanctioned, 1,800.00 drawn, so the book runs at 45%.
        assert "4,000.00" in response.text
        assert "1,800.00" in response.text
        assert "45.0%" in response.text
        assert "Sanction vintage" in response.text
        assert "Maturity profile" in response.text
        assert "Utilisation distribution" in response.text
        # Both facilities were sanctioned on 1 January 2026, which is FY26.
        assert "FY26" in response.text
        assert "Cash credit" in response.text
    finally:
        bundle.close()


def test_facility_insights_refuse_nothing_when_the_scope_is_empty() -> None:
    bundle = _Bundle()
    try:
        bundle.root()
        app = create_app(
            routers=(create_master_data_router(bundle.service),),
            principal_resolver=lambda _request: bundle.principal,
        )

        with TestClient(app) as client:
            response = client.get("/facilities/insights")

        assert response.status_code == 200
        assert "There are no facilities in this scope to summarise." in response.text
    finally:
        bundle.close()


def test_borrower_create_only_router_exposes_static_create_paths() -> None:
    bundle = _Bundle()
    try:
        router = create_master_data_router(bundle.service, borrower_create_only=True)
        assert {(route.path, method) for route in router.routes for method in route.methods} == {
            ("/borrowers/new", "GET"),
            ("/borrowers/new", "POST"),
        }
    finally:
        bundle.close()
