"""Integration coverage for T-135's REST API resources, schemas, pagination,
conditional requests and single error envelope (contracts `C-21`, `C-22`)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from covenant_radar.api.pagination import Cursor, digest_filters
from covenant_radar.api.v1.routers import (
    create_audit_events_router,
    create_borrowers_router,
    create_cases_router,
    create_covenant_tests_router,
    create_covenants_router,
    create_evidence_router,
    create_facilities_router,
    create_forecast_router,
    create_memos_router,
    create_simulations_router,
)
from covenant_radar.asgi import create_app
from covenant_radar.audit.record import AuditRecord
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastRun, Intervention, Simulation
from covenant_radar.db.models.identity import AppUser, UserPortfolioScope
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.db.models.workflow import Case, Memo
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
_CURSOR_SECRET = b"t-135-test-cursor-secret-bytes-0123456"

_ALL_VIEW_PERMISSIONS = (
    Permission.VIEW_BORROWER,
    Permission.VIEW_COVENANT,
    Permission.VIEW_EVIDENCE,
    Permission.VIEW_FORECAST,
    Permission.VIEW_MEMO,
    Permission.VIEW_CASE,
    Permission.VIEW_AUDIT,
    Permission.RUN_SIMULATION,
    Permission.CORRECT_SOURCE_DATA,
    Permission.REGISTER_COVENANT,
)


class _Audit:
    def record(self, *args: object, **kwargs: object) -> object:
        return object()


class _Bundle:
    """One in-scope resource of every `C-21` kind, plus one out-of-scope row."""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(uuid4(), _ALL_VIEW_PERMISSIONS)
        self.narrow_principal = Principal.user(uuid4(), (Permission.VIEW_EVIDENCE,))

        self.portfolio = self._portfolio("PF-A")
        self.other_portfolio = self._portfolio("PF-B")
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])

        self._user(self.principal.id, "principal-user")
        self.session.add(
            UserPortfolioScope(
                user_id=self.principal.id,
                portfolio_id=self.portfolio.id,
                include_descendants=True,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-scope-0001",
            )
        )
        self.session.flush()

        self.borrower = self._borrower(self.portfolio, "B-A0001")
        self.facility = self._facility(self.borrower, "F-A0001")
        self.covenant, self.covenant_version = self._covenant(self.facility, "CV-A0001")
        self.covenant_test = self._covenant_test(self.covenant_version)
        self.evidence = self._evidence(self.borrower, self.facility)
        self.run, self.forecast = self._forecast()
        self.intervention = self._intervention()
        self.simulation = self._simulation(self.forecast, self.intervention)
        self.case = self._case(self.borrower)
        self.memo = self._memo(self.borrower, self.case)
        self.audit_event = AuditRepository(self.session).append(
            AuditRecord(
                event_type="borrower_viewed",
                subject_type="borrower",
                subject_id=self.borrower.id,
                payload={"outcome": "read"},
                actor_id=self.principal.id,
                actor_label=None,
                occurred_at=_NOW,
                request_id="rq-audit-0001",
            )
        )

        other_borrower = self._borrower(self.other_portfolio, "B-B0001")
        other_facility = self._facility(other_borrower, "F-B0001")
        self.out_of_scope_evidence = self._evidence(other_borrower, other_facility, suffix="oos")

        self.session.commit()

        self.master_data_service = MasterDataService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            scope_resolver=lambda principal: self._scope_for(principal),
            request_id="rq-master-data-0001",
        )
        self.registry_service = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-registry-0001",
            scope_resolver=lambda principal: self._scope_for(principal),
            maker_checker_enabled=False,
        )

    def _scope_for(self, principal: Principal) -> Scope:
        if principal.id == self.principal.id:
            return self.scope
        return Scope.empty(principal.id)

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def app(self) -> object:
        return create_app(
            routers=(
                create_borrowers_router(self.master_data_service),
                create_facilities_router(self.master_data_service),
                create_covenants_router(self.registry_service),
                create_covenant_tests_router(self.session, cursor_secret=_CURSOR_SECRET),
                create_evidence_router(self.session, cursor_secret=_CURSOR_SECRET),
                create_forecast_router(self.session, cursor_secret=_CURSOR_SECRET),
                create_simulations_router(self.session, cursor_secret=_CURSOR_SECRET),
                create_memos_router(self.session, cursor_secret=_CURSOR_SECRET),
                create_cases_router(self.session, cursor_secret=_CURSOR_SECRET),
                create_audit_events_router(self.session, cursor_secret=_CURSOR_SECRET),
            ),
            principal_resolver=lambda _request: self._active_principal,
        )

    _active_principal: Principal = None  # type: ignore[assignment]

    def _portfolio(self, code: str) -> Portfolio:
        portfolio = Portfolio.create(
            code=code,
            name=code,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-portfolio-{code.lower()}",
        )
        self.session.add(portfolio)
        self.session.flush()
        return portfolio

    def _user(self, user_id: object, username: str) -> None:
        self.session.add(
            AppUser(
                id=user_id,
                username=username,
                email=f"{username}@example.test",
                full_name=username.title(),
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-user-{username}",
            )
        )
        self.session.flush()

    def _borrower(self, portfolio: Portfolio, reference: str) -> Borrower:
        borrower = Borrower(
            reference=reference,
            legal_name=f"Borrower {reference}",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-borrower-{reference.lower()}",
        )
        self.session.add(borrower)
        self.session.flush()
        return borrower

    def _facility(self, borrower: Borrower, reference: str) -> Facility:
        facility = Facility(
            reference=reference,
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000.0000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-facility-{reference.lower()}",
        )
        self.session.add(facility)
        self.session.flush()
        return facility

    def _covenant(self, facility: Facility, reference: str) -> tuple[Covenant, CovenantVersion]:
        covenant = Covenant(
            reference=reference,
            facility_id=facility.id,
            name="Leverage covenant",
            covenant_class="financial",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-covenant-{reference.lower()}",
        )
        self.session.add(covenant)
        self.session.flush()
        version = CovenantVersion(
            covenant_id=covenant.id,
            version_no=1,
            threshold=Decimal("2.5"),
            direction="max",
            unit="x",
            frequency="quarterly",
            test_basis="standalone",
            effective_from=date(2025, 1, 1),
            status="live",
            tested_at_least_once=False,
            registered_by_id=self.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-covenant-version-{reference.lower()}",
        )
        self.session.add(version)
        self.session.flush()
        return covenant, version

    def _covenant_test(self, version: CovenantVersion) -> CovenantTest:
        row = CovenantTest(
            covenant_version_id=version.id,
            as_of_date=date(2026, 1, 31),
            value=Decimal("2.1"),
            threshold_used=Decimal("2.5"),
            headroom_pct=Decimal("16.0"),
            verdict="pass",
            computed_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-covenant-test-0001",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _evidence(
        self, borrower: Borrower, facility: Facility, *, suffix: str = "a"
    ) -> EvidenceItem:
        row = EvidenceItem(
            borrower_id=borrower.id,
            facility_id=facility.id,
            family="payment",
            evidence_type="delay",
            first_seen=date(2026, 1, 1),
            last_seen=date(2026, 1, 15),
            state="sustained",
            counts_toward_pressure=True,
            source_event_ids=["evt-1", "evt-2"],
            last_scored_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-evidence-{suffix}",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _forecast(self) -> tuple[ForecastRun, Forecast]:
        run = ForecastRun(
            as_of_date=date(2026, 1, 31),
            started_at=_NOW,
            finished_at=_NOW,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-forecast-run-0001",
        )
        self.session.add(run)
        self.session.flush()
        forecast = Forecast(
            run_id=run.id,
            covenant_version_id=self.covenant_version.id,
            horizon_days=30,
            probability=Decimal("0.42"),
            confidence=Decimal("0.80"),
            below_confidence_floor=False,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-forecast-0001",
        )
        self.session.add(forecast)
        self.session.flush()
        return run, forecast

    def _intervention(self) -> Intervention:
        row = Intervention(
            code="LIMIT_REDUCTION_5CR",
            text="Reduce the sanctioned limit by INR 5 crore.",
            effect_model="level_shift",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-intervention-0001",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _simulation(self, forecast: Forecast, intervention: Intervention) -> Simulation:
        row = Simulation(
            forecast_id=forecast.id,
            intervention_id=intervention.id,
            parameters={"reduction": "5000000"},
            assumptions={"cure_days": 30},
            projected_cross_date=date(2026, 6, 1),
            probability=Decimal("0.20"),
            delta_days=45,
            delta_probability=Decimal("-0.22"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-simulation-0001",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _case(self, borrower: Borrower) -> Case:
        row = Case(
            reference="CASE-A0001",
            borrower_id=borrower.id,
            state="open",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-case-0001",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _memo(self, borrower: Borrower, case: Case) -> Memo:
        row = Memo(
            borrower_id=borrower.id,
            case_id=case.id,
            template_version="v1",
            slots={"headline": "Leverage covenant trending toward breach."},
            drafted_text="Model-drafted memo text.",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-memo-0001",
        )
        self.session.add(row)
        self.session.flush()
        return row


def _client(bundle: _Bundle, principal: Principal) -> TestClient:
    bundle._active_principal = principal
    return TestClient(bundle.app())


def test_every_resource_lists_and_reads() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle, bundle.principal) as client:
            cases = [
                ("/api/v1/borrowers", bundle.borrower.reference, "reference"),
                ("/api/v1/facilities", bundle.facility.reference, "reference"),
                ("/api/v1/covenants", bundle.covenant.reference, "reference"),
                ("/api/v1/tests", str(bundle.covenant_test.id), "id"),
                ("/api/v1/evidence", str(bundle.evidence.id), "id"),
                ("/api/v1/forecasts", str(bundle.forecast.id), "id"),
                ("/api/v1/simulations", str(bundle.simulation.id), "id"),
                ("/api/v1/memos", str(bundle.memo.id), "id"),
                ("/api/v1/cases", str(bundle.case.id), "id"),
                ("/api/v1/audit-events", str(bundle.audit_event.id), "id"),
            ]
            for list_path, expected_key, key_field in cases:
                list_response = client.get(list_path)
                assert list_response.status_code == 200, list_path
                rows = list_response.json()
                assert isinstance(rows, list) and rows, list_path
                assert any(str(row[key_field]) == expected_key for row in rows), list_path

                detail_response = client.get(f"{list_path}/{expected_key}")
                assert detail_response.status_code == 200, list_path
                assert str(detail_response.json()[key_field]) == expected_key
    finally:
        bundle.close()


def test_write_on_read_only_405() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle, bundle.principal) as client:
            response = client.post("/api/v1/tests", json={})
        assert response.status_code == 405
        allowed = {method.strip() for method in response.headers.get("allow", "").split(",")}
        assert "GET" in allowed
        assert "POST" not in allowed
    finally:
        bundle.close()


def test_no_route_for_forbidden_operations() -> None:
    bundle = _Bundle()
    try:
        app = bundle.app()
        forbidden_terms = (
            "credit-decision",
            "credit_decision",
            "sanction",
            "waiver-approve",
            "waiver_approve",
            "auto-approve",
            "escalate",
            "fraud",
        )
        offenders = [
            f"{','.join(sorted(getattr(route, 'methods', ()) or ()))} {getattr(route, 'path', '')}"
            for route in app.routes
            for term in forbidden_terms
            if term in getattr(route, "path", "").lower()
            or term in (getattr(route, "name", "") or "").lower()
        ]
        assert offenders == []
    finally:
        bundle.close()


def test_scope_returns_404() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle, bundle.principal) as client:
            response = client.get(f"/api/v1/evidence/{bundle.out_of_scope_evidence.id}")
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "not_found"
    finally:
        bundle.close()


def test_mismatched_cursor_refused() -> None:
    bundle = _Bundle()
    try:
        no_filters = {"borrower_id": None, "facility_id": None, "family": None, "state": None}
        stale_digest = digest_filters(no_filters)
        forged_cursor = Cursor(
            primary=bundle.evidence.updated_at.isoformat(),
            id=bundle.evidence.id,
            filters_digest=stale_digest,
        ).encode(_CURSOR_SECRET)

        with _client(bundle, bundle.principal) as client:
            response = client.get(
                "/api/v1/evidence",
                params={"cursor": forged_cursor, "family": "payment"},
            )
        assert response.status_code == 422
        body = response.json()
        assert body["field"] == "cursor"
    finally:
        bundle.close()


def test_conditional_request_304() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle, bundle.principal) as client:
            first = client.get(f"/api/v1/memos/{bundle.memo.id}")
            assert first.status_code == 200
            etag = first.headers["etag"]

            second = client.get(
                f"/api/v1/memos/{bundle.memo.id}",
                headers={"If-None-Match": etag},
            )
        assert second.status_code == 304
        assert second.headers["etag"] == etag
        assert second.content == b""
    finally:
        bundle.close()


def test_single_error_envelope_across_routes() -> None:
    bundle = _Bundle()
    try:
        with _client(bundle, bundle.principal) as client:
            not_found = client.get(f"/api/v1/memos/{uuid4()}")
            bad_path_param = client.get("/api/v1/tests/not-a-uuid")

        forged_cursor = Cursor(
            primary=bundle.evidence.updated_at.isoformat(),
            id=bundle.evidence.id,
            filters_digest=digest_filters({"wrong": "digest"}),
        ).encode(_CURSOR_SECRET)
        with _client(bundle, bundle.principal) as client:
            bad_cursor = client.get("/api/v1/evidence", params={"cursor": forged_cursor})

        with _client(bundle, bundle.narrow_principal) as client:
            forbidden = client.get("/api/v1/memos")

        for response, expected_status in (
            (not_found, 404),
            (bad_path_param, 422),
            (bad_cursor, 422),
            (forbidden, 403),
        ):
            assert response.status_code == expected_status
            body = response.json()
            assert set(("error", "message", "field", "request_id")) <= set(body)
            assert isinstance(body["error"], str) and body["error"]
            assert isinstance(body["message"], str) and body["message"]
            assert isinstance(body["request_id"], str) and body["request_id"]
    finally:
        bundle.close()
