"""Unit coverage for deterministic portfolio triage (T-059)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from covenant_radar.domain.triage import (
    ACT_BAND,
    AMBER_BAND,
    NO_FORECAST_STATE,
    SUPPRESSED_STATE,
    WATCH_BAND,
    ForecastFact,
    TriageInput,
    TriageThresholds,
    rank,
)

pytestmark = pytest.mark.unit

_THRESHOLDS = TriageThresholds(
    act=Decimal("0.70"),
    amber=Decimal("0.40"),
    confidence_floor=Decimal("0.50"),
)


class _ThresholdStore:
    def get(self, name: str) -> dict[str, Decimal]:
        if name == "T1":
            return {"act": Decimal("0.70"), "amber": Decimal("0.40")}
        if name == "T2":
            return {"confidence_floor": Decimal("0.50")}
        raise KeyError(name)


def _forecast(
    probability: str | None,
    *,
    horizon: int = 30,
    confidence: str | None = "0.80",
    below_floor: bool = False,
    reason: str | None = None,
    covenant_id: UUID | None = None,
) -> ForecastFact:
    return ForecastFact(
        covenant_version_id=covenant_id or uuid4(),
        horizon_days=horizon,
        probability=probability,
        confidence=confidence,
        below_confidence_floor=below_floor,
        reason=reason,
    )


def _borrower(
    reference: str,
    exposure: str,
    forecasts: tuple[ForecastFact, ...],
) -> TriageInput:
    return TriageInput(
        borrower_id=uuid4(),
        reference=reference,
        exposure=Decimal(exposure),
        forecasts=forecasts,
    )


@pytest.mark.parametrize(
    ("probability", "expected"),
    (
        ("0.70", ACT_BAND),
        ("0.40", AMBER_BAND),
        ("0.399999999", WATCH_BAND),
    ),
)
def test_boundary_probabilities_band_correctly(
    probability: str,
    expected: str,
) -> None:
    result = rank(
        [_borrower("B-000001", "100", (_forecast(probability),))],
        _ThresholdStore(),
    )

    assert result[0].band == expected


def test_worst_horizon_selected() -> None:
    first_id = uuid4()
    second_id = uuid4()
    result = rank(
        [
            _borrower(
                "B-000001",
                "100",
                (
                    _forecast("0.30", horizon=30, covenant_id=first_id),
                    _forecast("0.80", horizon=90, covenant_id=second_id),
                ),
            )
        ],
        _THRESHOLDS,
    )

    assert result[0].worst_horizon == 90
    assert result[0].worst_covenant_version_id == second_id
    assert result[0].probability == Decimal("0.80")
    assert result[0].urgency == Decimal("64.00")
    assert result[0].why["worst_horizon_rule"]


def test_tie_break_exposure_then_reference() -> None:
    same_urgency = "0.50"
    result = rank(
        [
            _borrower("B-000003", "100", (_forecast(same_urgency),)),
            _borrower("B-000002", "100", (_forecast(same_urgency),)),
            _borrower("B-000001", "120", (_forecast("0.50"),)),
        ],
        _THRESHOLDS,
    )

    assert [entry.reference for entry in result] == [
        "B-000001",
        "B-000002",
        "B-000003",
    ]


def test_applied_tie_break_recorded() -> None:
    result = rank(
        [
            _borrower("B-000002", "100", (_forecast("0.50"),)),
            _borrower("B-000001", "100", (_forecast("0.50"),)),
        ],
        _THRESHOLDS,
    )

    for entry in result:
        assert "exposure descending" in entry.applied_tie_break
        assert "borrower reference ascending" in entry.applied_tie_break
        assert entry.why["tie_break_rule"] == entry.tie_break_rule


def test_no_forecast_borrower_included_with_reason() -> None:
    result = rank(
        [
            _borrower("B-000001", "100", ()),
            _borrower("B-000002", "10", (_forecast("0.10"),)),
        ],
        _THRESHOLDS,
    )

    assert result[-1].reference == "B-000001"
    assert result[-1].state == NO_FORECAST_STATE
    assert result[-1].band == WATCH_BAND
    assert "no forecast" in result[-1].reason


def test_suppressed_forecast_banded_watch_with_reason() -> None:
    result = rank(
        [
            _borrower(
                "B-000001",
                "100",
                (_forecast("0.90", confidence="0.49", below_floor=True),),
            )
        ],
        _THRESHOLDS,
    )

    assert result[0].state == SUPPRESSED_STATE
    assert result[0].suppressed is True
    assert result[0].probability is None
    assert result[0].urgency is None
    assert result[0].band == WATCH_BAND
    assert "T2" in result[0].reason
    assert "0.49" in result[0].reason


def test_confidence_floor_is_inclusive_for_ranking() -> None:
    result = rank(
        [_borrower("B-000001", "100", (_forecast("0.70", confidence="0.50"),))],
        _THRESHOLDS,
    )

    assert result[0].state == "available"
    assert result[0].band == ACT_BAND
    assert result[0].urgency == Decimal("35.00")


def test_threshold_store_is_read_for_t1_and_t2() -> None:
    configured = TriageThresholds.from_store(_ThresholdStore())
    assert configured == _THRESHOLDS
