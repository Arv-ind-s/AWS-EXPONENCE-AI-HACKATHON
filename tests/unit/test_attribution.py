"""Unit coverage for forecast driver attribution (T-057)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.forecast.attribution import (
    AttributionThresholds,
    DriverShare,
    attribute,
)

pytestmark = pytest.mark.unit

_T5 = Decimal("0.10")


class _ThresholdStore:
    def __init__(self, share: Decimal) -> None:
        self.share = share
        self.requested: list[str] = []

    def get(self, name: str) -> dict[str, Decimal]:
        self.requested.append(name)
        return {"contribution_share": self.share}


def test_exactly_t5_is_listed() -> None:
    result = attribute(
        {"trend": Decimal("0.10"), "evidence": Decimal("0.90")},
        _T5,
    )

    assert isinstance(result[0], DriverShare)
    assert [row.name for row in result] == ["trend", "evidence"]
    assert result[0].share == _T5
    assert sum((row.share for row in result), Decimal("0")) == Decimal("1")


def test_all_below_t5_single_other_row() -> None:
    terms = {f"factor_{index}": Decimal("0.05") for index in range(20)}

    result = attribute(terms, _T5)

    assert result == [DriverShare("other", Decimal("1"))]


def test_negative_contribution_named() -> None:
    result = attribute(
        {"trend": Decimal("0.80"), "data_quality": Decimal("-0.20")},
        _T5,
    )

    assert [row.name for row in result] == ["trend", "data_quality"]
    assert result[1].share < Decimal("0")
    total = sum((row.share for row in result), Decimal("0"))
    assert abs(float(total - Decimal("1"))) <= 1e-12


def test_zero_total_documented_neutral() -> None:
    result = attribute({"trend": Decimal("0"), "evidence": Decimal("0")}, _T5)

    assert result == [
        DriverShare(
            "neutral",
            Decimal("1"),
            "all driver contributions are zero; no risk delta can be attributed",
        )
    ]
    assert result[0].is_neutral is True


def test_threshold_read_from_store() -> None:
    store = _ThresholdStore(Decimal("0.25"))
    result = attribute(
        {"trend": Decimal("0.20"), "evidence": Decimal("0.80")},
        store,
    )

    assert store.requested == ["T5"]
    assert AttributionThresholds.from_store(store).contribution_share == Decimal("0.25")
    assert [row.name for row in result] == ["evidence", "other"]
    assert result[0].share == Decimal("0.80")
    assert result[1].share == Decimal("0.20")


@pytest.mark.parametrize("reserved_name", ("other", "neutral"))
def test_reserved_output_names_are_rejected(reserved_name: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        attribute({reserved_name: Decimal("1")}, _T5)


@pytest.mark.parametrize(
    ("terms", "threshold"),
    (
        ({"trend": Decimal("NaN")}, _T5),
        ({"trend": Decimal("Infinity")}, _T5),
        ({"trend": Decimal("0.5")}, Decimal("-0.1")),
        ({"trend": Decimal("0.5")}, Decimal("1.1")),
    ),
)
def test_non_finite_or_invalid_values_fail_closed(
    terms: dict[str, Decimal],
    threshold: Decimal,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        attribute(terms, threshold)
