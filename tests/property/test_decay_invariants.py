"""Property coverage for geometric evidence decay."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.signals.decay import decay_factor

pytestmark = pytest.mark.property


def test_factor_within_zero_and_one() -> None:
    for rate in (Decimal("0"), Decimal("0.01"), Decimal("0.50"), Decimal("0.99"), Decimal("1")):
        for days in range(0, 365):
            factor = decay_factor(days, rate)
            assert Decimal("0") <= factor <= Decimal("1")


def test_factor_monotonic_in_days() -> None:
    for rate in (Decimal("0"), Decimal("0.25"), Decimal("0.75"), Decimal("0.99"), Decimal("1")):
        previous = decay_factor(0, rate)
        for days in range(1, 365):
            current = decay_factor(days, rate)
            assert current <= previous
            previous = current
