"""Integration coverage for the durable waiver lifecycle (`T-032`)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantTest
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.exceptions import resolve_waiver
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
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.requester = Principal.user(
            uuid4(),
            (Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT, Permission.RECORD_WAIVER),
        )
        self.approver = Principal.user(
            uuid4(),
            (Permission.VIEW_COVENANT, Permission.RECORD_WAIVER),
        )

        portfolio = Portfolio.create(
            code="ROOT",
            name="Root",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-waiver-portfolio",
        )
        self.session.add(portfolio)
        self.session.flush()
        self.requester_scope = Scope.from_paths(self.requester.id, [portfolio.path])
        self.approver_scope = Scope.from_paths(self.approver.id, [portfolio.path])

        borrower = Borrower(
            reference="B-000001",
            legal_name="Acme Pvt Ltd",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-waiver-borrower",
        )
        self.session.add(borrower)
        self.session.flush()
        facility = Facility(
            reference="F-000001",
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-waiver-facility",
        )
        self.session.add(facility)
        self.session.flush()
        self.facility = facility

        self.session.add_all(
            [
                AppUser(
                    id=self.requester.id,
                    username="waiver-requester",
                    email="waiver-requester@example.com",
                    full_name="Waiver Requester",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-waiver-user-requester",
                ),
                AppUser(
                    id=self.approver.id,
                    username="waiver-approver",
                    email="waiver-approver@example.com",
                    full_name="Waiver Approver",
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-waiver-user-approver",
                ),
            ]
        )
        self.session.flush()
        self.service = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t032-waiver-000001",
            maker_checker_enabled=False,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def register_covenant(self) -> object:
        terms = CovenantVersionTerms(
            definition_ref="leverage_ratio",
            custom_formula=None,
            threshold=Decimal("2.5"),
            direction="max",
            unit="x",
            frequency="monthly",
            test_basis="standalone",
            effective_from=date(2026, 1, 1),
        )
        return self.service.register(
            self.requester,
            facility_id=self.facility.id,
            reference="CV-000001",
            name="Leverage covenant",
            covenant_class="financial",
            terms=terms,
            scope=self.requester_scope,
        )


def test_unapproved_waiver_not_in_force() -> None:
    bundle = _Bundle()
    try:
        registered = bundle.register_covenant()
        waiver = bundle.service.request_waiver(
            bundle.requester,
            registered.covenant.reference,
            from_date=date(2026, 4, 1),
            to_date=date(2026, 6, 30),
            reason="Temporary approved restructuring request is under review.",
            scope=bundle.requester_scope,
        )

        covenant = SimpleNamespace(id=registered.covenant.id, waivers=(waiver,))
        assert resolve_waiver(covenant, date(2026, 5, 1)) is None
        assert waiver.state == "requested"
        assert waiver.approved_by_id is None
    finally:
        bundle.close()


def test_approved_waiver_named_in_test_record() -> None:
    bundle = _Bundle()
    try:
        registered = bundle.register_covenant()
        waiver = bundle.service.request_waiver(
            bundle.requester,
            registered.covenant.reference,
            from_date=date(2026, 4, 1),
            to_date=date(2026, 6, 30),
            reason="Temporary repayment holiday approved for two quarters.",
            scope=bundle.requester_scope,
        )
        approved = bundle.service.approve_waiver(
            bundle.approver,
            waiver.id,
            scope=bundle.approver_scope,
        )

        covenant = SimpleNamespace(id=registered.covenant.id, waivers=(approved,))
        active = resolve_waiver(covenant, date(2026, 6, 30))
        assert active is not None
        assert active.id == approved.id
        assert active.state == "approved"

        test = CovenantTest(
            id=new_id(),
            covenant_version_id=registered.version.id,
            as_of_date=date(2026, 6, 30),
            value=Decimal("2.7"),
            threshold_used=Decimal("2.5"),
            headroom_pct=Decimal("-8"),
            verdict="breach",
            waiver_id=approved.id,
            computed_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-waiver-test",
        )
        bundle.session.add(test)
        bundle.session.flush()
        assert test.waiver_id == approved.id
        assert approved.covenant_id == registered.covenant.id
    finally:
        bundle.close()


def test_waiver_lifecycle_audited() -> None:
    bundle = _Bundle()
    try:
        registered = bundle.register_covenant()
        requested = bundle.service.request_waiver(
            bundle.requester,
            registered.covenant.reference,
            from_date=date(2026, 4, 1),
            to_date=date(2026, 6, 30),
            reason="A waiver request that will be rejected.",
            scope=bundle.requester_scope,
        )
        bundle.service.reject_waiver(
            bundle.approver,
            requested.id,
            reason="Supporting approval evidence was not supplied.",
            scope=bundle.approver_scope,
        )
        approved_request = bundle.service.request_waiver(
            bundle.requester,
            registered.covenant.reference,
            from_date=date(2026, 7, 1),
            to_date=date(2026, 9, 30),
            reason="A separate waiver request that will be approved.",
            scope=bundle.requester_scope,
        )
        bundle.service.approve_waiver(
            bundle.approver,
            approved_request.id,
            scope=bundle.approver_scope,
        )

        event_types = [event[0] for event in bundle.audit.events]
        assert event_types == [
            "covenant_registered",
            "covenant_waiver_requested",
            "covenant_waiver_rejected",
            "covenant_waiver_requested",
            "covenant_waiver_approved",
        ]
        assert bundle.audit.events[2][2]["rejection_reason"] == (
            "Supporting approval evidence was not supplied."
        )
        assert bundle.audit.events[-1][2]["approved_by_id"] == str(bundle.approver.id)
    finally:
        bundle.close()
