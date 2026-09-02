"""Integration coverage for T-068's warning reconstruction assembly."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.audit.reconstruct import PartStatus
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import ThresholdSnapshot, TraceRow
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.document import Document, DocumentSpan
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, ForecastDriver, ForecastPath, ForecastRun
from covenant_radar.db.models.operations import RetentionPurgeLog
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import EvidenceItem, EvidenceTransition
from covenant_radar.db.models.workflow import Disposition, Memo, OverrideRecord
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.reconstruction import ReconstructionService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
_AS_OF = date(2026, 2, 28)
_REQUEST_ID = "rq-t068-fixture"


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@dataclass
class World:
    session: Session
    portfolio: Portfolio
    borrower: Borrower
    facility: Facility
    covenant: Covenant
    covenant_version: CovenantVersion
    threshold_snapshot: ThresholdSnapshot
    run: ForecastRun
    forecast: Forecast
    document: Document | None
    span: DocumentSpan | None
    principal: Principal
    scope: Scope

    def service(self, *, clock: FixedClock | None = None) -> ReconstructionService:
        return ReconstructionService(self.session, clock=clock or FixedClock(_NOW))


def _build_world(
    session: Session,
    *,
    with_document: bool = True,
    phantom_document_id: UUID | None = None,
) -> World:
    portfolio = Portfolio.create(
        code="P1", name="Portfolio One", created_at=_NOW, updated_at=_NOW, request_id=_REQUEST_ID
    )
    session.add(portfolio)
    session.flush()

    borrower = Borrower(
        reference="B-000001",
        legal_name="Borrower One Private Limited",
        portfolio_id=portfolio.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    session.add(borrower)
    session.flush()

    facility = Facility(
        reference="F-000001",
        borrower_id=borrower.id,
        facility_type="term_loan",
        sanctioned_limit=Decimal("100000000"),
        currency="INR",
        sanction_date=date(2024, 1, 1),
        effective_from=date(2024, 1, 1),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    session.add(facility)
    session.flush()

    document: Document | None = None
    span: DocumentSpan | None = None
    if with_document:
        document = Document(
            borrower_id=borrower.id,
            doc_type="loan_agreement",
            filename="loan-agreement.pdf",
            content_hash="hash-t068-doc-1",
            byte_size=1024,
            mime_type="application/pdf",
            storage_key="documents/t068/loan-agreement.pdf",
            uploaded_by_id=uuid4(),
            page_count=5,
            retention_class="statutory_7y",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
        session.add(document)
        session.flush()
        span = DocumentSpan(
            document_id=document.id,
            page_number=1,
            start_offset=0,
            end_offset=120,
            text="Minimum DSCR shall not be less than 1.20x, tested quarterly.",
            span_type="covenant_clause",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
        session.add(span)
        session.flush()

    covenant = Covenant(
        reference="CV-000001",
        facility_id=facility.id,
        name="Minimum DSCR",
        covenant_class="financial",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    session.add(covenant)
    session.flush()

    covenant_version = CovenantVersion(
        covenant_id=covenant.id,
        version_no=1,
        threshold=Decimal("1.20"),
        direction="min",
        unit="x",
        frequency="quarterly",
        test_basis="trailing_12m",
        effective_from=date(2024, 1, 1),
        warning_headroom_pct=Decimal("10.0000"),
        cure_days=30,
        grace_days=15,
        source_document_id=(document.id if document is not None else phantom_document_id),
        source_span_id=span.id if span is not None else None,
        status="live",
        tested_at_least_once=True,
        registered_by_id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    session.add(covenant_version)
    session.flush()

    threshold_snapshot = ThresholdSnapshot(
        values={"T1": {"act": "0.70"}, "T2": {"confidence_floor": "0.50"}},
        source="config",
        effective_from=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    session.add(threshold_snapshot)
    session.flush()

    run = ForecastRun(
        as_of_date=_AS_OF,
        threshold_snapshot_id=threshold_snapshot.id,
        model_version="forecast.scoring.v1",
        started_at=_NOW,
        finished_at=_NOW,
        covenant_count=1,
        state="complete",
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    session.add(run)
    session.flush()

    forecast = Forecast(
        run_id=run.id,
        covenant_version_id=covenant_version.id,
        horizon_days=90,
        probability=Decimal("0.3500"),
        confidence=Decimal("0.8000"),
        below_confidence_floor=False,
        projected_cross_date=date(2026, 5, 1),
        direction="min",
        formula_inputs={
            "candidate_inputs": {"dscr_trailing": "1.35", "trend": "declining"},
        },
        data_as_of=_AS_OF,
        staleness_days=0,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    session.add(forecast)
    session.flush()

    principal = Principal.user(uuid4(), (Permission.VIEW_AUDIT,))
    scope = Scope.from_paths(principal.id, [portfolio.path])

    return World(
        session=session,
        portfolio=portfolio,
        borrower=borrower,
        facility=facility,
        covenant=covenant,
        covenant_version=covenant_version,
        threshold_snapshot=threshold_snapshot,
        run=run,
        forecast=forecast,
        document=document,
        span=span,
        principal=principal,
        scope=scope,
    )


def _add_evidence(world: World, *, state: str = "sustained") -> EvidenceItem:
    item = EvidenceItem(
        borrower_id=world.borrower.id,
        family="payment",
        evidence_type="payment_delay",
        first_seen=date(2026, 2, 1),
        last_seen=_AS_OF,
        persistence_days=14,
        event_count_window=3,
        materiality_pct=Decimal("20"),
        decay_factor=Decimal("0.80"),
        state=state,
        counts_toward_pressure=True,
        source_event_ids=["evt-1"],
        last_scored_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    world.session.add(item)
    world.session.flush()
    return item


def _add_driver(world: World, *, name: str = "trend", evidence_id: UUID | None = None) -> None:
    world.session.add(
        ForecastDriver(
            forecast_id=world.forecast.id,
            name=name,
            share=Decimal("1.0000"),
            evidence_id=evidence_id,
            is_other=False,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    world.session.flush()


def _add_forecast_path(world: World) -> None:
    for day_offset, value, headroom in (
        (0, Decimal("1.30000000"), Decimal("8.3000")),
        (90, Decimal("1.15000000"), Decimal("-4.2000")),
    ):
        world.session.add(
            ForecastPath(
                run_id=world.run.id,
                covenant_version_id=world.covenant_version.id,
                day_offset=day_offset,
                projected_value=value,
                headroom_pct=headroom,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=_REQUEST_ID,
            )
        )
    world.session.flush()


def _add_trace(world: World) -> None:
    world.session.add(
        TraceRow(
            subject_type="forecast",
            subject_id=world.forecast.id,
            stage="4",
            decider="code",
            inputs={"threshold": "1.20"},
            outputs={"probability": "0.3500"},
            rule_or_prompt_version="forecast.trend_pressure.v1",
            thresholds_compared=[
                {"name": "T2", "value": "0.50", "observed": "0.80", "side": "above"}
            ],
            confidence=Decimal("0.8000"),
            sources=[{"type": "forecast", "id": str(world.forecast.id)}],
            occurred_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    world.session.flush()


def _add_memo(world: World) -> Memo:
    memo = Memo(
        borrower_id=world.borrower.id,
        run_id=world.run.id,
        template_version="memo.v1",
        prompt_version="prompt.v1",
        slots={"headline": "DSCR under pressure"},
        drafted_text="DSCR is trending toward breach within the horizon.",
        check_verdict="pass",
        generated_by_id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    world.session.add(memo)
    world.session.flush()
    return memo


def _add_override(world: World) -> None:
    world.session.add(
        OverrideRecord(
            subject_type="forecast",
            subject_id=world.forecast.id,
            stage="triage",
            shown={"band": "amber"},
            user_action="reclassified",
            user_value={"band": "red"},
            reason="Recent covenant conversation escalated the risk.",
            actor_id=uuid4(),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    world.session.flush()


def _add_disposition(world: World) -> None:
    world.session.add(
        Disposition(
            subject_type="forecast",
            subject_id=world.forecast.id,
            outcome="monitoring",
            reason_code="borrower_engaged",
            note="Relationship manager scheduled a follow-up call.",
            actor_id=uuid4(),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    world.session.flush()


def test_all_parts_present_from_one_call(db_session: Session) -> None:
    world = _build_world(db_session)
    evidence = _add_evidence(world)
    _add_driver(world, evidence_id=evidence.id)
    _add_forecast_path(world)
    _add_trace(world)
    _add_memo(world)
    _add_override(world)
    _add_disposition(world)

    result = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)

    assert result.forecast_id == world.forecast.id
    assert result.source_data.status is PartStatus.PRESENT
    assert result.source_data.filename == "loan-agreement.pdf"
    assert result.source_data.span_text is not None
    assert result.covenant_version["version_no"] == 1
    assert result.covenant_version["covenant_reference"] == "CV-000001"
    assert result.thresholds["source"] == "config"
    assert result.calculation is not None
    assert result.calculation["decider"] == "code"
    assert len(result.trend) == 2
    assert result.forecast["probability"] == Decimal("0.3500")
    assert len(result.evidence) == 1
    assert result.evidence[0].id == evidence.id
    assert len(result.drivers) == 1
    assert result.drivers[0].evidence_id == evidence.id
    assert result.memo.status is PartStatus.PRESENT
    assert result.memo.drafted_text is not None
    assert len(result.overrides) == 1
    assert result.overrides[0].reason.startswith("Recent covenant")
    assert len(result.dispositions) == 1
    assert result.dispositions[0].outcome == "monitoring"

    # Every part is reachable from the same call's JSON-safe view too.
    payload = result.as_dict()
    for key in (
        "source_data",
        "covenant_version",
        "thresholds",
        "calculation",
        "trend",
        "forecast",
        "evidence",
        "drivers",
        "memo",
        "overrides",
        "dispositions",
    ):
        assert key in payload


def test_uses_threshold_snapshot_in_force_then(db_session: Session) -> None:
    world = _build_world(db_session)
    before = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)

    db_session.add(
        ThresholdSnapshot(
            values={"T1": {"act": "0.90"}},
            source="config",
            effective_from=datetime(2026, 3, 15, tzinfo=UTC),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    db_session.flush()

    after = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)

    assert after.thresholds == before.thresholds
    assert after.thresholds["id"] == world.threshold_snapshot.id
    assert after.thresholds["values"]["T1"]["act"] == "0.70"


def test_uses_covenant_version_in_force_then(db_session: Session) -> None:
    world = _build_world(db_session)
    before = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)

    world.covenant_version.status = "superseded"
    world.covenant_version.effective_to = date(2026, 3, 1)
    db_session.add(
        CovenantVersion(
            covenant_id=world.covenant.id,
            version_no=2,
            threshold=Decimal("1.35"),
            direction="min",
            unit="x",
            frequency="quarterly",
            test_basis="trailing_12m",
            effective_from=date(2026, 3, 1),
            status="live",
            tested_at_least_once=False,
            registered_by_id=uuid4(),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    db_session.flush()

    after = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)

    # The version's terms are immutable once tested (`db/models/covenant.py`)
    # and are unaffected by the amendment; only `status`/`effective_to` — the
    # two columns the immutability trigger always allows — legitimately
    # change to record that this version has since been superseded.
    terms = {
        key: value
        for key, value in after.covenant_version.items()
        if key not in {"status", "effective_to"}
    }
    before_terms = {
        key: value
        for key, value in before.covenant_version.items()
        if key not in {"status", "effective_to"}
    }
    assert terms == before_terms
    assert after.covenant_version["version_no"] == 1
    assert after.covenant_version["threshold"] == Decimal("1.20")
    assert before.covenant_version["status"] == "live"
    assert after.covenant_version["status"] == "superseded"


def test_evidence_state_as_of_then(db_session: Session) -> None:
    world = _build_world(db_session)
    original = _add_evidence(world, state="sustained")

    before = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)
    assert len(before.evidence) == 1
    assert before.evidence[0].state == "sustained"
    assert before.evidence[0].superseded_since is None

    successor = EvidenceItem(
        borrower_id=world.borrower.id,
        family="payment",
        evidence_type="payment_received",
        first_seen=date(2026, 3, 10),
        last_seen=date(2026, 3, 10),
        persistence_days=1,
        event_count_window=1,
        state="sustained",
        counts_toward_pressure=True,
        supersedes_id=original.id,
        source_event_ids=["evt-2"],
        created_at=_NOW,
        updated_at=_NOW,
        request_id=_REQUEST_ID,
    )
    db_session.add(successor)
    db_session.flush()
    original.state = "superseded"
    original.counts_toward_pressure = False
    original.superseded_by_id = successor.id
    db_session.add(
        EvidenceTransition(
            evidence_id=original.id,
            from_state="sustained",
            to_state="superseded",
            occurred_on=date(2026, 3, 10),
            rule="signals.payment.delay_received.v1.forward",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    db_session.flush()

    after = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)
    assert len(after.evidence) == 1
    reconstructed = after.evidence[0]
    assert reconstructed.id == original.id
    assert reconstructed.state == "sustained"  # state as of the run, unchanged
    assert reconstructed.superseded_since is not None
    assert reconstructed.superseded_since.occurred_on == date(2026, 3, 10)
    assert reconstructed.superseded_since.rule == "signals.payment.delay_received.v1.forward"
    assert reconstructed.superseded_since.superseded_by_id == successor.id


def test_purged_source_named_with_rule(db_session: Session) -> None:
    purged_document_id = uuid4()
    world = _build_world(
        db_session, with_document=False, phantom_document_id=purged_document_id
    )
    db_session.add(
        RetentionPurgeLog(
            entity="document",
            criteria={
                "entity_id": str(purged_document_id),
                "rule": "statutory_7y retention expired",
            },
            purged_count=1,
            executed_at=datetime(2026, 2, 20, tzinfo=UTC),
            executed_by="retention-job",
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    db_session.flush()

    result = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)

    assert result.source_data.status is PartStatus.PURGED
    assert result.source_data.id == purged_document_id
    assert result.source_data.purged is not None
    assert result.source_data.purged.rule == "statutory_7y retention expired"
    assert result.source_data.purged.entity_id == purged_document_id
    assert result.source_data.purged.purged_count == 1


def test_missing_memo_marked_not_generated(db_session: Session) -> None:
    world = _build_world(db_session)

    result = world.service().reconstruct(world.principal, world.forecast.id, scope=world.scope)

    assert result.memo.status is PartStatus.NOT_GENERATED
    assert result.memo.drafted_text is None
    assert result.memo.id is None


def test_reconstruction_stable_after_later_changes(db_session: Session) -> None:
    world = _build_world(db_session)
    evidence = _add_evidence(world)
    _add_driver(world, evidence_id=evidence.id)
    _add_forecast_path(world)
    _add_trace(world)
    _add_memo(world)
    _add_override(world)
    _add_disposition(world)

    clock = FixedClock(_NOW)
    before = world.service(clock=clock).reconstruct(
        world.principal, world.forecast.id, scope=world.scope
    )
    before_payload = before.as_dict()

    # Unrelated later changes: a new threshold snapshot, a *new* covenant
    # version row (an amendment that leaves the original, reconstructed row
    # untouched — an in-place status flip on that same row is exercised
    # separately by test_uses_covenant_version_in_force_then), and a new
    # evidence event dated after the run's as_of_date.
    db_session.add(
        ThresholdSnapshot(
            values={"T1": {"act": "0.95"}},
            source="config",
            effective_from=datetime(2026, 4, 1, tzinfo=UTC),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    db_session.add(
        CovenantVersion(
            covenant_id=world.covenant.id,
            version_no=2,
            threshold=Decimal("1.35"),
            direction="min",
            unit="x",
            frequency="quarterly",
            test_basis="trailing_12m",
            effective_from=date(2026, 3, 1),
            status="live",
            tested_at_least_once=False,
            registered_by_id=uuid4(),
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    db_session.add(
        EvidenceItem(
            borrower_id=world.borrower.id,
            family="treasury",
            evidence_type="treasury_outflow",
            first_seen=date(2026, 3, 15),
            last_seen=date(2026, 3, 15),
            state="transient",
            counts_toward_pressure=True,
            source_event_ids=["evt-later"],
            created_at=_NOW,
            updated_at=_NOW,
            request_id=_REQUEST_ID,
        )
    )
    db_session.flush()

    after = world.service(clock=clock).reconstruct(
        world.principal, world.forecast.id, scope=world.scope
    )
    after_payload = after.as_dict()

    assert after_payload == before_payload
