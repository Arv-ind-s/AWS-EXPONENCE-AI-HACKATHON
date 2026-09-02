"""Integration coverage for `T-134`'s Board MIS and scheduled report delivery."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.forecast import Forecast, ForecastRun
from covenant_radar.db.models.identity import AppUser
from covenant_radar.db.models.operations import Connector, ConnectorRun, EvaluationRun
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Disposition
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.ports.notifier import DeliveryResult, DeliveryStatus, OutboundMessage
from covenant_radar.reporting.mis import MisPeriod, MisReportDeliveryService, MisReportService
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
_CURRENT_PERIOD = MisPeriod(start=date(2026, 6, 1), end=date(2026, 6, 30))
_PREVIOUS_PERIOD = MisPeriod(start=date(2026, 5, 1), end=date(2026, 5, 31))

_ALL_TABLES = [
    Portfolio.__table__,
    Borrower.__table__,
    Facility.__table__,
    FacilityConduct.__table__,
    Covenant.__table__,
    CovenantVersion.__table__,
    CovenantTest.__table__,
    ForecastRun.__table__,
    Forecast.__table__,
    Disposition.__table__,
    AppUser.__table__,
    Connector.__table__,
    ConnectorRun.__table__,
    EvaluationRun.__table__,
    AuditEvent.__table__,
]


def _new_session() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=_ALL_TABLES)
    return engine, Session(engine)


def _portfolio(session: Session) -> Portfolio:
    portfolio = Portfolio.create(
        code="ROOT", name="Root", created_at=_NOW, updated_at=_NOW, request_id="rq-t134-portfolio"
    )
    session.add(portfolio)
    session.flush()
    return portfolio


def _app_user(session: Session, username: str, *, email: str | None = None) -> AppUser:
    user = AppUser(
        username=username,
        email=email or f"{username}@example.test",
        full_name=username.replace("-", " ").title(),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t134-{username}",
    )
    session.add(user)
    session.flush()
    return user


def _borrower(session: Session, portfolio: Portfolio, reference: str) -> Borrower:
    borrower = Borrower(
        reference=reference,
        legal_name=f"{reference} Private Limited",
        portfolio_id=portfolio.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t134-{reference}",
    )
    session.add(borrower)
    session.flush()
    return borrower


def _facility(session: Session, borrower: Borrower, reference: str) -> Facility:
    facility = Facility(
        reference=reference,
        borrower_id=borrower.id,
        facility_type="term_loan",
        sanctioned_limit=Decimal("60000000.0000"),
        currency="INR",
        outstanding=Decimal("55000000.0000"),
        sanction_date=date(2025, 1, 1),
        effective_from=date(2025, 1, 1),
        effective_to=None,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t134-{reference}",
    )
    session.add(facility)
    session.flush()
    return facility


def _conduct(session: Session, facility: Facility, as_of_date: date, *, days_past_due: int) -> None:
    session.add(
        FacilityConduct(
            facility_id=facility.id,
            as_of_date=as_of_date,
            days_past_due=days_past_due,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-t134-conduct-{facility.reference}-{as_of_date.isoformat()}",
        )
    )
    session.flush()


def _covenant(
    session: Session, facility: Facility, reference: str, registered_by: AppUser
) -> tuple[Covenant, CovenantVersion]:
    covenant = Covenant(
        reference=reference,
        facility_id=facility.id,
        name=f"{reference} covenant",
        covenant_class="leverage",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t134-{reference}",
    )
    session.add(covenant)
    session.flush()
    version = CovenantVersion(
        covenant_id=covenant.id,
        version_no=1,
        threshold=Decimal("3.0000"),
        direction="max",
        unit="x",
        frequency="quarterly",
        test_basis="standalone",
        effective_from=date(2025, 1, 1),
        status="live",
        tested_at_least_once=True,
        registered_by_id=registered_by.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t134-{reference}-v1",
    )
    session.add(version)
    session.flush()
    return covenant, version


def _covenant_test(
    session: Session, version: CovenantVersion, as_of_date: date, verdict: str
) -> CovenantTest:
    row = CovenantTest(
        covenant_version_id=version.id,
        as_of_date=as_of_date,
        verdict=verdict,
        computed_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t134-test-{version.id}-{as_of_date.isoformat()}",
    )
    session.add(row)
    session.flush()
    return row


def _forecast(
    session: Session, version: CovenantVersion, *, created_at: datetime, below_floor: bool = False
) -> Forecast:
    run = ForecastRun(
        as_of_date=created_at.date(),
        started_at=created_at,
        state="succeeded",
        created_at=created_at,
        updated_at=created_at,
        request_id=f"rq-t134-run-{version.id}-{created_at.isoformat()}",
    )
    session.add(run)
    session.flush()
    forecast = Forecast(
        run_id=run.id,
        covenant_version_id=version.id,
        horizon_days=90,
        probability=Decimal("0.7000"),
        confidence=Decimal("0.8000"),
        below_confidence_floor=below_floor,
        direction="max",
        created_at=created_at,
        updated_at=created_at,
        request_id=f"rq-t134-forecast-{version.id}-{created_at.isoformat()}",
    )
    session.add(forecast)
    session.flush()
    return forecast


def _disposition(
    session: Session, forecast: Forecast, actor: AppUser, *, outcome: str, created_at: datetime
) -> None:
    session.add(
        Disposition(
            subject_type="forecast",
            subject_id=forecast.id,
            outcome=outcome,
            actor_id=actor.id,
            created_at=created_at,
            updated_at=created_at,
            request_id=f"rq-t134-disposition-{forecast.id}",
        )
    )
    session.flush()


def _evaluation_run(session: Session, *, executed_at: datetime) -> None:
    session.add(
        EvaluationRun(
            commit_sha="a" * 40,
            arm="default",
            scores={"g1_lead_time_pct": 72.5, "g3_false_escalation_pct": 3.1},
            passed=True,
            executed_at=executed_at,
            created_at=executed_at,
            updated_at=executed_at,
            request_id="rq-t134-eval",
        )
    )
    session.flush()


def _connector_with_run(session: Session, *, started_at: datetime) -> None:
    connector = Connector(
        name="core_banking",
        connector_type="csv",
        config={},
        is_active=True,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t134-connector",
    )
    session.add(connector)
    session.flush()
    session.add(
        ConnectorRun(
            connector_id=connector.id,
            started_at=started_at,
            finished_at=started_at,
            state="succeeded",
            record_count=1000,
            reject_count=5,
            lag_seconds=120,
            created_at=started_at,
            updated_at=started_at,
            request_id="rq-t134-connector-run",
        )
    )
    session.flush()


def _principal() -> Principal:
    return Principal.user(uuid4(), (Permission.EXPORT_EVIDENCE,))


def _service(session: Session, *, clock: FixedClock | None = None) -> MisReportService:
    resolved_clock = clock or FixedClock(_NOW)
    audit = AuditRecorder(
        AuditRepository(session), clock=resolved_clock, request_id="rq-t134-service"
    )
    return MisReportService(
        session,
        audit=audit,
        clock=resolved_clock,
        scope_resolver=lambda principal: Scope.empty(principal.id),
        request_id="rq-t134-service",
    )


def _build_full_fixture(session: Session, registered_by: AppUser) -> Portfolio:
    """A borrower with every kind of underlying fact the report reads."""

    portfolio = _portfolio(session)
    borrower = _borrower(session, portfolio, "B-000001")
    facility = _facility(session, borrower, "F-000001")
    _, version = _covenant(session, facility, "CV-0001", registered_by)

    _conduct(session, facility, date(2026, 6, 30), days_past_due=45)  # SMA-1

    _covenant_test(session, version, date(2026, 5, 15), "pass")
    _covenant_test(session, version, date(2026, 6, 25), "warning")
    _covenant_test(session, version, date(2026, 7, 15), "breach")  # confirms the forecast below

    warning = _forecast(session, version, created_at=datetime(2026, 6, 5, 12, 0, tzinfo=UTC))
    _disposition(
        session,
        warning,
        registered_by,
        outcome="acted",
        created_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
    )

    _evaluation_run(session, executed_at=datetime(2026, 6, 15, 0, 0, tzinfo=UTC))
    _connector_with_run(session, started_at=datetime(2026, 6, 20, 0, 0, tzinfo=UTC))

    session.commit()
    return portfolio


def _metric_labels(points: object) -> dict[str, Decimal]:
    return {point.label: point.value for point in points}


def test_every_section_computed() -> None:
    engine, session = _new_session()
    try:
        registered_by = _app_user(session, "system-actor")
        portfolio = _build_full_fixture(session, registered_by)

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        report = _service(session).generate(
            principal, period=_CURRENT_PERIOD, previous_period=_PREVIOUS_PERIOD, scope=scope
        )

        assert report.distribution.covenant_band.is_present
        assert _metric_labels(report.distribution.covenant_band.points) == {"warning": Decimal(1)}
        assert report.distribution.sma_band.is_present
        assert _metric_labels(report.distribution.sma_band.points) == {"SMA-1": Decimal(1)}

        assert report.migration.covenant_band_migration.is_present
        assert _metric_labels(report.migration.covenant_band_migration.points) == {
            "pass → warning": Decimal(1)
        }

        assert report.lead_time.lead_time_distribution.is_present
        assert report.lead_time.at_least_30_days_pct.points[0].value == Decimal("100.00")
        assert report.lead_time.at_least_60_days_pct.points[0].value == Decimal("0.00")

        assert report.escalations.warnings_raised.points[0].value == Decimal(1)
        assert report.escalations.amber_or_worse_pct.points[0].value == Decimal("100.00")
        assert _metric_labels(report.escalations.disposition_outcomes.points) == {
            "acted": Decimal(1)
        }
        assert report.escalations.acted_on_pct.points[0].value == Decimal("100.00")

        assert report.model_performance.passed is True
        assert report.model_performance.commit_sha == "a" * 40
        assert report.model_performance.scores.is_present
        assert len(report.model_performance.scores.points) == 2

        assert len(report.connectors.entries) == 1
        assert report.connectors.entries[0].name == "core_banking"
        assert report.connectors.record_counts.is_present
        assert report.connectors.record_counts.points[0].value == Decimal(1000)
        assert report.connectors.reject_counts.points[0].value == Decimal(5)
        assert report.connectors.lag_seconds.points[0].value == Decimal(120)
    finally:
        session.close()
        engine.dispose()


def test_absent_data_stated_not_charted_as_zero() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)  # a portfolio in scope with zero borrowers
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        report = _service(session).generate(principal, period=_CURRENT_PERIOD, scope=scope)

        assert not report.distribution.covenant_band.is_present
        assert report.distribution.covenant_band.absent_reason
        assert not report.distribution.sma_band.is_present
        assert report.distribution.sma_band.absent_reason

        assert not report.migration.covenant_band_migration.is_present
        assert not report.migration.sma_migration.is_present

        # A genuinely computed zero (no warnings were raised) is present
        # data, not an absence — the two must never be conflated.
        assert report.escalations.warnings_raised.is_present
        assert report.escalations.warnings_raised.points[0].value == Decimal(0)
    finally:
        session.close()
        engine.dispose()


def test_uncomputable_metric_named_with_reason() -> None:
    engine, session = _new_session()
    try:
        registered_by = _app_user(session, "system-actor")
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000002")
        facility = _facility(session, borrower, "F-000002")
        _covenant(session, facility, "CV-0002", registered_by)
        # No forecast raised, no evaluation run, no connector configured —
        # borrowers and covenants exist, but these specific metrics cannot
        # be computed for the period.
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        report = _service(session).generate(principal, period=_CURRENT_PERIOD, scope=scope)

        assert not report.lead_time.lead_time_distribution.is_present
        assert "confirmed by an actual covenant breach" in (
            report.lead_time.lead_time_distribution.absent_reason or ""
        )
        assert not report.model_performance.scores.is_present
        assert not report.connectors.record_counts.is_present
        assert not report.escalations.disposition_outcomes.is_present
    finally:
        session.close()
        engine.dispose()


def test_regeneration_reproduces() -> None:
    engine, session = _new_session()
    try:
        registered_by = _app_user(session, "system-actor")
        portfolio = _build_full_fixture(session, registered_by)

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        first = _service(session).generate(
            principal, period=_CURRENT_PERIOD, previous_period=_PREVIOUS_PERIOD, scope=scope
        )
        second = _service(session, clock=FixedClock(_NOW.replace(hour=23))).generate(
            principal, period=_CURRENT_PERIOD, previous_period=_PREVIOUS_PERIOD, scope=scope
        )

        assert first.content_hash() == second.content_hash()
        assert first.as_dict() == second.as_dict()
    finally:
        session.close()
        engine.dispose()


def test_every_chart_has_its_figures() -> None:
    engine, session = _new_session()
    try:
        registered_by = _app_user(session, "system-actor")
        portfolio = _build_full_fixture(session, registered_by)

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        export = _service(session).export(
            principal, period=_CURRENT_PERIOD, previous_period=_PREVIOUS_PERIOD, scope=scope
        )

        present_metrics = [
            metric
            for section in (
                export.report.distribution,
                export.report.migration,
                export.report.lead_time,
                export.report.escalations,
                export.report.model_performance,
                export.report.connectors,
            )
            for metric in (getattr(section, field.name) for field in fields(section))
            if hasattr(metric, "is_present")
        ]
        assert present_metrics  # the fixture produces at least one present metric
        for metric in present_metrics:
            if not metric.is_present:
                continue
            assert metric.points, f"{metric.name} is present but carries no figures"
            for point in metric.points:
                assert point.label in export.html
                assert format(point.value, "f") in export.html
    finally:
        session.close()
        engine.dispose()


class _AlwaysFailingNotifier:
    """A notifier that never succeeds — every attempt returns RETRY."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: OutboundMessage) -> DeliveryResult:
        self.calls += 1
        return DeliveryResult(DeliveryStatus.RETRY, error="SMTP relay unreachable")


