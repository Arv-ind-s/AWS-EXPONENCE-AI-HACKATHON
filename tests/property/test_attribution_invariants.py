"""Invariant coverage for signed forecast driver attribution (T-057)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.forecast.attribution import attribute

pytestmark = pytest.mark.property


_TERM_CASES = (
    {"trend": Decimal("0"), "evidence": Decimal("0")},
    {"trend": Decimal("1")},
    {"trend": Decimal("0.8"), "quality": Decimal("-0.2")},
    {
        "trend": Decimal("0.2"),
        "evidence": Decimal("0.3"),
        "quality": Decimal("0.5"),
    },
    {f"factor_{index}": Decimal("0.05") for index in range(20)},
    {"positive": Decimal("0.1"), "negative": Decimal("-0.1")},
)


@pytest.mark.parametrize("terms", _TERM_CASES)
@pytest.mark.parametrize(
    "threshold",
    (Decimal("0.01"), Decimal("0.10"), Decimal("0.50"), Decimal("1")),
)
def test_shares_sum_to_one(terms: dict[str, Decimal], threshold: Decimal) -> None:
    result = attribute(terms, threshold)
    total = sum((row.share for row in result), Decimal("0"))

    assert abs(float(total - Decimal("1"))) <= 1e-12


@pytest.mark.parametrize("terms", _TERM_CASES)
@pytest.mark.parametrize(
    "threshold",
    (Decimal("0.01"), Decimal("0.10"), Decimal("0.50"), Decimal("1")),
)
def test_listed_shares_never_below_t5(
    terms: dict[str, Decimal],
    threshold: Decimal,
) -> None:
    result = attribute(terms, threshold)

    for row in result:
        if row.name not in {"other", "neutral"} and row.share >= Decimal("0"):
            assert row.share >= threshold
        if row.name not in {"other", "neutral"}:
            original = terms[row.name]
            if original < Decimal("0"):
                assert row.share < Decimal("0")
