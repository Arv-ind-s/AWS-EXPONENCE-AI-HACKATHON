"""Integration coverage for T-101: memo refusal, retry and persistence.

Uses an isolated in-memory SQLite schema (the same pattern
`tests/security/test_audit_coverage.py` already relies on) rather than the
PostgreSQL-only `tests/integration/conftest.py` fixtures, so this suite does
not depend on `COVENANT_RADAR_DATABASE_URL` being configured.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from covenant_radar.ai.budget import BudgetLedger, BudgetLimits
from covenant_radar.ai.client import InMemoryModelCallWriter, ModelClient
from covenant_radar.ai.errors import ProviderUnavailable
from covenant_radar.ai.registry import PRODUCTION, ModelRegistryGuard
from covenant_radar.ai.shapes import CatalogueAction
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import FixedClock
from covenant_radar.db.base import Base
from covenant_radar.db.models.audit import AuditEvent, TraceRow
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.forecast import ForecastRun
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.workflow import Memo
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.domain.memo import MemoRecord, MemoRecords, RecordReference
from covenant_radar.ports.llm import CompletionRequest, CompletionResponse
from covenant_radar.services.memo import (
    DEGRADED_MEMO_MESSAGE,
    MODEL_GOVERNANCE_MEMO_MESSAGE,
    MemoGenerationService,
    MemoOutcomeKind,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class _RecordingProvider:
    provider_name = "fixture"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            text=self.reply,
            model="fixture-model",
            input_tokens=20,
            output_tokens=40,
            latency_ms=1,
            raw_payload={"fixture": True},
        )


class _SequenceProvider(_RecordingProvider):
    def __init__(self, replies: list[str]) -> None:
        super().__init__(replies[0])
        self.replies = replies

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        reply = self.replies.pop(0)
        return CompletionResponse(
            text=reply,
            model="fixture-model",
            input_tokens=20,
            output_tokens=40,
            latency_ms=1,
            raw_payload={"fixture": True},
        )


class _FailingProvider:
    provider_name = "fixture"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        raise ProviderUnavailable("fixture", reason="transport failure")


class _MissingModelRegistration:
    def get_by_component(self, component: str):
        return None


def _records() -> MemoRecords:
    return MemoRecords(
        situation=MemoRecord(
            RecordReference("triage", "triage-1"),
            {"situation": "Projected pressure requires review."},
        ),
        covenant_position=MemoRecord(
            RecordReference("forecast", "forecast-1"),
            {
                "ratio_name": "Debt service coverage",
                "value": Decimal("1.25"),
                "threshold": Decimal("1.10"),
                "headroom": Decimal("0.15"),
                "probability": Decimal("0.42"),
                "confidence": Decimal("0.88"),
                "crossing_date": date(2026, 10, 15),
            },
        ),
        drivers=(
            MemoRecord(RecordReference("driver", "driver-1"), {"name": "Cash-flow pressure"}),
        ),
        evidence=(
            MemoRecord(
                RecordReference("evidence", "evidence-1"),
                {"citation": "EV-001", "count": 3},
            ),
        ),
        recommendations=(
            MemoRecord(
                RecordReference("intervention", "intervention-1"),
                {
                    "code": "CREDIT-REDUCE",
                    "role_tag": "credit",
                    "text": "Review and reduce funded exposure.",
                },
            ),
        ),
    )


def _catalogue():
    return (
        CatalogueAction(
            id="CREDIT-REDUCE",
            role_tag="credit",
            text="Review and reduce funded exposure.",
        ),
    )


def _good_reply() -> str:
    return json.dumps(
        {
            "headline": (
                "Debt service coverage is projected to reach the action point on 2026-10-15."
            ),
            "summary": (
                "The recorded value is 1.25 against a threshold of 1.10, with headroom of "
                "0.15. The projected breach probability is 0.42 at confidence 0.88."
            ),
            "drivers": ["ROLE_DRIVER_1"],
            "actions": [{"id": "CREDIT-REDUCE", "role_tag": "credit"}],
            "recommended_next_step": "Review and reduce funded exposure.",
            "disclaimer": "human credit review is required before action",
        }
    )


def _fabricated_reply() -> str:
    return json.dumps(
        {
            "headline": (
                "Debt service coverage is projected to reach the action point on 2026-10-15."
            ),
            "summary": "The fabricated value is 9.99.",
            "drivers": ["ROLE_DRIVER_1"],
            "actions": [{"id": "CREDIT-REDUCE", "role_tag": "credit"}],
            "recommended_next_step": "Review and reduce funded exposure.",
            "disclaimer": "human credit review is required before action",
        }
    )


def _schema_session() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def _portfolio_and_borrower(session: Session, *, reference: str) -> tuple[Portfolio, Borrower]:
    portfolio = Portfolio.create(
        code=f"ROOT-{reference}",
        name="Root",
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t101-setup",
    )
    session.add(portfolio)
    session.flush()
    borrower = Borrower(
        reference=reference,
        legal_name=f"{reference} Private Limited",
        portfolio_id=portfolio.id,
        created_at=_NOW,
        updated_at=_NOW,
        request_id="rq-t101-setup",
    )
    session.add(borrower)
    session.flush()
    return portfolio, borrower


def _service(session: Session, client: ModelClient, *, request_id: str) -> MemoGenerationService:
    audit = AuditRecorder(AuditRepository(session), clock=FixedClock(_NOW), request_id=request_id)
    return MemoGenerationService(
        session, client=client, audit=audit, clock=FixedClock(_NOW), request_id=request_id
    )


def test_two_failures_write_no_memo_row() -> None:
    engine, session = _schema_session()
    try:
        _, borrower = _portfolio_and_borrower(session, reference="B-000001")
        provider = _SequenceProvider([_fabricated_reply(), _fabricated_reply()])
        client = ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter())
        service = _service(session, client, request_id="rq-t101-refuse")

        before = session.scalar(select(func.count(Memo.id)))
        outcome = service.generate(
            borrower_id=borrower.id, records=_records(), catalogue=_catalogue()
        )
        after = session.scalar(select(func.count(Memo.id)))

        assert outcome.kind is MemoOutcomeKind.REFUSED
        assert outcome.memo is None
        assert before == 0
        assert after == 0
        assert len(provider.requests) == 2
    finally:
        session.close()
        engine.dispose()


def test_refusal_traced_and_audited() -> None:
    engine, session = _schema_session()
    try:
        _, borrower = _portfolio_and_borrower(session, reference="B-000002")
        provider = _SequenceProvider([_fabricated_reply(), _fabricated_reply()])
        client = ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter())
        service = _service(session, client, request_id="rq-t101-trace")

        outcome = service.generate(
            borrower_id=borrower.id, records=_records(), catalogue=_catalogue()
        )
        assert outcome.kind is MemoOutcomeKind.REFUSED
        assert outcome.failed_checks

        trace_rows = session.scalars(
            select(TraceRow).where(
                TraceRow.subject_type == "borrower", TraceRow.subject_id == borrower.id
            )
        ).all()
        assert len(trace_rows) == 1
        assert trace_rows[0].stage == "7"
        assert trace_rows[0].decider == "model"
        assert trace_rows[0].outputs["verdict"] == "refused"
        assert trace_rows[0].outputs["failed_checks"]

        audit_events = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "memo_refused")
        ).all()
        assert len(audit_events) == 1
        assert audit_events[0].subject_id == borrower.id
        assert audit_events[0].payload["attempts"] == 2
        assert audit_events[0].payload["failed_checks"]
    finally:
        session.close()
        engine.dispose()


def test_regeneration_feeds_back_failure_detail() -> None:
    engine, session = _schema_session()
    try:
        _, borrower = _portfolio_and_borrower(session, reference="B-000003")
        provider = _SequenceProvider([_fabricated_reply(), _good_reply()])
        client = ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter())
        service = _service(session, client, request_id="rq-t101-retry")

        outcome = service.generate(
            borrower_id=borrower.id, records=_records(), catalogue=_catalogue()
        )

        assert outcome.kind is MemoOutcomeKind.GENERATED
        assert outcome.drafting is not None
        assert outcome.drafting.attempts == 2
        assert len(provider.requests) == 2
        assert "9.99" in provider.requests[1].messages[-1].content

        memo_count = session.scalar(select(func.count(Memo.id)))
        assert memo_count == 1
        trace = session.scalar(
            select(TraceRow).where(
                TraceRow.subject_type == "borrower",
                TraceRow.subject_id == borrower.id,
                TraceRow.stage == "7",
            )
        )
        assert trace is not None
        explanation = trace.outputs["explanation"]
        assert explanation["headline"].startswith("Debt service coverage is projected")
        assert explanation["recommended_next_step"] == "Review and reduce funded exposure."
    finally:
        session.close()
        engine.dispose()


def test_provider_unavailable_degrades_screen_intact() -> None:
    engine, session = _schema_session()
    try:
        _, borrower_a = _portfolio_and_borrower(session, reference="B-000004")
        good_provider = _RecordingProvider(_good_reply())
        good_client = ModelClient(
            good_provider, model="fixture-model", model_calls=InMemoryModelCallWriter()
        )
        baseline_service = _service(session, good_client, request_id="rq-t101-baseline")
        baseline = baseline_service.generate(
            borrower_id=borrower_a.id, records=_records(), catalogue=_catalogue()
        )
        assert baseline.kind is MemoOutcomeKind.GENERATED
        assert baseline.memo is not None
        baseline_memo_id = baseline.memo.id

        _, borrower_b = _portfolio_and_borrower(session, reference="B-000005")
        failing_provider = _FailingProvider()
        failing_client = ModelClient(
            failing_provider, model="fixture-model", model_calls=InMemoryModelCallWriter()
        )
        degraded_service = _service(session, failing_client, request_id="rq-t101-degraded")

        outcome = degraded_service.generate(
            borrower_id=borrower_b.id, records=_records(), catalogue=_catalogue()
        )

        assert outcome.kind is MemoOutcomeKind.PROVIDER_UNAVAILABLE
        assert outcome.message == DEGRADED_MEMO_MESSAGE
        assert outcome.memo is None

        # Everything already on the screen is unaffected by the degraded call.
        assert session.get(Memo, baseline_memo_id) is not None
        assert (
            session.scalar(select(func.count(Memo.id)).where(Memo.borrower_id == borrower_b.id))
            == 0
        )
    finally:
        session.close()
        engine.dispose()


def test_model_governance_refusal_degrades_with_actionable_reason() -> None:
    engine, session = _schema_session()
    try:
        _, borrower = _portfolio_and_borrower(session, reference="B-000005-GOV")
        provider = _RecordingProvider(_good_reply())
        client = ModelClient(
            provider,
            model="fixture-model",
            model_calls=InMemoryModelCallWriter(),
            registry_guard=ModelRegistryGuard(
                _MissingModelRegistration(),  # type: ignore[arg-type]
                environment=PRODUCTION,
            ),
        )
        service = _service(session, client, request_id="rq-t101-governance")

        outcome = service.generate(
            borrower_id=borrower.id, records=_records(), catalogue=_catalogue()
        )

        assert outcome.kind is MemoOutcomeKind.PROVIDER_UNAVAILABLE
        assert outcome.message == MODEL_GOVERNANCE_MEMO_MESSAGE
        assert outcome.memo is None
        assert provider.requests == []
        assert (
            session.scalar(select(func.count(Memo.id)).where(Memo.borrower_id == borrower.id)) == 0
        )
    finally:
        session.close()
        engine.dispose()


def test_ceiling_queues_with_banner() -> None:
    engine, session = _schema_session()
    try:
        _, borrower = _portfolio_and_borrower(session, reference="B-000006")
        ledger = BudgetLedger(
            BudgetLimits(calls_per_hour=1, calls_per_day=5), clock=FixedClock(_NOW)
        )
        # Consume the one hourly slot before the memo request is attempted, so
        # the very next reservation raises CeilingReached with no call made.
        ledger.reserve(estimated_cost=Decimal(0))
        provider = _RecordingProvider(_good_reply())
        client = ModelClient(
            provider,
            model="fixture-model",
            model_calls=InMemoryModelCallWriter(),
            budget=ledger,
        )
        service = _service(session, client, request_id="rq-t101-ceiling")

        outcome = service.generate(
            borrower_id=borrower.id, records=_records(), catalogue=_catalogue()
        )

        assert outcome.kind is MemoOutcomeKind.CEILING_REACHED
        assert outcome.dimension == "hourly"
        assert outcome.retry_at is not None
        assert outcome.message is not None
        assert provider.requests == []
        assert session.scalar(select(func.count(Memo.id))) == 0
    finally:
        session.close()
        engine.dispose()


def test_memo_retained_after_forecast_superseded() -> None:
    engine, session = _schema_session()
    try:
        _, borrower = _portfolio_and_borrower(session, reference="B-000007")
        run_one = ForecastRun(
            id=uuid4(),
            as_of_date=date(2026, 8, 1),
            started_at=_NOW,
            finished_at=_NOW,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t101-run-one",
        )
        session.add(run_one)
        session.flush()

        provider = _RecordingProvider(_good_reply())
        client = ModelClient(provider, model="fixture-model", model_calls=InMemoryModelCallWriter())
        service = _service(session, client, request_id="rq-t101-supersede")

        outcome = service.generate(
            borrower_id=borrower.id,
            records=_records(),
            catalogue=_catalogue(),
            run_id=run_one.id,
        )
        assert outcome.kind is MemoOutcomeKind.GENERATED
        assert outcome.memo is not None
        memo_id = outcome.memo.id
        original_drafted_text = outcome.memo.drafted_text

        run_two = ForecastRun(
            id=uuid4(),
            as_of_date=date(2026, 8, 8),
            started_at=_NOW,
            finished_at=_NOW,
            state="complete",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-t101-run-two",
        )
        session.add(run_two)
        run_one.state = "superseded"
        session.flush()

        retained = session.get(Memo, memo_id)
        assert retained is not None
        assert retained.run_id == run_one.id
        assert retained.drafted_text == original_drafted_text
    finally:
        session.close()
        engine.dispose()
