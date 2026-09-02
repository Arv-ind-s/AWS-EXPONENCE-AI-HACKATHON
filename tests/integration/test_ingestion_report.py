"""Integration checks for T-045 signal ingestion reporting."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models import AppUser, Borrower, Facility, ImportBatch, Portfolio
from covenant_radar.db.models.statements import QuarantineRow
from covenant_radar.db.repositories.ingestion import SignalIngestionRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ingestion import SignalIngestionReport, SignalIngestionService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


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
        self.user = AppUser(
            id=uuid4(),
            username="ingestion-report",
            email="ingestion-report@example.com",
            full_name="Ingestion Report",
            auth_source="local",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-report-test-0001",
        )
        self.portfolio = Portfolio.create(
            code="REPORT",
            name="Ingestion Report Portfolio",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-report-test-0002",
        )
        self.borrower = Borrower(
            id=uuid4(),
            reference="B-REPORT-001",
            legal_name="Ingestion Report Borrower",
            portfolio_id=self.portfolio.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-report-test-0003",
        )
        self.facility = Facility(
            id=uuid4(),
            reference="F-REPORT-001",
            borrower_id=self.borrower.id,
            facility_type="term_loan",
            sanctioned_limit=Decimal("100"),
            currency="INR",
            sanction_date=date(2025, 1, 1),
            effective_from=date(2025, 1, 1),
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-report-test-0004",
        )
        self.session.add_all([self.user, self.portfolio, self.borrower, self.facility])
        self.session.flush()
        self.principal = Principal.user(
            self.user.id,
            (Permission.INGEST_DATA, Permission.RESOLVE_QUARANTINE),
        )
        self.scope = Scope.from_paths(self.principal.id, [self.portfolio.path])
        self.service = SignalIngestionService(
            self.session,
            audit=_Audit(),
            clock=FixedClock(_NOW),
            request_id="rq-report-test-0005",
        )

    def event(self, *, event_date: date = date(2026, 1, 14)) -> dict[str, object]:
        return {
            "borrower_id": self.borrower.id,
            "facility_id": self.facility.id,
            "event_date": event_date,
            "family": "payment",
            "event_type": "payment_delay",
            "magnitude": Decimal("3"),
            "unit": "days",
            "payload": {"days_past_due": 3, "is_adverse": True},
            "source_id": self.source_id,
        }

    @property
    def source_id(self) -> UUID:
        if not hasattr(self, "_source_id"):
            self._source_id = uuid4()
        return self._source_id

    def close(self) -> None:
        self.session.close()
        self.engine.dispose()


def test_report_written_even_with_no_rejects() -> None:
    fixture = _Fixture()
    try:
        report = fixture.service.ingest(
            fixture.principal,
            [fixture.event()],
            scope=fixture.scope,
            source_reference="bank-api",
            source_as_of_date=date(2026, 1, 14),
        )
        fixture.session.commit()

        batch = fixture.session.get(ImportBatch, report.batch_id)
        assert batch is not None
        assert report.rejected == 0
        assert report.family_volumes["payment"] == 1
        assert report.source_lag[0].lag_days == 1
        assert batch.report == report.as_dict()
        assert SignalIngestionReport.from_dict(batch.report) == report
    finally:
        fixture.close()


def test_dominant_reason_flagged() -> None:
    fixture = _Fixture()
    try:
        invalid_rows = []
        for _ in range(4):
            row = fixture.event()
            row["event_type"] = "not_a_real_signal"
            invalid_rows.append(row)
        report = fixture.service.ingest(
            fixture.principal,
            [*invalid_rows, fixture.event()],
            scope=fixture.scope,
        )
        fixture.session.commit()

        assert report.rejected == 4
        assert report.dominant_reason_flagged is True
        assert report.probable_mapping_error is True
        assert report.top_rejection_reasons[0].count == 4
        assert report.top_rejection_reasons[0].dominant is True
        assert fixture.session.scalars(select(QuarantineRow)).all()
        stored_batch = fixture.session.get(ImportBatch, report.batch_id)
        assert stored_batch is not None
        assert len(stored_batch.report["quarantine"]["sample"]) == 4
    finally:
        fixture.close()


def test_resolution_does_not_alter_prior_report() -> None:
    fixture = _Fixture()
    try:
        invalid = fixture.event()
        invalid["event_type"] = "not_a_real_signal"
        report = fixture.service.ingest(
            fixture.principal, [invalid], scope=fixture.scope
        )
        fixture.session.commit()
        batch = fixture.session.get(ImportBatch, report.batch_id)
        assert batch is not None
        original_report = dict(batch.report)
        quarantine_row = fixture.session.scalars(select(QuarantineRow)).one()

        repository = SignalIngestionRepository(fixture.session)
        resolved = repository.resolve_quarantine(
            fixture.principal,
            quarantine_row.id,
            reason="Source owner supplied the corrected event type.",
            resolved_at=_NOW,
            request_id="rq-report-test-resolve",
        )
        fixture.session.commit()

        assert resolved.resolved_at == _NOW
        assert resolved.resolution == "Source owner supplied the corrected event type."
        assert batch.report == original_report
        assert repository.get_report(report.batch_id) == original_report
    finally:
        fixture.close()


def test_counts_exposed_for_metrics() -> None:
    fixture = _Fixture()
    try:
        event = fixture.event()
        first = fixture.service.ingest(fixture.principal, [event], scope=fixture.scope)
        fixture.session.commit()
        second = fixture.service.ingest(fixture.principal, [event], scope=fixture.scope)
        fixture.session.commit()
        invalid = fixture.event(event_date=date(2026, 1, 13))
        invalid["event_type"] = "not_a_real_signal"
        fixture.service.ingest(fixture.principal, [invalid], scope=fixture.scope)
        fixture.session.commit()

        metrics = SignalIngestionRepository(fixture.session).metrics()
        assert first.inserted == 1
        assert second.duplicates == 1
        assert metrics == {
            "runs": 3,
            "received": 3,
            "accepted": 2,
            "inserted": 1,
            "duplicates": 1,
            "rejected": 1,
            "open_quarantine": 1,
            "resolved_quarantine": 0,
        }
    finally:
        fixture.close()
