"""Invariant coverage for the deterministic forecast probability mapping."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import Weights, probability

pytestmark = pytest.mark.property


def _weights() -> Weights:
    return Weights(
        distance=Decimal("0.4"),
        velocity=Decimal("0.35"),
        pressure=Decimal("0.25"),
        max_probability=Decimal("0.98"),
    )


def test_within_bounds() -> None:
    weights = _weights()
    for distance in (Decimal("0"), Decimal("0.1"), Decimal("1"), Decimal("10")):
        for velocity in (Decimal("0"), Decimal("0.1"), Decimal("1"), Decimal("10")):
            for pressure in (Decimal("0"), Decimal("0.1"), Decimal("1"), Decimal("10")):
                result = probability(distance, velocity, pressure, 90, weights)
                assert Decimal("0") <= result.probability <= weights.max_probability


def test_monotonic_in_each_input() -> None:
    weights = _weights()

    farther = probability(Decimal("5"), Decimal("1"), Decimal("1"), 30, weights)
    closer = probability(Decimal("1"), Decimal("1"), Decimal("1"), 30, weights)
    assert closer.probability >= farther.probability

    lower_velocity = probability(Decimal("2"), Decimal("0.1"), Decimal("1"), 30, weights)
    higher_velocity = probability(Decimal("2"), Decimal("5"), Decimal("1"), 30, weights)
    assert higher_velocity.probability >= lower_velocity.probability

    lower_pressure = probability(Decimal("2"), Decimal("1"), Decimal("0.1"), 30, weights)
    higher_pressure = probability(Decimal("2"), Decimal("1"), Decimal("5"), 30, weights)
    assert higher_pressure.probability >= lower_pressure.probability


def test_longer_horizon_never_lower() -> None:
    weights = _weights()
    previous = probability(Decimal("2"), Decimal("1"), Decimal("1"), 0, weights).probability

    for horizon in (1, 7, 30, 60, 90, 365):
        current = probability(
            Decimal("2"), Decimal("1"), Decimal("1"), horizon, weights
        ).probability
        assert current >= previous
        previous = current
