"""Unit coverage for the shared stage trace value object and reader shape."""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.domain.trace import TraceRecord, stage_record

pytestmark = pytest.mark.unit


def _record(**overrides: object) -> TraceRecord:
    values: dict[str, object] = {
        "stage": 2,
        "decider": "code",
        "inputs": {"observed": "2.4"},
        "outputs": {"verdict": "warning"},
        "rule_or_prompt_version": "covenant.engine.v1",
        "thresholds_compared": [
            {"name": "covenant_threshold", "value": "2.5", "observed": "2.4", "side": "below"}
        ],
        "confidence": Decimal("1"),
        "sources": [{"type": "financial_period", "id": str(uuid4())}],
    }
    values.update(overrides)
    return stage_record(**values)  # type: ignore[arg-type]


def test_entry_without_side_raises() -> None:
    with pytest.raises(ValueError, match="covenant_threshold.*side"):
        _record(
            thresholds_compared=[{"name": "covenant_threshold", "value": "2.5", "observed": "3.0"}]
        )


def test_stage_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="outside the defined range"):
        _record(stage=8)


def test_non_serialisable_coerced_not_lost() -> None:
    value = object()
    record = _record(inputs={"opaque": value})

    assert record.inputs["opaque"] == str(value)
    assert record.inputs["_coercions"] == [{"path": "inputs.opaque", "type": "object"}]


def test_read_pads_missing_stages_as_not_run() -> None:
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from covenant_radar.core.clock import FixedClock
    from covenant_radar.db.base import Base
    from covenant_radar.db.repositories.trace import TraceRepository

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    subject = ("covenant_test", uuid4())
    try:
        with Session(engine) as session:
            repository = TraceRepository(
                session,
                clock=FixedClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC)),
            )
            rows = repository.read(subject)

            assert len(rows) == 7
            assert [row.stage for row in rows] == list(range(1, 8))
            assert all(row.not_run for row in rows)
            assert all(row.inputs == {} and row.outputs == {} for row in rows)
    finally:
        engine.dispose()


def test_code_and_model_stages_share_one_field_set() -> None:
    code_fields = {item.name for item in fields(_record())}
    model_fields = {item.name for item in fields(_record(stage=7, decider="model"))}

    assert (
        code_fields
        == model_fields
        == {
            "stage",
            "decider",
            "inputs",
            "outputs",
            "rule_or_prompt_version",
            "thresholds_compared",
            "confidence",
            "sources",
        }
    )
