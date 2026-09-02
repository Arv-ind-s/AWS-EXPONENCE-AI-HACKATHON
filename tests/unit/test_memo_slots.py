"""Unit coverage for T-099's record-only memo slot assembly."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from covenant_radar.domain.memo import (
    ABSENT_VALUE_TEXT,
    DEFAULT_MEMO_TEMPLATE,
    NO_SIMULATIONS_VALUE_TEXT,
    MemoRecord,
    MemoRecords,
    RecordReference,
    SlotState,
)
from covenant_radar.services.memo import MemoAssemblyService

pytestmark = pytest.mark.unit


def _record(record_type: str, **values: object) -> MemoRecord:
    return MemoRecord(RecordReference(record_type, uuid4()), values)


def _records(
    *,
    simulations: tuple[MemoRecord, ...] = (),
    recommendations: tuple[MemoRecord, ...] = (),
    covenant: MemoRecord | None = None,
) -> MemoRecords:
    return MemoRecords(
        situation=_record("triage_entry", situation="Projected pressure requires review."),
        covenant_position=covenant
        or _record(
            "forecast",
            ratio_name="Debt service coverage",
            value=Decimal("1.25"),
            threshold=Decimal("1.10"),
            headroom=Decimal("0.15"),
            probability=Decimal("0.42"),
            confidence=Decimal("0.88"),
            crossing_date=date(2026, 10, 15),
        ),
        drivers=(_record("forecast_driver", name="Cash-flow pressure", share=Decimal("0.60")),),
        evidence=(_record("evidence_item", citation="EV-001", count=3),),
        simulations=simulations,
        recommendations=recommendations,
    )


def test_every_slot_carries_a_record_reference() -> None:
    simulations = (
        _record(
            "simulation",
            code="REDUCE_DRAWING",
            text="Reduce the drawing limit.",
            projected_cross_date=date(2026, 12, 1),
            probability=Decimal("0.20"),
            delta_days=47,
            delta_probability=Decimal("-0.22"),
            assumptions=("The approved limit reduction takes effect immediately.",),
        ),
    )
    recommendations = (
        _record(
            "intervention",
            code="REDUCE_DRAWING",
            role_tag="credit",
            text="Reduce the drawing limit.",
            requires_approval=True,
        ),
    )

    result = MemoAssemblyService().assemble(
        _records(simulations=simulations, recommendations=recommendations)
    )

    assert result.all_resolved is True
    assert all(slot.record_references for slot in result)
    assert all(slot.state is SlotState.PRESENT for slot in result)


def test_absent_record_uses_documented_absence_text() -> None:
    records = _records()
    records = MemoRecords(
        situation=None,
        covenant_position=records.covenant_position,
        drivers=records.drivers,
        evidence=records.evidence,
    )

    slot = MemoAssemblyService().assemble(records)["situation"]

    assert slot.value == ABSENT_VALUE_TEXT
    assert slot.reason == "the situation record is absent"
    assert slot.record_references == ()


def test_suppressed_forecast_slot_carries_reason() -> None:
    covenant = _record(
        "forecast",
        ratio_name="Debt service coverage",
        value=Decimal("1.25"),
        threshold=Decimal("1.10"),
        headroom=Decimal("0.15"),
        probability=None,
        probability_suppressed=True,
        probability_suppression_reason="confidence is below the configured floor",
        confidence=Decimal("0.40"),
        crossing_date=None,
    )

    probability = MemoAssemblyService().assemble(_records(covenant=covenant))["probability"]

    assert probability.state is SlotState.SUPPRESSED
    assert "confidence is below the configured floor" in str(probability.value)
    assert probability.reason == "confidence is below the configured floor"
    assert probability.record_reference is not None


def test_no_simulations_section_still_present() -> None:
    result = MemoAssemblyService().assemble(_records())

    slot = result["simulation_options"]
    assert slot.value == NO_SIMULATIONS_VALUE_TEXT
    assert slot.reason == "no simulations are recorded for this borrower"
    assert result.get("simulation_options") is slot


def test_assembly_computes_no_value() -> None:
    headroom = Decimal("0.1500")
    source = _records(
        covenant=MemoRecord(
            RecordReference("forecast", "forecast-1"),
            {
                "ratio_name": "Debt service coverage",
                "value": Decimal("1.25"),
                "threshold": Decimal("1.10"),
                "headroom": headroom,
                "probability": Decimal("0.42"),
                "confidence": Decimal("0.88"),
                "crossing_date": date(2026, 10, 15),
            },
        )
    )

    result = MemoAssemblyService().assemble(source)

    assert result["value"].value == Decimal("1.25")
    assert result["threshold"].value == Decimal("1.10")
    assert result["headroom"].value is headroom
    assert result["probability"].value == Decimal("0.42")
    assert result["crossing_date"].value == date(2026, 10, 15)
    assert result["value"].record_reference == source.covenant_position.reference


def test_template_sections_fixed() -> None:
    assert DEFAULT_MEMO_TEMPLATE.section_names == (
        "situation",
        "covenant_position",
        "drivers",
        "evidence_citations",
        "simulated_options",
        "recommended_interventions",
        "advisory_closing",
    )
    assert DEFAULT_MEMO_TEMPLATE.slot_names == (
        "situation",
        "ratio_name",
        "value",
        "threshold",
        "headroom",
        "probability",
        "confidence",
        "crossing_date",
        "drivers",
        "evidence_counts",
        "simulation_options",
        "recommended_interventions",
        "intervention_text",
    )
