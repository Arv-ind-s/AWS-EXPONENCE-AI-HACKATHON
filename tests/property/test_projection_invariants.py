"""Invariant coverage for deterministic forecast paths (T-052)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import cast

import pytest

from covenant_radar.domain.forecast import Direction, Observation, project

pytestmark = pytest.mark.property


def _observations() -> tuple[Observation, ...]:
    start = date(2026, 2, 1)
    return tuple(
        Observation(
            observed_on=start + timedelta(days=index),
            value=Decimal(100 + (index * 3)),
            source_id=f"constant-drift-{index}",
        )
        for index in range(5)
    )


def test_constant_drift_monotonic_path() -> None:
    result = project(
        _observations(),
        pressure=Decimal("0"),
        horizon_days=30,
        threshold=Decimal("200"),
        direction=Direction.MAX,
    )

    values = result.values
    assert len(values) == 31
    assert all(value is not None for value in values)
    numeric_values = tuple(cast(Decimal, value) for value in values)
    assert all(
        left <= right for left, right in zip(numeric_values, numeric_values[1:], strict=False)
    )
    assert all(
        point.trend_component == result.per_day_drift * Decimal(point.day) for point in result.path
    )
    assert all(point.pressure_component == Decimal("0") for point in result.path)


def test_day_zero_equals_current_value() -> None:
    result = project(
        _observations(),
        pressure=Decimal("0.25"),
        horizon_days=12,
        threshold=Decimal("130"),
        direction=Direction.MAX,
    )

    assert result.path[0].day == 0
    assert result.path[0].value == result.current_value
    assert result.path[0].trend_component == Decimal("0")
    assert result.path[0].pressure_component == Decimal("0")
