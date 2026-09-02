"""Integration coverage for `T-035`'s retest-on-arrival queueing."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantSchedule, CovenantTest
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.calendar import RetestTrigger, RetestTriggerKind, ScheduleState
from covenant_radar.domain.covenants.evaluate import PeriodFacts
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.domain.ratios.compute import RatioResult
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.engine import EngineService
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_TEST_DATE = date(2026, 1, 15)


class _Audit:
    def record(
        self,
        event_type: str,
        subject: object,
        payload: object,
        *,
        actor: object,
        request_id: str,
    ) -> object:
        return object()


class _Bundle:
    """One borrower, one facility, two live covenants: a statement-based
    `leverage_ratio` and a facility-conduct-based `utilisation` — enough to
    tell a borrower-wide trigger (affects both) apart from a facility-conduct
    trigger (affects only the conduct-dependent one)."""

    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(), (Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT)
        )
        portfolio = Portfolio.create(
            code="ROOT",
            name="Root portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t035-portfolio",
        )
        self.session.add(portfolio)
        self.session.flush()
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t035-user",
                email="t035-user@example.com",
                full_name="T035 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t035-user",
            )
        )
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [portfolio.path])
        self.borrower = Borrower(
            reference="B-T035",
            legal_name="T035 Retest Borrower Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t035-borrower",
        )
        self.session.add(self.borrower)
        self.session.flush()
        self.facility = Facility(
            reference="F-T035",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            outstanding=Decimal("700"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t035-facility",
        )
        self.session.add(self.facility)
        self.session.flush()

        self.registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t035-registry",
            maker_checker_enabled=False,
        )
        leverage_registered = self.registry.register(
            self.principal,
            facility_id=self.facility.id,
            reference="CV-T035-LEV",
            name="Leverage ratio",
            covenant_class="financial",
            terms=CovenantVersionTerms(
                definition_ref="leverage_ratio",
                custom_formula=None,
                threshold=Decimal("2.5"),
                direction="max",
                unit="x",
                frequency="quarterly",
                test_basis="standalone",
                effective_from=date(2025, 1, 1),
            ),
            scope=self.scope,
        )
        self.leverage_version = leverage_registered.version
        utilisation_registered = self.registry.register(
            self.principal,
            facility_id=self.facility.id,
            reference="CV-T035-UTIL",
            name="Facility utilisation",
            covenant_class="financial",
            terms=CovenantVersionTerms(
                definition_ref="utilisation",
                custom_formula=None,
                threshold=Decimal("90"),
                direction="max",
                unit="%",
                frequency="quarterly",
                test_basis="standalone",
                effective_from=date(2025, 1, 1),
            ),
            scope=self.scope,
        )
        self.utilisation_version = utilisation_registered.version

        self.service = EngineService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t035-engine",
            scope_resolver=lambda _principal: self.scope,
        )

    def test_leverage(self) -> CovenantTest:
        return self.service.test(
            self.principal,
            covenant_version_id=self.leverage_version.id,
            period=PeriodFacts(
                period_id=new_id(),
                period_label="FY26Q4",
                as_of_date=_TEST_DATE,
            ),
            ratio=RatioResult(
                code="leverage_ratio",
                value=Decimal("3.0"),
                computable=True,
                reason=None,
                inputs_used={
                    "total_debt": Decimal("750"),
                    "tangible_net_worth": Decimal("250"),
                },
                band_breached=False,
            ),
            scope=self.scope,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_restatement_queues_retest_and_keeps_prior() -> None:
    bundle = _Bundle()
    try:
        prior_test = bundle.test_leverage()

        trigger = RetestTrigger(
            kind=RetestTriggerKind.RESTATEMENT,
            as_of_date=_TEST_DATE,
            borrower_id=bundle.borrower.id,
            period_label="FY26Q4",
        )
        queued = bundle.service.queue_retest(bundle.principal, trigger, scope=bundle.scope)

        assert {row.covenant_version_id for row in queued} == {
            bundle.leverage_version.id,
            bundle.utilisation_version.id,
        }
        assert all(row.due_date == _TEST_DATE for row in queued)
        assert all(row.state == ScheduleState.DUE.value for row in queued)

        # The prior test is retained exactly as it was — a restatement never
        # rewrites or deletes a settled fact, it only opens a fresh retest.
        retained = bundle.session.get(CovenantTest, prior_test.id)
        assert retained is not None
        assert retained.value == Decimal("3.0")
        assert retained.verdict == "breach"

        # Both the prior test and the freshly queued retest are visible.
        schedule_rows = bundle.session.scalars(
            select(CovenantSchedule).where(
                CovenantSchedule.covenant_version_id == bundle.leverage_version.id
            )
        ).all()
        assert len(schedule_rows) == 1
        assert schedule_rows[0].state == ScheduleState.DUE.value
    finally:
        bundle.close()


def test_conduct_change_triggers_affected_covenants_only() -> None:
    bundle = _Bundle()
    try:
        trigger = RetestTrigger(
            kind=RetestTriggerKind.CONDUCT,
            as_of_date=date(2026, 1, 20),
            facility_id=bundle.facility.id,
        )
        queued = bundle.service.queue_retest(bundle.principal, trigger, scope=bundle.scope)

        assert len(queued) == 1
        assert queued[0].covenant_version_id == bundle.utilisation_version.id
        assert queued[0].due_date == date(2026, 1, 20)
    finally:
        bundle.close()


def test_retest_queueing_is_idempotent() -> None:
    bundle = _Bundle()
    try:
        trigger = RetestTrigger(
            kind=RetestTriggerKind.CONDUCT,
            as_of_date=date(2026, 1, 20),
            facility_id=bundle.facility.id,
        )

        first = bundle.service.queue_retest(bundle.principal, trigger, scope=bundle.scope)
        second = bundle.service.queue_retest(bundle.principal, trigger, scope=bundle.scope)

        assert [row.id for row in first] == [row.id for row in second]
        total = bundle.session.scalar(
            select(func.count(CovenantSchedule.id)).where(
                CovenantSchedule.covenant_version_id == bundle.utilisation_version.id,
                CovenantSchedule.due_date == date(2026, 1, 20),
            )
        )
        assert total == 1
    finally:
        bundle.close()
