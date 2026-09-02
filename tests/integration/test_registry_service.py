"""Integration coverage for T-033's registry and maker-checker surface."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.api.v1.routers.covenants import create_covenants_router
from covenant_radar.asgi import create_app
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import Conflict
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.registry import RegistryService

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
    def __init__(self, *, maker_checker_enabled: bool) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        all_maker_permissions = (
            Permission.REGISTER_COVENANT,
            Permission.VIEW_COVENANT,
            Permission.RECORD_WAIVER,
            Permission.APPROVE_COVENANT,
        )
        self.maker = Principal.user(uuid4(), all_maker_permissions)
        self.checker = Principal.user(
            uuid4(),
            (
                Permission.VIEW_COVENANT,
                Permission.RECORD_WAIVER,
                Permission.APPROVE_COVENANT,
            ),
        )
        self.scopes: dict[UUID, Scope] = {}
        self.portfolio = self._add_portfolio("ROOT")
        self._add_user(self.maker, "maker")
        self._add_user(self.checker, "checker")
        self.scopes[self.maker.id] = Scope.from_paths(self.maker.id, [self.portfolio.path])
        self.scopes[self.checker.id] = Scope.from_paths(self.checker.id, [self.portfolio.path])
        self.facility = self._add_facility(self.portfolio, "000001")
        self.service = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t033-test-000001",
            scope_resolver=lambda principal: self.scopes[principal.id],
            maker_checker_enabled=maker_checker_enabled,
        )

    def _add_portfolio(self, code: str) -> Portfolio:
        portfolio = Portfolio.create(
            code=code,
            name=f"Portfolio {code}",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-portfolio-{code.lower()}",
        )
        self.session.add(portfolio)
        self.session.flush()
        return portfolio

    def _add_user(self, principal: Principal, username: str) -> None:
        self.session.add(
            AppUser(
                id=principal.id,
                username=username,
                email=f"{username}@example.com",
                full_name=username.title(),
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-user-{username}",
            )
        )
        self.session.flush()

    def _add_facility(self, portfolio: Portfolio, suffix: str) -> Facility:
        borrower = Borrower(
            reference=f"B-{suffix}",
            legal_name=f"Borrower {suffix}",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-borrower-{suffix}",
        )
        self.session.add(borrower)
        self.session.flush()
        facility = Facility(
            reference=f"F-{suffix}",
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-facility-{suffix}",
        )
        self.session.add(facility)
        self.session.flush()
        return facility

    def terms(self, **overrides: object) -> CovenantVersionTerms:
        values: dict[str, object] = {
            "definition_ref": "leverage_ratio",
            "custom_formula": None,
            "threshold": Decimal("2.5"),
            "direction": "max",
            "unit": "x",
            "frequency": "quarterly",
            "test_basis": "standalone",
            "effective_from": date(2026, 1, 1),
        }
        values.update(overrides)
        return CovenantVersionTerms(**values)

    def register(self, *, reference: str = "CV-000001") -> object:
        return self.service.register(
            self.maker,
            facility_id=self.facility.id,
            reference=reference,
            name="Leverage covenant",
            covenant_class="financial",
            terms=self.terms(),
            scope=self.scopes[self.maker.id],
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_maker_cannot_approve() -> None:
    bundle = _Bundle(maker_checker_enabled=True)
    try:
        registered = bundle.register()
        assert registered.approval_request is not None

        with pytest.raises(Conflict, match="distinct-actor"):
            bundle.service.approve_covenant(
                bundle.maker,
                registered.covenant.reference,
                approved=True,
                scope=bundle.scopes[bundle.maker.id],
            )
    finally:
        bundle.close()


def test_approved_version_immediately_testable() -> None:
    bundle = _Bundle(maker_checker_enabled=True)
    try:
        registered = bundle.register()
        request = registered.approval_request
        assert request is not None
        bundle.service.decide_approval(
            bundle.checker,
            request.id,
            approved=True,
            scope=bundle.scopes[bundle.checker.id],
        )

        live_versions = bundle.service.live_at(
            bundle.checker,
            bundle.facility.id,
            date(2026, 1, 15),
            scope=bundle.scopes[bundle.checker.id],
        )
        assert [version.id for version in live_versions] == [registered.version.id]
        assert registered.version.status == "live"
    finally:
        bundle.close()


def test_rejected_draft_retained_with_reason() -> None:
    bundle = _Bundle(maker_checker_enabled=True)
    try:
        registered = bundle.register()
        request = registered.approval_request
        assert request is not None
        reason = "The supporting credit evidence is incomplete."
        decided = bundle.service.decide_approval(
            bundle.checker,
            request.id,
            approved=False,
            reason=reason,
            scope=bundle.scopes[bundle.checker.id],
        )
        bundle.session.expire_all()
        version = bundle.session.get(CovenantVersion, registered.version.id)

        assert decided.state.value == "rejected"
        assert decided.reason == reason
        assert version is not None
        assert version.status == "draft"
        assert version.approved_by_id is None
        assert not bundle.service.pending_approvals(
            bundle.checker, scope=bundle.scopes[bundle.checker.id]
        )
    finally:
        bundle.close()


def test_retire_with_open_cure_refused() -> None:
    bundle = _Bundle(maker_checker_enabled=False)
    try:
        registered = bundle.register()
        bundle.session.add(
            CovenantTest(
                id=new_id(),
                covenant_version_id=registered.version.id,
                as_of_date=date(2026, 1, 15),
                value=Decimal("3.0"),
                threshold_used=Decimal("2.5"),
                headroom_pct=Decimal("-20"),
                verdict="breach_cure_open",
                cure_ends_on=date(2026, 2, 15),
                computed_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-cure-test-000001",
            )
        )
        bundle.session.flush()

        with pytest.raises(Conflict, match="breach_cure_open"):
            bundle.service.retire(
                bundle.maker,
                registered.covenant.reference,
                scope=bundle.scopes[bundle.maker.id],
            )

        assert registered.covenant.is_active
        assert registered.version.status == "live"
    finally:
        bundle.close()


def test_permission_enforced_on_api() -> None:
    bundle = _Bundle(maker_checker_enabled=False)
    try:
        unauthorized = Principal.user(uuid4(), ())
        app = create_app(
            routers=(create_covenants_router(bundle.service),),
            principal_resolver=lambda _request: unauthorized,
        )
        with TestClient(app) as client:
            response = client.get("/api/v1/covenants")

        assert response.status_code == 403
        assert "VIEW_COVENANT" in response.text
    finally:
        bundle.close()


def test_scope_enforced_on_list() -> None:
    bundle = _Bundle(maker_checker_enabled=False)
    try:
        other = Principal.user(uuid4(), (Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT))
        bundle._add_user(other, "other-maker")
        other_portfolio = bundle._add_portfolio("OTHER")
        bundle.scopes[other.id] = Scope.from_paths(other.id, [other_portfolio.path])
        other_facility = bundle._add_facility(other_portfolio, "000002")

        first = bundle.register(reference="CV-ROOT")
        second = bundle.service.register(
            other,
            facility_id=other_facility.id,
            reference="CV-OTHER",
            name="Other covenant",
            covenant_class="financial",
            terms=bundle.terms(),
            scope=bundle.scopes[other.id],
        )

        visible = bundle.service.list_covenants(
            bundle.maker,
            scope=bundle.scopes[bundle.maker.id],
        )
        assert [row.id for row in visible] == [first.covenant.id]
        assert second.covenant.id not in {row.id for row in visible}
    finally:
        bundle.close()


def test_every_operation_audited() -> None:
    bundle = _Bundle(maker_checker_enabled=False)
    try:
        registered = bundle.register()
        bundle.service.amend(
            bundle.maker,
            registered.covenant.reference,
            terms=bundle.terms(threshold=Decimal("3.0"), effective_from=date(2026, 4, 1)),
            scope=bundle.scopes[bundle.maker.id],
        )
        waiver = bundle.service.request_waiver(
            bundle.maker,
            registered.covenant.reference,
            from_date=date(2026, 5, 1),
            to_date=date(2026, 6, 30),
            reason="Temporary repayment holiday for an approved restructuring.",
            scope=bundle.scopes[bundle.maker.id],
        )
        bundle.service.approve_waiver(
            bundle.checker,
            waiver.id,
            scope=bundle.scopes[bundle.checker.id],
        )
        bundle.service.retire(
            bundle.maker,
            registered.covenant.reference,
            scope=bundle.scopes[bundle.maker.id],
        )

        event_types = [event[0] for event in bundle.audit.events]
        assert {
            "covenant_registered",
            "covenant_amended",
            "covenant_waiver_requested",
            "covenant_waiver_approved",
            "covenant_retired",
        } <= set(event_types)
        assert len(event_types) == 5
    finally:
        bundle.close()
