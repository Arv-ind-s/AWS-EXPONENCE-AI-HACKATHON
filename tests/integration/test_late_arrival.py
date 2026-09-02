"""Integration checks for T-044 late signal handling."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, Facility, Portfolio, SignalEvent
from covenant_radar.db.scoping import Scope
from covenant_radar.ingestion.signals.watermark import (
    InMemoryRecomputationQueue,
    InMemoryWatermarkStore,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ingestion import SignalIngestionService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_WATERMARK_DATE = date(2026, 1, 10)


class _Audit:
    def record(
        self,
        event_type: str,
        subject: object,
        payload: dict[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        del event_type, subject, payload, actor, request_id
        return object()


class _Fixture:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.source_id = uuid4()
        self.user = AppUser(
            id=uuid4(),
            username="late-arrival",
            email="late-arrival@example.com",
            full_name="Late Arrival",
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-late-test-0001",
        )
        self.portfolio = Portfolio.create(
            code="LATE",
            name="Late Arrival Portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-late-test-0002",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-LATE-001",
            legal_name="Late Arrival Borrower",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-late-test-0003",
        )
        self.facility = Facility(
            id=uuid4(),
            reference="F-LATE-001",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("100"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-late-test-0004",
        )
        self.session.add_all([self.user, self.portfolio, self.borrower, self.facility])
        self.session.flush()
        self.principal = Principal.user(self.user.id, (Permission.INGEST_DATA,))
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])

    def service(
        self,
        watermark_store: InMemoryWatermarkStore,
        recomputation_queue: InMemoryRecomputationQueue,
        *,
        forecast_exists: Callable[[UUID], bool] | None = None,
    ) -> SignalIngestionService:
        return SignalIngestionService(
            self.session,
            audit=_Audit(),
            clock=FixedClock(_NOW),
            request_id="rq-late-test-0005",
            watermark_store=watermark_store,
            recomputation_queue=recomputation_queue,
            forecast_exists=forecast_exists,
        )

    def event(self, event_date: date, *, value: int = 3) -> dict[str, object]:
        return {
            "borrower_id": self.borrower.id,
            "facility_id": self.facility.id,
            "event_date": event_date,
            "family": "payment",
            "event_type": "payment_delay",
            "magnitude": Decimal(value),
            "unit": "days",
            "payload": {"days_past_due": value, "is_adverse": True},
            "source_id": self.source_id,
        }

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_event_at_watermark_not_late() -> None:
    fixture = _Fixture()
    store = InMemoryWatermarkStore()
    queue = InMemoryRecomputationQueue()
    store.advance(fixture.source_id, _WATERMARK_DATE)
    try:
        fixture.service(store, queue).ingest(
            fixture.principal,
            [fixture.event(_WATERMARK_DATE)],
            scope=fixture.scope,
        )
        fixture.session.commit()

        row = fixture.session.scalar(select(SignalEvent))
        assert row is not None
        assert row.is_late is False
        assert queue.requests == ()
        assert store.get(fixture.source_id) == _WATERMARK_DATE
    finally:
        fixture.close()


def test_late_event_stored_and_marked() -> None:
    fixture = _Fixture()
    store = InMemoryWatermarkStore()
    queue = InMemoryRecomputationQueue()
    store.advance(fixture.source_id, _WATERMARK_DATE)
    try:
        report = fixture.service(store, queue, forecast_exists=lambda _borrower_id: True).ingest(
            fixture.principal,
            [fixture.event(date(2026, 1, 9))],
            scope=fixture.scope,
        )
        fixture.session.commit()

        row = fixture.session.scalar(select(SignalEvent))
        assert report.inserted == 1
        assert row is not None
        assert row.is_late is True
        assert len(queue.requests) == 1
        assert queue.requests[0].borrower_id == fixture.borrower.id
        assert queue.requests[0].start_date == date(2026, 1, 9)
        assert queue.requests[0].end_date == _WATERMARK_DATE
    finally:
        fixture.close()


def test_late_events_coalesce_to_one_request() -> None:
    fixture = _Fixture()
    store = InMemoryWatermarkStore()
    queue = InMemoryRecomputationQueue()
    store.advance(fixture.source_id, _WATERMARK_DATE)
    events = [fixture.event(date(2026, 1, day), value=day) for day in range(1, 10)]
    events.append(fixture.event(date(2026, 1, 9), value=10))
    try:
        report = fixture.service(store, queue, forecast_exists=lambda _borrower_id: True).ingest(
            fixture.principal, events, scope=fixture.scope
        )
        fixture.session.commit()

        assert report.inserted == 10
        assert len(queue.requests) == 1
        assert queue.requests[0].start_date == date(2026, 1, 1)
        assert queue.requests[0].end_date == _WATERMARK_DATE
        assert len(queue.late_arrivals) == 10
    finally:
        fixture.close()


def test_no_forecast_records_reason() -> None:
    fixture = _Fixture()
    store = InMemoryWatermarkStore()
    queue = InMemoryRecomputationQueue()
    store.advance(fixture.source_id, _WATERMARK_DATE)
    try:
        fixture.service(store, queue).ingest(
            fixture.principal,
            [fixture.event(date(2026, 1, 9))],
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert queue.requests == ()
        assert len(queue.no_forecast_records) == 1
        record = queue.no_forecast_records[0]
        assert record.recomputation_queued is False
        assert "no forecast exists" in record.reason
    finally:
        fixture.close()


def test_watermark_never_regresses() -> None:
    fixture = _Fixture()
    store = InMemoryWatermarkStore()
    queue = InMemoryRecomputationQueue()
    try:
        service = fixture.service(store, queue)
        service.ingest(
            fixture.principal,
            [fixture.event(date(2026, 1, 20))],
            scope=fixture.scope,
        )
        fixture.session.commit()
        service.ingest(
            fixture.principal,
            [fixture.event(date(2026, 1, 10), value=4)],
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert store.get(fixture.source_id) == date(2026, 1, 20)
        assert len(queue.no_forecast_records) == 1
        assert queue.no_forecast_records[0].watermark == date(2026, 1, 20)
    finally:
        fixture.close()
