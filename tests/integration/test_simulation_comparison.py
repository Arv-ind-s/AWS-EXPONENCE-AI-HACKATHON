"""Integration coverage for T-063 multi-option comparison."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import Observation, Weights, project
from covenant_radar.domain.interventions import (
    InterventionFacts,
    LevelShiftEffect,
    PressureReductionEffect,
    RateChangeEffect,
    ThresholdRelaxationEffect,
)
from covenant_radar.domain.interventions.simulate import MAX_COMPARISON_OPTIONS
from covenant_radar.services.simulation import SimulationService

pytestmark = pytest.mark.integration

_START = date(2026, 1, 1)
_WEIGHTS = Weights(Decimal("1"), Decimal("1"), Decimal("1"))
_CLASSES = frozenset({"leverage"})


def _projection():
    return project(
        (
            Observation(observed_on=_START, value=Decimal("80"), source_id="one"),
            Observation(
                observed_on=_START + timedelta(days=1),
                value=Decimal("85"),
                source_id="two",
            ),
        ),
        pressure=Decimal("0.20"),
        horizon_days=30,
        threshold=Decimal("100"),
        direction="max",
    )


def _options() -> tuple[InterventionFacts, ...]:
    return (
        InterventionFacts(
            code="LEVEL",
            effect=LevelShiftEffect(
                amount=Decimal("-2"),
                assumptions=("the balance is reduced immediately",),
                applicable_covenant_classes=_CLASSES,
            ),
        ),
        InterventionFacts(
            code="RATE",
            effect=RateChangeEffect(
                multiplier=Decimal("0.5"),
                assumptions=("the operating improvement persists",),
                applicable_covenant_classes=_CLASSES,
            ),
        ),
        InterventionFacts(
            code="PRESSURE",
            effect=PressureReductionEffect(
                fraction=Decimal("0.5"),
                assumptions=("the observed pressure is reduced",),
                applicable_covenant_classes=_CLASSES,
            ),
        ),
        InterventionFacts(
            code="THRESHOLD",
            effect=ThresholdRelaxationEffect(
                amount=Decimal("2"),
                assumptions=("the amendment is approved",),
                applicable_covenant_classes=_CLASSES,
            ),
        ),
    )


def _parameters() -> dict[str, object]:
    return {"covenant_class": "leverage", "weights": _WEIGHTS}


def test_four_options_plus_baseline() -> None:
    comparison = SimulationService().compare(_projection(), _options(), _parameters())

    assert comparison.option_count == MAX_COMPARISON_OPTIONS
    assert len(comparison.results) == MAX_COMPARISON_OPTIONS + 1
    assert comparison.results[0] is comparison.baseline
    assert [option.intervention_code for option in comparison.options] == [
        "LEVEL",
        "RATE",
        "PRESSURE",
        "THRESHOLD",
    ]
    assert all(option.assumptions for option in comparison.options)


def test_more_than_four_refused() -> None:
    options = _options() + (
        InterventionFacts(
            code="FIFTH",
            effect=LevelShiftEffect(
                amount=Decimal("-1"),
                assumptions=("the fifth action is approved",),
                applicable_covenant_classes=_CLASSES,
            ),
        ),
    )

    with pytest.raises(ValueError, match="At most four"):
        SimulationService().compare(_projection(), options, _parameters())


def test_uses_same_functions_as_baseline() -> None:
    projection = _projection()
    option = _options()[0]
    comparison = SimulationService().compare(projection, (option,), _parameters())
    result = comparison.options[0]

    assert result.baseline.path == comparison.baseline.path
    assert result.projection.path[0].value == comparison.baseline.path[0].value - Decimal("2")
    assert result.delta_probability == result.probability - comparison.baseline.probability
    assert result.delta_days == (result.crossing_day - comparison.baseline.crossing.crossing_day)
