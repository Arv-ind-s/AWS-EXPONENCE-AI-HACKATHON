"""Integration coverage for `T-133`'s EWS/RFA pack assembly and export."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from markupsafe import escape as markup_escape
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from covenant_radar.audit.reconstruct import PartStatus
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
)
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastDriver, ForecastRun, Intervention
from covenant_radar.db.models.operations import RetentionPurgeLog
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import CertificateRequest, EvidenceItem
from covenant_radar.db.models.workflow import ActionTaken, Case, Disposition, Memo
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.reporting.rfa_pack import (
    RFA_PACK_ADVISORY_STATEMENT,
    RFA_PACK_NO_FRAUD_DETERMINATION_STATEMENT,
    SECTION_COVENANTS,
    SECTION_DOCUMENTS,
    SECTION_EXPOSURE,
    SECTION_FORECASTS,
    SECTION_INTERVENTIONS,
    SECTION_SIGNALS,
    SECTION_WARNINGS,
    RfaPackService,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
_AS_OF = date(2026, 8, 31)

_ALL_TABLES = [
    Portfolio.__table__,
    Borrower.__table__,
    Facility.__table__,
    Covenant.__table__,
    CovenantVersion.__table__,
    CovenantTest.__table__,
    CovenantSchedule.__table__,
    EvidenceItem.__table__,
    ForecastRun.__table__,
    Forecast.__table__,
    ForecastDriver.__table__,
    Disposition.__table__,
    Case.__table__,
    ActionTaken.__table__,
    Intervention.__table__,
    Document.__table__,
    CertificateRequest.__table__,
    RetentionPurgeLog.__table__,
    Memo.__table__,
    AuditEvent.__table__,
]


def _new_session() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=_ALL_TABLES)
    return engine, Session(engine)


def _portfolio(session: Session) -> Portfolio:
    portfolio = Portfolio.create(
        code="ROOT", name="Root", created_at=_NOW, updated_at=_NOW, request_id="rq-t133-portfolio"
    )
    session.add(portfolio)
    session.flush()
    return portfolio


def _borrower(session: Session, portfolio: Portfolio, reference: str) -> Borrower:
    borrower = Borrower(
        reference=reference,
        legal_name=f"{reference} Private Limited",
        portfolio_id=portfolio.id,
        industry_code="MFG",
        constitution="private_limited",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t133-{reference}",
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
        request_id=f"rq-t133-{reference}",
    )
    session.add(facility)
    session.flush()
    return facility


def _covenant(session: Session, facility: Facility, reference: str) -> Covenant:
    covenant = Covenant(
        reference=reference,
        facility_id=facility.id,
        name="Debt service coverage ratio",
        covenant_class="financial",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t133-{reference}",
    )
    session.add(covenant)
    session.flush()
    return covenant


def _covenant_version(
    session: Session,
    covenant: Covenant,
    *,
    version_no: int = 1,
    source_document_id: UUID | None = None,
) -> CovenantVersion:
    version = CovenantVersion(
        covenant_id=covenant.id,
        version_no=version_no,
        threshold=Decimal("1.2000"),
        direction="min",
        unit="ratio",
        frequency="quarterly",
        test_basis="audited",
        effective_from=date(2025, 1, 1),
        status="live",
        tested_at_least_once=False,
        registered_by_id=uuid4(),
        source_document_id=source_document_id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t133-{covenant.reference}-v{version_no}",
    )
    session.add(version)
    session.flush()
    return version


def _covenant_test(session: Session, version: CovenantVersion, *, as_of_date: date) -> CovenantTest:
    test = CovenantTest(
        covenant_version_id=version.id,
        as_of_date=as_of_date,
        value=Decimal("1.3500"),
        threshold_used=version.threshold,
        headroom_pct=Decimal("12.5000"),
        verdict="warning",
        computed_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-covenant-test",
    )
    session.add(test)
    session.flush()
    return test


def _covenant_schedule(
    session: Session, version: CovenantVersion, *, due_date: date
) -> CovenantSchedule:
    schedule = CovenantSchedule(
        covenant_version_id=version.id,
        due_date=due_date,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-schedule",
    )
    session.add(schedule)
    session.flush()
    return schedule


def _evidence_item(session: Session, borrower: Borrower) -> EvidenceItem:
    item = EvidenceItem(
        id=uuid4(),
        borrower_id=borrower.id,
        family="payment",
        evidence_type="payment_delay",
        first_seen=date(2026, 8, 1),
        last_seen=date(2026, 8, 20),
        materiality_pct=Decimal("15.0000"),
        decay_factor=Decimal("0.9000"),
        state="sustained",
        counts_toward_pressure=True,
        source_event_ids=["evt-1", "evt-2"],
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-evidence",
    )
    session.add(item)
    session.flush()
    return item


def _forecast_run(session: Session) -> ForecastRun:
    run = ForecastRun(
        as_of_date=_AS_OF,
        model_version="m1",
        started_at=_NOW,
        finished_at=_NOW,
        covenant_count=1,
        state="complete",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-run",
    )
    session.add(run)
    session.flush()
    return run


def _forecast(
    session: Session,
    run: ForecastRun,
    version: CovenantVersion,
    *,
    below_confidence_floor: bool = False,
) -> Forecast:
    forecast = Forecast(
        run_id=run.id,
        covenant_version_id=version.id,
        horizon_days=90,
        probability=Decimal("0.6500"),
        confidence=Decimal("0.8000"),
        below_confidence_floor=below_confidence_floor,
        direction="min",
        data_as_of=_AS_OF,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-forecast",
    )
    session.add(forecast)
    session.flush()
    return forecast


def _forecast_driver(session: Session, forecast: Forecast) -> ForecastDriver:
    driver = ForecastDriver(
        forecast_id=forecast.id,
        name="utilisation_spike",
        share=Decimal("0.7000"),
        is_other=False,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-driver",
    )
    session.add(driver)
    session.flush()
    return driver


def _disposition(session: Session, forecast: Forecast) -> Disposition:
    disposition = Disposition(
        subject_type="forecast",
        subject_id=forecast.id,
        outcome="monitoring",
        reason_code="monitoring_only",
        note="Desk is watching next quarter's statement.",
        actor_id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-disposition",
    )
    session.add(disposition)
    session.flush()
    return disposition


def _case(session: Session, borrower: Borrower, reference: str) -> Case:
    case = Case(
        reference=reference,
        borrower_id=borrower.id,
        state="in_progress",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t133-{reference}",
    )
    session.add(case)
    session.flush()
    return case


def _intervention_catalog_entry(session: Session, code: str) -> Intervention:
    intervention = Intervention(
        code=code,
        text="Request an updated stock statement from the borrower.",
        effect_model="delay_days",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-t133-{code}",
    )
    session.add(intervention)
    session.flush()
    return intervention


def _action_taken(session: Session, case: Case, intervention: Intervention) -> ActionTaken:
    action = ActionTaken(
        case_id=case.id,
        intervention_id=intervention.id,
        taken_at=_NOW,
        actor_id=uuid4(),
        outcome="Stock statement requested.",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-action",
    )
    session.add(action)
    session.flush()
    return action


def _document(session: Session, borrower: Borrower, content_hash: str) -> Document:
    document = Document(
        borrower_id=borrower.id,
        doc_type="sanction_letter",
        filename="sanction-letter.pdf",
        content_hash=content_hash,
        byte_size=1024,
        mime_type="application/pdf",
        storage_key=f"documents/{content_hash}",
        uploaded_by_id=uuid4(),
        scan_result="clean",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-document",
    )
    session.add(document)
    session.flush()
    return document


def _certificate_request(
    session: Session, borrower: Borrower, schedule: CovenantSchedule, *, due_date: date
) -> CertificateRequest:
    certificate = CertificateRequest(
        covenant_schedule_id=schedule.id,
        borrower_id=borrower.id,
        due_date=due_date,
        state="received",
        requested_at=_NOW,
        received_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-certificate",
    )
    session.add(certificate)
    session.flush()
    return certificate


def _retention_purge_log(session: Session, *, entity_id: UUID, rule: str) -> RetentionPurgeLog:
    log = RetentionPurgeLog(
        entity="document",
        criteria={"entity_id": str(entity_id), "rule": rule},
        purged_count=1,
        executed_at=_NOW,
        executed_by="retention-job",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-purge",
    )
    session.add(log)
    session.flush()
    return log


def _memo(session: Session, borrower: Borrower, run: ForecastRun) -> Memo:
    memo = Memo(
        borrower_id=borrower.id,
        run_id=run.id,
        template_version="v1",
        slots={"slots": {"situation": {"value": "Headroom narrowing.", "state": "measured"}}},
        drafted_text=(
            "The borrower's DSCR has narrowed over the last two quarters.\n\n"
            "Recommend the committee review the attached evidence timeline."
        ),
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t133-memo",
    )
    session.add(memo)
    session.flush()
    return memo


def _principal() -> Principal:
    return Principal.user(uuid4(), (Permission.VIEW_AUDIT, Permission.EXPORT_EVIDENCE))


def _service(session: Session, *, clock: FixedClock | None = None) -> RfaPackService:
    resolved_clock = clock or FixedClock(_NOW)
    audit = AuditRecorder(
        AuditRepository(session), clock=resolved_clock, request_id="rq-t133-service"
    )
    return RfaPackService(
        session,
        audit=audit,
        clock=resolved_clock,
        scope_resolver=lambda principal: Scope.empty(principal.id),
        request_id="rq-t133-service",
    )


def test_every_section_present() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000001")
        facility = _facility(session, borrower, "F-000001")
        covenant = _covenant(session, facility, "CV-000001")
        version = _covenant_version(session, covenant)
        _covenant_test(session, version, as_of_date=_AS_OF)
        _evidence_item(session, borrower)
        run = _forecast_run(session)
        forecast = _forecast(session, run, version, below_confidence_floor=False)
        _forecast_driver(session, forecast)
        _disposition(session, forecast)
        case = _case(session, borrower, "CASE-000001")
        intervention = _intervention_catalog_entry(session, "REQUEST_STOCK_STATEMENT")
        _action_taken(session, case, intervention)
        _document(session, borrower, "hash-000001")
        schedule = _covenant_schedule(session, version, due_date=date(2026, 9, 30))
        _certificate_request(session, borrower, schedule, due_date=date(2026, 9, 30))
        _memo(session, borrower, run)
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        pack = _service(session).assemble(
            principal, borrower.id, as_of_date=_AS_OF, prepared_for="RFA Committee", scope=scope
        )

        assert len(pack.exposure.facilities) == 1
        assert pack.exposure.facilities[0].facility_id == facility.id
        assert len(pack.covenants) == 1
        assert pack.covenants[0].reference == covenant.reference
        assert len(pack.covenants[0].history) == 1
        assert len(pack.evidence) == 1
        assert len(pack.forecasts) == 1
        assert pack.forecasts[0].drivers[0].name == "utilisation_spike"
        assert len(pack.warnings) == 1
        assert pack.warnings[0].dispositions[0].outcome == "monitoring"
        assert len(pack.interventions) == 1
        assert pack.interventions[0].intervention_code == "REQUEST_STOCK_STATEMENT"
        assert len(pack.documents) == 1
        assert len(pack.certificates) == 1
        assert pack.memo.status is PartStatus.PRESENT
        assert pack.gaps == ()
    finally:
        session.close()
        engine.dispose()


def test_gaps_named_and_dated() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000002")
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        pack = _service(session).assemble(
            principal, borrower.id, as_of_date=_AS_OF, prepared_for="RFA Committee", scope=scope
        )

        expected_sections = {
            SECTION_EXPOSURE,
            SECTION_COVENANTS,
            SECTION_SIGNALS,
            SECTION_FORECASTS,
            SECTION_WARNINGS,
            SECTION_INTERVENTIONS,
            SECTION_DOCUMENTS,
        }
        gap_sections = {gap.section for gap in pack.gaps}
        assert gap_sections == expected_sections
        for gap in pack.gaps:
            assert gap.as_of == _AS_OF
            assert gap.reason.strip()
            assert borrower.reference in gap.reason or "No " in gap.reason
        # Nothing was padded: every corresponding collection is genuinely empty.
        assert pack.exposure.facilities == ()
        assert pack.covenants == ()
        assert pack.evidence == ()
        assert pack.forecasts == ()
        assert pack.warnings == ()
        assert pack.interventions == ()
        assert pack.documents == ()
        assert pack.certificates == ()
        assert pack.memo.status is PartStatus.NOT_GENERATED
    finally:
        session.close()
        engine.dispose()


def test_model_drafted_content_marked() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000003")
        run = _forecast_run(session)
        _memo(session, borrower, run)
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        result = _service(session).export(
            principal, borrower.id, as_of_date=_AS_OF, prepared_for="RFA Committee", scope=scope
        )

        assert result.pack.memo.status is PartStatus.PRESENT
        assert "DSCR has narrowed" in result.pack.memo.drafted_text
        assert "Model-drafted" in result.html
        assert "DSCR has narrowed" in result.html
    finally:
        session.close()
        engine.dispose()


def test_cover_carries_advisory_and_no_fraud_determination() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000004")
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        result = _service(session).export(
            principal, borrower.id, as_of_date=_AS_OF, prepared_for="RFA Committee", scope=scope
        )

        assert result.pack.cover.advisory_statement == RFA_PACK_ADVISORY_STATEMENT
        assert (
            result.pack.cover.no_fraud_determination_statement
            == RFA_PACK_NO_FRAUD_DETERMINATION_STATEMENT
        )
        assert "no fraud determination" in RFA_PACK_NO_FRAUD_DETERMINATION_STATEMENT.lower()
        # The statements are rendered on the pack's face, not just held in the model.
        # (The HTML is autoescaped, so compare against the escaped form.)
        assert str(markup_escape(RFA_PACK_ADVISORY_STATEMENT)) in result.html
        assert str(markup_escape(RFA_PACK_NO_FRAUD_DETERMINATION_STATEMENT)) in result.html
    finally:
        session.close()
        engine.dispose()


def test_purged_element_named_with_rule() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000005")
        facility = _facility(session, borrower, "F-000005")
        covenant = _covenant(session, facility, "CV-000005")
        purged_document_id = uuid4()
        version = _covenant_version(session, covenant, source_document_id=purged_document_id)
        _retention_purge_log(
            session, entity_id=purged_document_id, rule="documents.retention.7_years"
        )
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        pack = _service(session).assemble(
            principal, borrower.id, as_of_date=_AS_OF, prepared_for="RFA Committee", scope=scope
        )

        purged_entries = [entry for entry in pack.documents if entry.id == purged_document_id]
        assert len(purged_entries) == 1
        entry = purged_entries[0]
        assert entry.status is PartStatus.PURGED
        assert entry.purged is not None
        assert entry.purged.rule == "documents.retention.7_years"
        assert entry.purged.purged_at == _NOW
        assert version.source_document_id == purged_document_id
    finally:
        session.close()
        engine.dispose()


def test_export_audited() -> None:
    engine, session = _new_session()
    try:
        portfolio = _portfolio(session)
        borrower = _borrower(session, portfolio, "B-000006")
        session.commit()

        principal = _principal()
        scope = Scope.from_paths(principal.id, [portfolio.path])
        result = _service(session).export(
            principal, borrower.id, as_of_date=_AS_OF, prepared_for="RFA Committee", scope=scope
        )

        events = (
            session.execute(select(AuditEvent).where(AuditEvent.event_type == "rfa_pack_exported"))
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.actor_id == principal.id
        assert event.payload["borrower_id"] == str(borrower.id)
        assert event.payload["prepared_for"] == "RFA Committee"
        assert event.payload["assembled_by"] == str(principal.id)
        assert event.payload["content_hash"] == result.content_hash
        assert result.content_hash == hashlib.sha256(result.html.encode("utf-8")).hexdigest()
        assert result.audit_event is event
    finally:
        session.close()
        engine.dispose()
