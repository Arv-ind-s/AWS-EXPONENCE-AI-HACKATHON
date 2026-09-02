"""Integration coverage for stage-2 trace emission from the covenant engine."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.core.ids import new_id
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import TraceRow
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import CovenantTest
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.covenants.evaluate import PeriodFacts
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.domain.ratios.compute import RatioResult
from covenant_radar.domain.trace import stage_record
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.engine import EngineService
from covenant_radar.services.registry import RegistryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


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
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.principal = Principal.user(
            uuid4(),
            (Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT),
        )
        portfolio = Portfolio.create(
            code="ROOT",
            name="Root portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t037-portfolio",
        )
        self.session.add(portfolio)
        self.session.flush()
        self.session.add(
            AppUser(
                id=self.principal.id,
                username="t037-user",
                email="t037-user@example.com",
                full_name="T037 User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-t037-user",
            )
        )
        self.session.flush()
        self.scope = Scope.from_paths(self.principal.id, [portfolio.path])
        borrower = Borrower(
            reference="B-T037",
            legal_name="T037 Trace Borrower Private Limited",
            portfolio_id=portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t037-borrower",
        )
        self.session.add(borrower)
        self.session.flush()
        facility = Facility(
            reference="F-T037",
            borrower_id=borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("1000"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            outstanding=Decimal("700"),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t037-facility",
        )
        self.session.add(facility)
        self.session.flush()
        self.registry = RegistryService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t037-registry",
            maker_checker_enabled=False,
        )
        terms = CovenantVersionTerms(
            definition_ref="leverage_ratio",
            custom_formula=None,
            threshold=Decimal("2.5"),
            direction="max",
            unit="x",
            frequency="quarterly",
            test_basis="standalone",
            effective_from=date(2025, 1, 1),
        )
        registered = self.registry.register(
            self.principal,
            facility_id=facility.id,
            reference="CV-T037",
            name="Leverage ratio",
            covenant_class="financial",
            terms=terms,
            scope=self.scope,
        )
        self.version = registered.version
        self.service = EngineService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-t037-engine",
            scope_resolver=lambda _principal: self.scope,
        )

    def test(self, *, period_id: UUID | None = None) -> CovenantTest:
        return self.service.test(
            self.principal,
            covenant_version_id=self.version.id,
            period=PeriodFacts(
                period_id=period_id if period_id is not None else new_id(),
                period_label="FY26Q4",
                as_of_date=date(2026, 1, 15),
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


def test_every_test_writes_one_stage2_row() -> None:
    bundle = _Bundle()
    try:
        first = bundle.test()
        second = bundle.test()

        rows = bundle.session.scalars(
            select(TraceRow)
            .where(TraceRow.subject_type == "covenant_test")
            .order_by(TraceRow.occurred_at, TraceRow.id)
        ).all()
        assert len(rows) == 2
        assert [row.subject_id for row in rows] == [first.id, second.id]
        assert [row.stage for row in rows] == ["2", "2"]
    finally:
        bundle.close()


def test_row_names_threshold_value_observed_and_side() -> None:
    bundle = _Bundle()
    try:
        test = bundle.test()
        row = bundle.session.scalar(
            select(TraceRow).where(
                TraceRow.subject_type == "covenant_test", TraceRow.subject_id == test.id
            )
        )

        assert row is not None
        assert row.thresholds_compared == [
            {
                "name": "covenant_threshold",
                "value": "2.5",
                "observed": "3.0",
                "side": "above",
            }
        ]
        assert row.outputs["verdict"] == "breach"
        assert row.rule_or_prompt_version == "covenant.engine.v1"
    finally:
        bundle.close()


def test_duplicate_stage_writes_are_retained_and_latest_is_read() -> None:
    bundle = _Bundle()
    try:
        test = bundle.test()
        repository = TraceRepository(bundle.session, clock=FixedClock(_NOW))
        subject = ("covenant_test", test.id)
        repository.write(
            subject,
            stage_record(
                2,
                "code",
                {"source": "rerun-1"},
                {"verdict": "warning"},
                "covenant.engine.v1",
                [],
                Decimal("1"),
                [],
            ),
        )
        repository.write(
            subject,
            stage_record(
                2,
                "code",
                {"source": "rerun-2"},
                {"verdict": "breach"},
                "covenant.engine.v1",
                [],
                Decimal("1"),
                [],
            ),
        )

        visible = repository.read(subject)
        history = repository.history(subject, stage=2)
        assert visible[1].outputs["verdict"] == "breach"
        assert len(history) == 3
        assert history[0].outputs["verdict"] == "breach"
        assert history[1].outputs["verdict"] == "warning"
        assert history[-1].outputs["verdict"] == "breach"
    finally:
        bundle.close()
