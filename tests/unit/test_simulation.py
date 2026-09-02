"""Unit coverage for deterministic counterfactual simulation (T-063)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import Direction, Observation, Weights, project
from covenant_radar.domain.interventions import (
    InterventionFacts,
    InterventionNotApplicable,
    LevelShiftEffect,
    PressureReductionEffect,
    RateChangeEffect,
)
from covenant_radar.domain.interventions.simulate import simulate

pytestmark = pytest.mark.unit

_START = date(2026, 1, 1)
_WEIGHTS = Weights(Decimal("1"), Decimal("1"), Decimal("1"))
_ASSUMPTION = ("the approved operating action takes effect immediately",)


def _projection(
    values: tuple[str, ...] = ("80", "85"),
    *,
    threshold: str = "100",
    horizon: int = 10,
    direction: Direction = Direction.MAX,
):
    observations = tuple(
        Observation(
            observed_on=_START + timedelta(days=index),
            value=Decimal(value),
            source_id=f"observation-{index}",
        )
        for index, value in enumerate(values)
    )
    return project(
        observations,
        pressure=Decimal("0.20"),
        horizon_days=horizon,
        threshold=Decimal(threshold),
        direction=direction,
    )


def _intervention(effect, code: str = "ACTION") -> InterventionFacts:
    return InterventionFacts(code=code, effect=effect, text="Approved action")


def test_delta_reconciles_to_recomputation() -> None:
    projection = _projection()
    intervention = _intervention(
        RateChangeEffect(
            multiplier=Decimal("0.5"),
            assumptions=_ASSUMPTION,
            applicable_covenant_classes=frozenset({"leverage"}),
        )
    )

    result = simulate(
        projection,
        intervention,
        {"covenant_class": "leverage", "weights": _WEIGHTS},
    )

    assert result.delta_probability == result.probability - result.baseline_probability
    assert result.delta_days == result.crossing_day - result.baseline_crossing.crossing_day
    assert result.projection.values[-1] == Decimal("112.00")
    assert result.delta_days_qualifier == "exact"


def test_beyond_horizon_reported_as_at_least() -> None:
    projection = _projection(values=("90", "95"), horizon=3)
    intervention = _intervention(
        RateChangeEffect(
            multiplier=Decimal("0"),
            assumptions=_ASSUMPTION,
            applicable_covenant_classes=frozenset({"leverage"}),
        )
    )

    result = simulate(
        projection,
        intervention,
        {"covenant_class": "leverage", "weights": _WEIGHTS},
    )

    assert result.baseline_crossing.crossing_day == 1
    assert result.crossing_date is None
    assert result.delta_days == 3
    assert result.delta_days_at_least is True
    assert result.delta_days_qualifier == "at_least"


def test_zero_effect_distinct_from_inapplicable() -> None:
    projection = _projection()
    zero_effect = _intervention(
        PressureReductionEffect(
            fraction=Decimal("0"),
            assumptions=_ASSUMPTION,
            applicable_covenant_classes=frozenset({"leverage"}),
        ),
        code="ZERO",
    )
    result = simulate(
        projection,
        zero_effect,
        {"covenant_class": "leverage", "weights": _WEIGHTS},
    )

    assert result.no_effect is True
    assert result.effect_status == "no_effect"
    assert result.delta_probability == Decimal("0")

    with pytest.raises(InterventionNotApplicable, match="coverage.*leverage"):
        simulate(
            projection,
            zero_effect,
            {"covenant_class": "coverage", "weights": _WEIGHTS},
        )


def test_breached_covenant_simulates_cure_path_with_assumption() -> None:
    projection = _projection(values=("110", "115"), threshold="100", horizon=5)
    intervention = _intervention(
        LevelShiftEffect(
            amount=Decimal("-20"),
            assumptions=_ASSUMPTION,
            applicable_covenant_classes=frozenset({"leverage"}),
        )
    )

    result = simulate(
        projection,
        intervention,
        {"covenant_class": "leverage", "weights": _WEIGHTS},
    )

    assert result.already_breached is True
    assert result.cure_path is True
    assert any("cure path" in assumption for assumption in result.assumptions)
    assert result.baseline_crossing.crossing_day == 0


def test_identical_reruns() -> None:
    projection = _projection()
    intervention = _intervention(
        LevelShiftEffect(
            amount=Decimal("-2"),
            assumptions=_ASSUMPTION,
            applicable_covenant_classes=frozenset({"leverage"}),
        )
    )
    parameters = {"weights": _WEIGHTS, "covenant_class": "leverage"}

    first = simulate(projection, intervention, parameters)
    second = simulate(projection, intervention, dict(reversed(tuple(parameters.items()))))

    assert first.content_hash == second.content_hash
    assert first.to_mapping() == second.to_mapping()
