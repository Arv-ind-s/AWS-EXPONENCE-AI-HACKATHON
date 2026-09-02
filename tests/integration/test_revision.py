"""Integration coverage for durable evidence revision and reconstruction."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, EvidenceItem, Portfolio
from covenant_radar.db.models.signal import EvidenceTransition
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.signals.evidence import SignalEventFacts
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ledger import LedgerService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object]]] = []

    def record(
        self,
        event_type: str,
        subject: object,
        payload: object,
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del actor, request_id
        self.events.append((event_type, subject, dict(payload)))
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.user_id = uuid4()
        self.portfolio = Portfolio.create(
            code="REVISION",
            name="Revision portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-revision-portfolio",
        )
        self.session.add(self.portfolio)
        self.session.flush()
        self.session.add(
            AppUser(
                id=self.user_id,
                username="revision-user",
                email="revision@example.com",
                full_name="Revision User",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-revision-user",
            )
        )
        self.borrower = Borrower(
            reference="B-REVISION",
            legal_name="Revision Borrower Private Limited",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-revision-borrower",
        )
        self.session.add(self.borrower)
        self.session.flush()
        self.scope = Scope.from_paths(self.user_id, [self.portfolio.path])
        self.principal = Principal.user(
            self.user_id,
            (Permission.INGEST_DATA, Permission.VIEW_EVIDENCE),
        )
        self.service = LedgerService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-revision-service",
        )

    def add_prior(self) -> EvidenceItem:
        item = EvidenceItem(
            id=uuid4(),
            borrower_id=self.borrower.id,
            facility_id=None,
            family="payment",
            evidence_type="payment_delay",
            first_seen=date(2026, 8, 1),
            last_seen=date(2026, 8, 1),
            persistence_days=14,
            event_count_window=3,
            materiality_pct=Decimal("10"),
            decay_factor=Decimal("1"),
            state="sustained",
            counts_toward_pressure=True,
            source_event_ids=["delay-1"],
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-revision-prior",
        )
        self.session.add(item)
        self.session.flush()
        return item

    def received(self, event_date: date = date(2026, 8, 2)) -> SignalEventFacts:
        return SignalEventFacts(
            borrower_id=self.borrower.id,
            facility_id=None,
            event_date=event_date,
            family="payment",
            event_type="payment_received",
            magnitude=Decimal("0"),
            payload={"is_adverse": False},
            event_id="received-1",
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_point_in_time_read_returns_prior_state() -> None:
    fixture = _Fixture()
    try:
        prior = fixture.add_prior()
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [fixture.received()],
            as_of=date(2026, 8, 2),
            scope=fixture.scope,
        )
        fixture.session.commit()

        before = fixture.service.read_as_of(
            fixture.principal,
            fixture.borrower.id,
            date(2026, 8, 1),
            scope=fixture.scope,
        )
        assert len(before) == 1
        assert before[0].id == prior.id
        assert before[0].state == "sustained"
        assert before[0].superseded_by_id is None
    finally:
        fixture.close()


def test_risk_view_revises_after_contradiction() -> None:
    fixture = _Fixture()
    try:
        prior = fixture.add_prior()
        revision = fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [fixture.received()],
            as_of=date(2026, 8, 2),
            scope=fixture.scope,
        )
        fixture.session.commit()

        current = fixture.service.read_as_of(
            fixture.principal,
            fixture.borrower.id,
            date(2026, 8, 2),
            scope=fixture.scope,
        )
        assert revision.changed is True
        assert {item.state for item in current} == {"superseded", "transient"}
        assert next(item for item in current if item.id == prior.id).counts_toward_pressure is False
        assert any(event[0] == "evidence_superseded" for event in fixture.audit.events)
    finally:
        fixture.close()


def test_nothing_is_ever_deleted() -> None:
    fixture = _Fixture()
    try:
        prior = fixture.add_prior()
        fixture.service.revise(
            fixture.principal,
            fixture.borrower.id,
            [fixture.received()],
            as_of=date(2026, 8, 2),
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert (
            fixture.session.scalar(
                select(func.count(EvidenceItem.id)).where(
                    EvidenceItem.borrower_id == prior.borrower_id
                )
            )
            == 2
        )
        assert fixture.session.scalar(select(func.count(EvidenceTransition.id))) == 1
    finally:
        fixture.close()