def test_delivery_failure_surfaced() -> None:
    engine, session = _new_session()
    try:
        registered_by = _app_user(session, "system-actor")
        recipient = _app_user(session, "board-member", email="board@example.test")
        portfolio = _build_full_fixture(session, registered_by)
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        export = _service(session).export(
            principal, period=_CURRENT_PERIOD, previous_period=_PREVIOUS_PERIOD, scope=scope
        )

        clock = FixedClock(_NOW)
        audit = AuditRecorder(AuditRepository(session), clock=clock, request_id="rq-t134-delivery")
        notifier = _AlwaysFailingNotifier()
        alerts: list[tuple[UUID, str]] = []
        delivery = MisReportDeliveryService(
            session,
            notifier,
            audit=audit,
            clock=clock,
            max_attempts=3,
            sleep=lambda _seconds: None,
            dead_letter_alert=lambda recipient_id, reason: alerts.append((recipient_id, reason)),
        )

        outcomes = delivery.deliver(
            export, [recipient.id], actor_id=registered_by.id, request_id="rq-t134-delivery"
        )
        session.commit()

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.delivered is False
        assert outcome.attempts == 3
        assert notifier.calls == 3
        assert alerts == [(recipient.id, "SMTP relay unreachable")]

        events = (
            session.execute(
                select(AuditEvent).where(AuditEvent.event_type == "mis_report_delivery_failed")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["recipient_id"] == str(recipient.id)
        assert events[0].payload["attempts"] == 3
    finally:
        session.close()
        engine.dispose()
