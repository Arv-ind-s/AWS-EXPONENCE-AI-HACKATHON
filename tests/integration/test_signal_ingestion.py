"""Integration checks for the T-042 signal ingestion vertical slice."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, Facility, Portfolio, SignalEvent
from covenant_radar.db.scoping import Scope
from covenant_radar.ingestion.signals.framework import InMemorySignalQuarantine
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ingestion import SignalIngestionService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, dict[str, object]]] = []

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
        self.events.append((event_type, subject, payload))
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.audit = _Audit()
        self.user = AppUser(
            id=uuid4(),
            username="signal-ingest",
            email="signal-ingest@example.com",
            full_name="Signal Ingest",
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-signal-test-0001",
        )
        self.portfolio = Portfolio.create(
            code="SIGNALS",
            name="Signal Portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-signal-test-0002",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-SIGNAL-001",
            legal_name="Signal Test Borrower",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-signal-test-0003",
        )
        self.facility = Facility(
            id=uuid4(),
            reference="F-SIGNAL-001",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("100"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-signal-test-0004",
        )
        self.session.add_all([self.user, self.portfolio, self.borrower, self.facility])
        self.session.flush()
        self.principal = Principal.user(self.user.id, (Permission.INGEST_DATA,))
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.quarantine = InMemorySignalQuarantine()
        self.service = SignalIngestionService(
            self.session,
            audit=self.audit,
            clock=FixedClock(_NOW),
            request_id="rq-signal-test-0005",
            quarantine=self.quarantine,
        )

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()

    def event(
        self,
        family: str = "payment",
        *,
        borrower_id: UUID | None = None,
        facility_id: UUID | None = None,
    ) -> dict[str, object]:
        definitions = {
            "payment": ("payment_delay", "days", Decimal("3"), {"days_past_due": 3}),
            "utilisation": (
                "facility_utilisation",
                "%",
                Decimal("60.00"),
                {"utilisation_pct": Decimal("60.00")},
            ),
            "treasury": (
                "treasury_outflow",
                "ratio",
                Decimal("0.10"),
                {"cash_outflow_ratio": Decimal("0.10")},
            ),
            "concentration": (
                "concentration_exposure",
                "%",
                Decimal("20.00"),
                {"top_group_exposure_pct": Decimal("20.00")},
            ),
            "industry": (
                "industry_indicator",
                "score",
                Decimal("0.10"),
                {"industry_stress_score": Decimal("0.10")},
            ),
            "news": (
                "news_event",
                "score",
                Decimal("0.10"),
                {"news_risk_score": Decimal("0.10")},
            ),
        }
        event_type, unit, magnitude, payload = definitions[family]
        payload["is_adverse"] = False
        return {
            "borrower_id": borrower_id or self.borrower.id,
            "facility_id": facility_id or self.facility.id,
            "event_date": date(2026, 1, 1),
            "family": family,
            "event_type": event_type,
            "magnitude": magnitude,
            "unit": unit,
            "payload": payload,
            "source_id": uuid4(),
        }


def test_batch_inserts_and_counts() -> None:
    fixture = _Fixture()
    try:
        families = ("payment", "utilisation", "treasury", "concentration", "industry", "news")
        events = [fixture.event(family) for family in families]
        report = fixture.service.ingest(fixture.principal, events, scope=fixture.scope)
        fixture.session.commit()

        assert (report.inserted, report.duplicates, report.rejected) == (6, 0, 0)
        assert report.reconciled
        assert fixture.session.scalar(select(func.count(SignalEvent.id))) == 6
        assert {row.family for row in fixture.session.scalars(select(SignalEvent))} == {
            "payment",
            "utilisation",
            "treasury",
            "concentration",
            "industry",
            "news",
        }
    finally:
        fixture.close()


def test_duplicate_counted_not_errored() -> None:
    fixture = _Fixture()
    try:
        event = fixture.event()
        first = fixture.service.ingest(fixture.principal, [event], scope=fixture.scope)
        fixture.session.commit()
        second = fixture.service.ingest(fixture.principal, [event], scope=fixture.scope)
        fixture.session.commit()

        assert first.inserted == 1
        assert second.inserted == 0
        assert second.duplicates == 1
        assert second.rejected == 0
        assert fixture.session.scalar(select(func.count(SignalEvent.id))) == 1
    finally:
        fixture.close()


def test_unknown_type_quarantined_rest_proceed() -> None:
    fixture = _Fixture()
    try:
        invalid = fixture.event()
        invalid["event_type"] = "not_a_real_signal"
        report = fixture.service.ingest(
            fixture.principal, [invalid, fixture.event("news")], scope=fixture.scope
        )
        fixture.session.commit()

        assert (report.inserted, report.rejected) == (1, 1)
        assert len(fixture.quarantine.signals) == 1
        assert "requires event type" in fixture.quarantine.signals[0].reason
        assert fixture.session.scalar(select(func.count(SignalEvent.id))) == 1
    finally:
        fixture.close()


def test_unknown_borrower_quarantined() -> None:
    fixture = _Fixture()
    try:
        report = fixture.service.ingest(
            fixture.principal,
            [fixture.event(borrower_id=uuid4()), fixture.event("news")],
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert (report.inserted, report.rejected) == (1, 1)
        assert "Unknown borrower" in fixture.quarantine.signals[0].reason
        assert fixture.session.scalar(select(func.count(SignalEvent.id))) == 1
    finally:
        fixture.close()


def test_mid_batch_failure_commits_nothing() -> None:
    fixture = _Fixture()

    def source() -> Iterator[dict[str, object]]:
        yield fixture.event()
        raise RuntimeError("source failed mid-batch")

    try:
        with pytest.raises(RuntimeError, match="source failed mid-batch"):
            fixture.service.ingest(fixture.principal, source(), scope=fixture.scope)
        assert fixture.session.scalar(select(func.count(SignalEvent.id))) == 0
        assert fixture.quarantine.signals == ()
    finally:
        fixture.close()
