"""Integration coverage for `T-132`'s CRILC export and weekly default report."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.reporting.crilc import CrilcReportType
from covenant_radar.reporting.layouts.crilc import available_layout_versions, load_crilc_layout
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.reporting import CrilcReportService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_AS_OF = date(2026, 8, 31)

_MINIMAL_V1_LAYOUT = """{
  "report_type": "crilc_monthly",
  "version": 1,
  "effective_from": "2019-06-07",
  "fields": [
    {"name": "as_of_date", "label": "As Of", "data_type": "date", "required": true},
    {
      "name": "borrower_reference", "label": "Borrower Reference",
      "data_type": "string", "required": true, "max_length": 20
    }
  ]
}"""

_MINIMAL_V2_LAYOUT = """{
  "report_type": "crilc_monthly",
  "version": 2,
  "effective_from": "2026-01-01",
  "fields": [
    {"name": "as_of_date", "label": "As Of", "data_type": "date", "required": true},
    {
      "name": "borrower_reference", "label": "Borrower Reference",
      "data_type": "string", "required": true, "max_length": 20
    },
    {
      "name": "legal_name", "label": "Legal Name",
      "data_type": "string", "required": true, "max_length": 300
    }
  ]
}"""


def _new_session() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            Portfolio.__table__,
            Borrower.__table__,
            Facility.__table__,
            FacilityConduct.__table__,
            AuditEvent.__table__,
        ],
    )
    return engine, Session(engine)


def _portfolio(session: Session) -> Portfolio:
    portfolio = Portfolio.create(
        code="ROOT", name="Root", created_at=_NOW, updated_at=_NOW, request_id="rq-t132-portfolio"
    )
    session.add(portfolio)
    session.flush()
    return portfolio


def _borrower(
    session: Session,
    portfolio: Portfolio,
    reference: str,
    *,
    industry_code: str | None = "MFG",
    constitution: str | None = "private_limited",
) -> Borrower:
    borrower = Borrower(
        reference=reference,
        legal_name=f"{reference} Private Limited",
        portfolio_id=portfolio.id,
        industry_code=industry_code,
        constitution=constitution,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t132-{reference}",
    )
    session.add(borrower)
    session.flush()
    return borrower


def _facility(
    session: Session,
    borrower: Borrower,
    reference: str,
    *,
    sanctioned_limit: Decimal,
    outstanding: Decimal | None,
    currency: str = "INR",
    effective_from: date = date(2025, 1, 1),
    effective_to: date | None = None,
) -> Facility:
    facility = Facility(
        reference=reference,
        borrower_id=borrower.id,
        facility_type="term_loan",
        sanctioned_limit=sanctioned_limit,
        currency=currency,
        outstanding=outstanding,
        sanction_date=effective_from,
        effective_from=effective_from,
        effective_to=effective_to,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t132-{reference}",
    )
    session.add(facility)
    session.flush()
    return facility


def _conduct(
    session: Session,
    facility: Facility,
    as_of_date: date,
    *,
    days_past_due: int,
    overdue_amount: Decimal,
) -> FacilityConduct:
    conduct = FacilityConduct(
        facility_id=facility.id,
        as_of_date=as_of_date,
        days_past_due=days_past_due,
        overdue_amount=overdue_amount,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t132-conduct-{facility.reference}",
    )
    session.add(conduct)
    session.flush()
    return conduct


def _principal() -> Principal:
    return Principal.user(uuid4(), (Permission.EXPORT_EVIDENCE,))


def _service(session: Session, *, clock: FixedClock | None = None) -> CrilcReportService:
    resolved_clock = clock or FixedClock(_NOW)
    audit = AuditRecorder(
        AuditRepository(session), clock=resolved_clock, request_id="rq-t132-service"
    )
    return CrilcReportService(
        session,
        audit=audit,
        clock=resolved_clock,
        scope_resolver=lambda principal: Scope.empty(principal.id),
        request_id="rq-t132-service",
    )


def test_validates_against_layout() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000001")
        facility = _facility(
            session,
            borrower,
            "F-000001",
            sanctioned_limit=Decimal("60000000.0000"),
            outstanding=Decimal("55000000.0000"),
        )
        _conduct(session, facility, _AS_OF, days_past_due=10, overdue_amount=Decimal("100000.0000"))
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        result = _service(session).generate(
            principal, report_type=CrilcReportType.MONTHLY, as_of_date=_AS_OF, scope=scope
        )

        assert result.report.row_count == 1
        row = result.report.rows[0]
        assert result.report.layout.validate_row(row) == ()
        assert row["borrower_reference"] == "B-000001"
        assert row["aggregate_exposure_amount"] == Decimal("60000000")
        assert row["sma_band"] == "SMA-0"
        assert row["days_past_due"] == 10
        assert row["overdue_amount"] == Decimal("100000")
        assert result.layout_version == 1
    finally:
        session.close()
        engine.dispose()


def test_below_threshold_excluded_and_counted() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000002")
        _facility(
            session,
            borrower,
            "F-000002",
            sanctioned_limit=Decimal("40000000.0000"),
            outstanding=Decimal("35000000.0000"),
        )
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        result = _service(session).generate(
            principal, report_type=CrilcReportType.MONTHLY, as_of_date=_AS_OF, scope=scope
        )

        assert result.report.row_count == 0
        reconciliation = result.report.reconciliation
        assert reconciliation.total_considered == 1
        assert reconciliation.included == 0
        assert reconciliation.excluded_below_threshold == 1
        assert reconciliation.exceptions == 0
    finally:
        session.close()
        engine.dispose()


def test_incomplete_record_listed_not_defaulted() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000003")
        _facility(
            session,
            borrower,
            "F-000003",
            sanctioned_limit=Decimal("70000000.0000"),
            outstanding=Decimal("65000000.0000"),
        )
        # Deliberately no FacilityConduct row for `_AS_OF`: the borrower
        # clears the exposure threshold but its SMA classification cannot
        # be computed, so it must be listed, never silently defaulted.
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        result = _service(session).generate(
            principal, report_type=CrilcReportType.MONTHLY, as_of_date=_AS_OF, scope=scope
        )

        assert result.report.row_count == 0
        assert not any(row["borrower_reference"] == "B-000003" for row in result.report.rows)
        assert result.report.reconciliation.exceptions == 1
        exception = result.report.exceptions[0]
        assert exception.borrower_reference == "B-000003"
        assert set(exception.missing_fields) >= {"days_past_due", "sma_band", "overdue_amount"}
        assert exception.reason
    finally:
        session.close()
        engine.dispose()


def test_regeneration_reproduces_exactly() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000001")
        facility = _facility(
            session,
            borrower,
            "F-000001",
            sanctioned_limit=Decimal("60000000.0000"),
            outstanding=Decimal("55000000.0000"),
        )
        _conduct(session, facility, _AS_OF, days_past_due=10, overdue_amount=Decimal("100000.0000"))
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        service = _service(session)
        first = service.generate(
            principal, report_type=CrilcReportType.MONTHLY, as_of_date=_AS_OF, scope=scope
        )

        later_service = _service(session, clock=FixedClock(_NOW + timedelta(days=3)))
        second = later_service.generate(
            principal, report_type=CrilcReportType.MONTHLY, as_of_date=_AS_OF, scope=scope
        )

        assert first.content_hash == second.content_hash
        assert first.content_bytes == second.content_bytes
        assert first.report.as_dict() == second.report.as_dict()
        assert first.generated_at != second.generated_at
    finally:
        session.close()
        engine.dispose()


def test_layout_version_change_retains_both(tmp_path: Path) -> None:
    (tmp_path / "crilc_monthly.v1.json").write_text(_MINIMAL_V1_LAYOUT, encoding="utf-8")
    (tmp_path / "crilc_monthly.v2.json").write_text(_MINIMAL_V2_LAYOUT, encoding="utf-8")

    versions = available_layout_versions(CrilcReportType.MONTHLY, layouts_dir=tmp_path)
    assert versions == (1, 2)

    superseded = load_crilc_layout(CrilcReportType.MONTHLY, 1, layouts_dir=tmp_path)
    latest = load_crilc_layout(CrilcReportType.MONTHLY, layouts_dir=tmp_path)
    assert superseded.version == 1
    assert latest.version == 2
    assert superseded.field_names == ("as_of_date", "borrower_reference")
    assert latest.field_names == ("as_of_date", "borrower_reference", "legal_name")

    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000001")
        _facility(
            session,
            borrower,
            "F-000001",
            sanctioned_limit=Decimal("60000000.0000"),
            outstanding=Decimal("55000000.0000"),
        )
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        pinned = _service(session).generate(
            principal,
            report_type=CrilcReportType.MONTHLY,
            as_of_date=_AS_OF,
            layout_version=1,
            layouts_dir=tmp_path,
            scope=scope,
        )
        assert pinned.layout_version == 1
        assert set(pinned.report.rows[0]) == {"as_of_date", "borrower_reference"}

        defaulted = _service(session).generate(
            principal,
            report_type=CrilcReportType.MONTHLY,
            as_of_date=_AS_OF,
            layouts_dir=tmp_path,
            scope=scope,
        )
        assert defaulted.layout_version == 2
        assert set(defaulted.report.rows[0]) == {"as_of_date", "borrower_reference", "legal_name"}
    finally:
        session.close()
        engine.dispose()


def test_generation_audited() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000001")
        facility = _facility(
            session,
            borrower,
            "F-000001",
            sanctioned_limit=Decimal("60000000.0000"),
            outstanding=Decimal("55000000.0000"),
        )
        _conduct(session, facility, _AS_OF, days_past_due=10, overdue_amount=Decimal("100000.0000"))
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        result = _service(session).generate(
            principal, report_type=CrilcReportType.MONTHLY, as_of_date=_AS_OF, scope=scope
        )

        events = (
            session.execute(
                select(AuditEvent).where(AuditEvent.event_type == "crilc_report_generated")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.actor_id == principal.id
        assert event.payload["row_count"] == result.report.row_count == 1
        assert event.payload["content_hash"] == result.content_hash
        assert event.payload["layout_version"] == 1
        assert event.payload["reconciliation"]["included"] == 1
    finally:
        session.close()
        engine.dispose()
