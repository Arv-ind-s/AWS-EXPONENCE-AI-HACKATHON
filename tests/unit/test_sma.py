from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from covenant_radar.domain.covenants.sma import (
    MISSING_CONDUCT_REASON,
    MISSING_DAYS_REASON,
    FacilityConductFacts,
    SmaBand,
    derive_borrower_sma,
    derive_facility_sma,
    sma_band,
)
from covenant_radar.services.engine import EngineService


@pytest.mark.parametrize(
    ("days_past_due", "expected"),
    (
        (0, SmaBand.NONE),
        (1, SmaBand.SMA_0),
        (30, SmaBand.SMA_0),
        (31, SmaBand.SMA_1),
        (60, SmaBand.SMA_1),
        (61, SmaBand.SMA_2),
        (90, SmaBand.SMA_2),
        (91, SmaBand.BEYOND),
    ),
)
def test_every_boundary_value(days_past_due: int, expected: SmaBand) -> None:
    assert sma_band(days_past_due) is expected


def test_negative_days_raises() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        sma_band(-1)


def test_borrower_band_is_worst_across_facilities() -> None:
    conduct_date = date(2026, 8, 31)
    result = derive_borrower_sma(
        {
            "F-001": FacilityConductFacts("F-001", conduct_date, 4),
            "F-002": FacilityConductFacts("F-002", conduct_date, 65),
            "F-003": FacilityConductFacts("F-003", conduct_date, 91),
        },
        borrower_id="B-001",
        as_of_date=conduct_date,
    )

    assert result.band is SmaBand.BEYOND
    assert result.worst_facility is not None
    assert result.worst_facility.facility_id == "F-003"
    assert [item.band for item in result.facilities] == [
        SmaBand.SMA_0,
        SmaBand.SMA_2,
        SmaBand.BEYOND,
    ]


def test_missing_conduct_records_reason_not_assumption() -> None:
    result = derive_borrower_sma(
        {"F-001": None},
        borrower_id="B-001",
        as_of_date=date(2026, 8, 31),
    )

    assert result.band is SmaBand.NONE
    assert result.reason == MISSING_CONDUCT_REASON
    assert len(result.facilities) == 1
    assert result.facilities[0].band is SmaBand.NONE
    assert result.facilities[0].days_past_due is None
    assert result.facilities[0].reason == MISSING_CONDUCT_REASON


def test_missing_days_past_due_records_reason_not_assumption() -> None:
    conduct_date = date(2026, 8, 31)
    result = derive_borrower_sma(
        {"F-001": FacilityConductFacts("F-001", conduct_date, None)},
        borrower_id="B-001",
        as_of_date=conduct_date,
    )

    assert result.band is SmaBand.NONE
    assert result.reason == MISSING_DAYS_REASON
    assert result.facilities[0].days_past_due is None
    assert result.facilities[0].reason == MISSING_DAYS_REASON


def test_conduct_from_another_date_is_missing_for_requested_day() -> None:
    requested_date = date(2026, 8, 31)
    result = derive_borrower_sma(
        {
            "F-001": FacilityConductFacts("F-001", date(2026, 8, 30), 65),
        },
        as_of_date=requested_date,
        facility_ids=("F-001",),
    )

    assert result.band is SmaBand.NONE
    assert result.facilities[0].reason == MISSING_CONDUCT_REASON


def test_facility_identifier_is_normalised_in_derivation() -> None:
    conduct_date = date(2026, 8, 31)
    result = derive_facility_sma(
        {"as_of_date": conduct_date, "days_past_due": 31},
        facility_id=" F-001 ",
        as_of_date=conduct_date,
    )

    assert result.facility_id == "F-001"
    assert result.band is SmaBand.SMA_1


def test_duplicate_conduct_rows_for_same_facility_and_date_are_rejected() -> None:
    conduct_date = date(2026, 8, 31)
    rows = (
        FacilityConductFacts("F-001", conduct_date, 1),
        FacilityConductFacts("F-001", conduct_date, 2),
    )

    with pytest.raises(ValueError, match="More than one conduct row"):
        derive_borrower_sma(rows, as_of_date=conduct_date)


def test_engine_records_sma_derivation_in_stage_two_trace() -> None:
    class TraceSink:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object]] = []

        def write(self, subject: object, trace: object, **_: object) -> None:
            self.calls.append((subject, trace))

    service = EngineService.__new__(EngineService)
    principal = SimpleNamespace(id=uuid4())
    trace_sink = TraceSink()
    service.request_id = "request-1"
    service.traces = trace_sink
    service._read_context = lambda supplied_principal, scope: (supplied_principal, scope)
    service._now = lambda: datetime(2026, 8, 31, tzinfo=UTC)
    borrower_id = uuid4()
    conduct_date = date(2026, 8, 31)

    result = service._derive_sma(
        principal,
        borrower_id,
        as_of_date=conduct_date,
        conduct={"F-001": {"days_past_due": 65}},
    )

    assert result.band is SmaBand.SMA_2
    assert len(trace_sink.calls) == 1
    subject, trace = trace_sink.calls[0]
    assert subject == ("borrower_sma", borrower_id)
    assert trace.stage == 2
    assert trace.outputs["sma_band"] == "SMA-2"
    assert trace.outputs["facility_bands"][0]["days_past_due"] == 65
    assert "sma_band" not in trace.inputs["facility_conduct"][0]


def test_engine_loads_conduct_snapshot_when_input_is_omitted() -> None:
    class TraceSink:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object]] = []

        def write(self, subject: object, trace: object, **_: object) -> None:
            self.calls.append((subject, trace))

    service = EngineService.__new__(EngineService)
    principal = SimpleNamespace(id=uuid4())
    trace_sink = TraceSink()
    first_facility = uuid4()
    second_facility = uuid4()
    conduct_date = date(2026, 8, 31)
    service.request_id = "request-1"
    service.traces = trace_sink
    service._read_context = lambda supplied_principal, scope: (supplied_principal, scope)
    service._now = lambda: datetime(2026, 8, 31, tzinfo=UTC)
    service._sma_conduct_snapshot = lambda borrower_id, as_of_date, scope: (
        (first_facility, second_facility),
        {
            first_facility: {
                "facility_id": first_facility,
                "as_of_date": as_of_date,
                "days_past_due": 1,
            },
            second_facility: {
                "facility_id": second_facility,
                "as_of_date": as_of_date,
                "days_past_due": 65,
            },
        },
    )

    result = service._derive_sma(
        principal,
        uuid4(),
        as_of_date=conduct_date,
        conduct=None,
    )

    assert result.band is SmaBand.SMA_2
    assert [item.facility_id for item in result.facilities] == [
        first_facility,
        second_facility,
    ]
    assert result.worst_facility is not None
    assert result.worst_facility.facility_id == second_facility
    assert len(trace_sink.calls) == 1
