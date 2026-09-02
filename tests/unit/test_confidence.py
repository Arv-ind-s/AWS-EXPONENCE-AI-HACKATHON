"""Unit coverage for the forecast confidence model (T-055)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import (
    ConfidenceResult,
    ConfidenceThresholds,
    confidence,
)

pytestmark = pytest.mark.unit


class _ThresholdStore:
    def __init__(self, floor: Decimal = Decimal("0.50")) -> None:
        self.floor = floor
        self.requested: list[str] = []

    def get(self, name: str) -> dict[str, Decimal]:
        self.requested.append(name)
        return {"confidence_floor": self.floor}


def test_exactly_at_t2_is_shown() -> None:
    result = confidence(
        completeness=Decimal("0.5"),
        evidence_support=Decimal("1"),
        staleness_days=0,
        confidence_floor=Decimal("0.50"),
    )

    assert isinstance(result, ConfidenceResult)
    assert result.confidence == Decimal("0.5")
    assert result.confidence_floor == Decimal("0.50")
    assert result.below_confidence_floor is False
    assert result.probability_suppressed is False
    assert result.shown is True
    assert "inclusive" in result.reason


def test_zero_periods_zero_confidence_with_reason() -> None:
    result = confidence(
        completeness=Decimal("0"),
        evidence_support=Decimal("1"),
        staleness_days=0,
    )

    assert result.confidence == Decimal("0")
    assert result.completeness_factor == Decimal("0")
    assert result.probability_suppressed is True
    assert result.probability_absent is True
    assert result.reason == (
        "no complete periods available; confidence is zero and probability is absent"
    )
    assert result.limiting_factor == "completeness"


def test_stale_test_applies_factor_and_names_it() -> None:
    result = confidence(
        completeness=Decimal("1"),
        evidence_support=Decimal("1"),
        staleness_days=2,
    )

    assert result.staleness_factor == Decimal(1) / Decimal(3)
    assert result.confidence == Decimal(1) / Decimal(3)
    assert result.limiting_factor == "staleness"
    assert "staleness" in result.reason
    assert result.formula_inputs["staleness_factor"] == Decimal(1) / Decimal(3)


def test_all_factors_maximum_gives_one() -> None:
    result = confidence(
        completeness=Decimal("1"),
        evidence_support=Decimal("1"),
        staleness_days=0,
    )

    assert result.confidence == Decimal("1")
    assert result.below_confidence_floor is False
    assert result.probability_suppressed is False
    assert result.limiting_factor == "none"
    assert result.limiting_value == Decimal("1")
    assert "maximum" in result.reason


@pytest.mark.parametrize(
    ("completeness", "evidence_support", "staleness_days", "expected"),
    (
        (Decimal("0.4"), Decimal("1"), 0, "completeness"),
        (Decimal("1"), Decimal("0.4"), 0, "evidence_support"),
        (Decimal("1"), Decimal("1"), 2, "staleness"),
        (Decimal("1"), Decimal("1"), 0, "none"),
    ),
)
def test_limiting_factor_always_recorded(
    completeness: Decimal,
    evidence_support: Decimal,
    staleness_days: int,
    expected: str,
) -> None:
    result = confidence(completeness, evidence_support, staleness_days)

    assert result.limiting_factor == expected
    assert result.limiting_factor_name == expected
    assert result.reason
    assert set(result.factors_by_name) == {"completeness", "evidence_support", "staleness"}


def test_confidence_floor_is_read_from_t2_configuration() -> None:
    store = _ThresholdStore(Decimal("0.60"))
    result = confidence(Decimal("0.5"), Decimal("1"), 0, store)

    assert store.requested == ["T2"]
    assert isinstance(ConfidenceThresholds.from_store(store), ConfidenceThresholds)
    assert result.confidence_floor == Decimal("0.60")
    assert result.below_confidence_floor is True
    assert result.probability_suppressed is True
    assert "T2" in result.reason
