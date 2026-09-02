"""Unit coverage for the why-panel's stage reader and its `explain` service
(`T-070`, `spec §17.6`, `plan.md §8.6`)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from covenant_radar.audit.trace_reader import present, stage_name, validate_subject_type
from covenant_radar.core.clock import FixedClock
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.base import Base
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.domain.trace import TraceReadRecord, TraceStage, stage_record
from covenant_radar.services.explain import explain

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as bound_session:
            yield bound_session
    finally:
        engine.dispose()


def _repository(session: Session, *, moment: datetime = _NOW) -> TraceRepository:
    return TraceRepository(session, clock=FixedClock(moment))


def _threshold(name: str = "T1", *, side: str = "above") -> dict[str, object]:
    return {"name": name, "value": "0.70", "observed": "0.71", "side": side}


def test_all_stages_returned_in_order(session: Session) -> None:
    subject = ("forecast", uuid4())
    repository = _repository(session)
    repository.write(
        subject,
        stage_record(
            TraceStage.INTAKE,
            "code",
            {"document_id": str(uuid4())},
            {"extracted_fields": 4},
            "intake.extract.v1",
            [],
            Decimal("1"),
            [],
        ),
    )
    repository.write(
        subject,
        stage_record(
            TraceStage.FORECAST,
            "code",
            {"slope": "0.02"},
            {"probability": "0.71"},
            "forecast.trend_pressure.v1",
            [_threshold()],
            Decimal("0.9"),
            [],
        ),
    )
    repository.write(
        subject,
        stage_record(
            TraceStage.MEMO,
            "model",
            {"borrower": "Acme"},
            {"drafted_text": "Acme is approaching its leverage covenant."},
            "memo.draft.v3",
            [],
            Decimal("0.8"),
            [],
        ),
    )

    result = explain(session, subject)

    assert [item.stage for item in result] == [1, 2, 3, 4, 5, 6, 7]
    assert [item.name for item in result] == [stage_name(stage) for stage in range(1, 8)]
    assert result[0].not_run is False
    assert result[0].name == "Intake"
    assert result[1].not_run is True  # stage 2, covenant engine, never written
    assert result[3].not_run is False
    assert result[3].name == "Forecast"
    assert result[3].thresholds_compared[0]["side"] == "above"
    assert result[6].not_run is False
    assert result[6].decider == "model"


def test_no_rows_returns_all_not_run(session: Session) -> None:
    subject = ("borrower", uuid4())

    result = explain(session, subject)

    assert len(result) == 7
    assert [item.stage for item in result] == list(range(1, 8))
    assert all(item.not_run for item in result)
    assert all(item.decider is None for item in result)
    assert all(item.inputs == {} and item.outputs == {} for item in result)
    assert all(item.confidence is None for item in result)
    assert all(item.thresholds_compared == () and item.sources == () for item in result)


def test_later_row_shown_earlier_retrievable(session: Session) -> None:
    subject = ("borrower", uuid4())
    repository = _repository(session, moment=_NOW)
    repository.write(
        subject,
        stage_record(
            TraceStage.EVIDENCE_LEDGER,
            "code",
            {"run": "1"},
            {"pressure": "0.10"},
            "ledger.score.v1",
            [_threshold("T3", side="below")],
            Decimal("0.6"),
            [],
        ),
        occurred_at=_NOW,
    )
    later = _repository(session, moment=_NOW.replace(hour=10))
    later.write(
        subject,
        stage_record(
            TraceStage.EVIDENCE_LEDGER,
            "code",
            {"run": "2"},
            {"pressure": "0.25"},
            "ledger.score.v1",
            [_threshold("T3", side="above")],
            Decimal("0.7"),
            [],
        ),
        occurred_at=_NOW.replace(hour=10),
    )

    result = explain(session, subject)
    stage_three = result[2]
    assert stage_three.outputs["pressure"] == "0.25"
    assert stage_three.thresholds_compared[0]["side"] == "above"

    history = TraceRepository(session).history(subject, stage=3)
    assert len(history) == 2
    assert history[0].outputs["pressure"] == "0.10"
    assert history[1].outputs["pressure"] == "0.25"


def test_model_stage_fields_present(session: Session) -> None:
    subject = ("forecast", uuid4())
    repository = _repository(session)
    repository.write(
        subject,
        stage_record(
            TraceStage.MEMO,
            "model",
            {"borrower": "Acme"},
            {"drafted_text": "Draft text.", "check_verdict": "grounded"},
            "memo.draft.v3",
            [],
            Decimal("0.82"),
            [{"type": "call_log", "id": str(uuid4())}],
        ),
    )

    result = explain(session, subject)
    memo_stage = result[6]

    assert memo_stage.decider == "model"
    assert memo_stage.rule_or_prompt_version == "memo.draft.v3"
    assert memo_stage.outputs["check_verdict"] == "grounded"
    assert {item.name for item in fields(memo_stage)} == {item.name for item in fields(result[0])}


def test_stored_rows_all_carry_sides() -> None:
    good = TraceReadRecord(
        stage=1,
        decider="code",
        inputs={},
        outputs={},
        rule_or_prompt_version=None,
        thresholds_compared=(),
        confidence=Decimal("1"),
        sources=(),
    )
    tampered = TraceReadRecord(
        stage=2,
        decider="code",
        inputs={},
        outputs={},
        rule_or_prompt_version="covenant.engine.v1",
        thresholds_compared=({"name": "T1", "value": "0.70", "observed": "0.71"},),
        confidence=Decimal("1"),
        sources=(),
    )
    remaining = tuple(
        TraceReadRecord(
            stage=stage,
            decider=None,
            inputs={},
            outputs={},
            rule_or_prompt_version=None,
            thresholds_compared=(),
            confidence=None,
            sources=(),
            not_run=True,
        )
        for stage in range(3, 8)
    )

    with pytest.raises(ValueError, match="missing a"):
        present((good, tampered, *remaining))


def test_unknown_subject_type_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="covenant_test.*borrower.*forecast"):
        validate_subject_type("case")

    with pytest.raises(ValidationError, match="covenant_test.*borrower.*forecast"):
        explain(session, ("case", uuid4()))
