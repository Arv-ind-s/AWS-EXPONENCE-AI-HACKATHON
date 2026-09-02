"""Unit coverage for deterministic forecast threshold crossing (T-053)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from covenant_radar.domain.covenants.headroom import signed_headroom
from covenant_radar.domain.forecast import (
    Direction,
    ThresholdChange,
    first_crossing,
)

pytestmark = pytest.mark.unit

_AS_OF = date(2026, 8, 31)


def test_already_breached_crosses_today() -> None:
    result = first_crossing(
        (Decimal("90"), Decimal("91")),
        threshold=Decimal("85"),
        direction=Direction.MAX,
        as_of_date=_AS_OF,
    )

    assert result.crossed is True
    assert result.crossing_day == 0
    assert result.crossing_date == _AS_OF
    assert result.crossing_value == Decimal("90")
    assert result.threshold_used == Decimal("85")
    assert result.margin == Decimal("5")


def test_improving_returns_none_with_direction() -> None:
    result = first_crossing(
        (Decimal("80"), Decimal("79"), Decimal("78")),
        threshold=Decimal("85"),
        direction=Direction.MAX,
        as_of_date=_AS_OF,
    )

    assert result.crossed is False
    assert result.crossing_day is None
    assert result.crossing_date is None
    assert result.direction is Direction.MAX
    assert result.reason is not None
    assert "moving away" in result.reason


def test_exact_touch_is_a_crossing() -> None:
    result = first_crossing(
        (Decimal("80"), Decimal("85"), Decimal("90")),
        threshold=Decimal("85"),
        direction="max",
        as_of_date=_AS_OF,
    )

    assert result.crossing_day == 1
    assert result.crossing_date == _AS_OF + timedelta(days=1)
    assert result.crossing_value == Decimal("85")
    assert result.margin == Decimal("0")


@pytest.mark.parametrize(
    ("direction", "values"),
    (
        (Direction.MAX, (Decimal("84"), Decimal("85"))),
        (Direction.MIN, (Decimal("86"), Decimal("85"))),
    ),
)
def test_boundary_matches_engine_convention(
    direction: Direction,
    values: tuple[Decimal, Decimal],
) -> None:
    result = first_crossing(
        values,
        threshold=Decimal("85"),
        direction=direction,
        as_of_date=_AS_OF,
    )

    assert result.crossing_day == 1
    assert result.crossing_value == Decimal("85")
    assert result.margin == Decimal("0")
    assert signed_headroom(Decimal("85"), Decimal("85"), direction.value) == Decimal("0")


def test_mid_horizon_threshold_change_applied() -> None:
    result = first_crossing(
        (Decimal("80"), Decimal("84"), Decimal("86"), Decimal("88")),
        threshold=Decimal("85"),
        direction=Direction.MAX,
        as_of_date=_AS_OF,
        threshold_changes=(
            ThresholdChange(
                threshold=Decimal("87"),
                effective_date=_AS_OF + timedelta(days=2),
                reason="approved exception",
            ),
        ),
    )

    assert result.crossing_day == 3
    assert result.crossing_date == _AS_OF + timedelta(days=3)
    assert result.crossing_value == Decimal("88")
    assert result.threshold_used == Decimal("87")
    assert result.margin == Decimal("1")
    assert tuple(point.threshold for point in result.threshold_path) == (
        Decimal("85"),
        Decimal("85"),
        Decimal("87"),
        Decimal("87"),
    )


def test_flat_trajectory_at_threshold_crosses_day_zero() -> None:
    result = first_crossing(
        (Decimal("85"), Decimal("85"), Decimal("85")),
        threshold=Decimal("85"),
        direction=Direction.MAX,
        as_of_date=_AS_OF,
    )

    assert result.crossing_day == 0
    assert result.crossing_date == _AS_OF
