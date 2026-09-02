"""Unit coverage for T-062 intervention effects and applicability."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import Direction
from covenant_radar.domain.interventions import (
    CombinationEffect,
    EffectModelType,
    InterventionNotApplicable,
    LevelShiftEffect,
    PressureReductionEffect,
    ProjectionInputs,
    RateChangeEffect,
    ThresholdRelaxationEffect,
    build_effect,
    is_applicable,
)

pytestmark = pytest.mark.unit

_ASSUMPTIONS = ("the approved action takes effect immediately",)
_CLASSES = frozenset({"leverage"})


def _inputs(*, direction: Direction = Direction.MAX) -> ProjectionInputs:
    return ProjectionInputs(
        current_value=Decimal("10"),
        threshold=Decimal("20"),
        direction=direction,
        per_day_drift=Decimal("2"),
        pressure=Decimal("4"),
    )


def test_each_effect_transforms_as_documented() -> None:
    level = LevelShiftEffect(
        amount=Decimal("3"),
        assumptions=_ASSUMPTIONS,
        applicable_covenant_classes=_CLASSES,
    )
    rate = RateChangeEffect(
        multiplier=Decimal("0.5"),
        assumptions=_ASSUMPTIONS,
        applicable_covenant_classes=_CLASSES,
    )
    threshold = ThresholdRelaxationEffect(
        amount=Decimal("5"),
        assumptions=_ASSUMPTIONS,
        applicable_covenant_classes=_CLASSES,
    )
    pressure = PressureReductionEffect(
        fraction=Decimal("0.75"),
        assumptions=_ASSUMPTIONS,
        applicable_covenant_classes=_CLASSES,
    )

    assert level.transform(_inputs()).current_value == Decimal("13")
    assert rate.transform(_inputs()).per_day_drift == Decimal("1")
    assert threshold.transform(_inputs()).threshold == Decimal("25")
    assert pressure.transform(_inputs()).pressure == Decimal("1")
    assert threshold.transform(_inputs(direction=Direction.MIN)).threshold == Decimal("15")

    assert level.model_type is EffectModelType.LEVEL_SHIFT
    assert level.assumptions == _ASSUMPTIONS
    assert level.applicable_covenant_classes == _CLASSES


def test_inapplicable_class_refused_naming_why() -> None:
    effect = LevelShiftEffect(
        amount=Decimal("3"),
        assumptions=_ASSUMPTIONS,
        applicable_covenant_classes=_CLASSES,
    )

    with pytest.raises(InterventionNotApplicable, match="coverage.*leverage"):
        effect.apply(_inputs(), "coverage")

    assert is_applicable(effect, "leverage_ratio")
    assert not is_applicable(effect, "coverage")


def test_parameter_out_of_range_refused() -> None:
    with pytest.raises(ValueError, match="multiplier.*between 0 and 2"):
        RateChangeEffect(
            multiplier=Decimal("2.01"),
            assumptions=_ASSUMPTIONS,
            applicable_covenant_classes=_CLASSES,
        )

    with pytest.raises(ValueError, match="fraction.*between 0 and 1"):
        PressureReductionEffect(
            fraction=Decimal("-0.01"),
            assumptions=_ASSUMPTIONS,
            applicable_covenant_classes=_CLASSES,
        )


def test_empty_assumptions_refused() -> None:
    with pytest.raises(ValueError, match="assumptions"):
        LevelShiftEffect(
            amount=Decimal("1"),
            assumptions=(),
            applicable_covenant_classes=_CLASSES,
        )


def test_combination_order_documented_and_applied() -> None:
    combination = CombinationEffect(
        components=(
            ThresholdRelaxationEffect(
                amount=Decimal("5"),
                assumptions=("the lender approves a threshold amendment",),
                applicable_covenant_classes=_CLASSES,
            ),
            PressureReductionEffect(
                fraction=Decimal("0.75"),
                assumptions=("the control reduces observed pressure",),
                applicable_covenant_classes=_CLASSES,
            ),
            RateChangeEffect(
                multiplier=Decimal("0.5"),
                assumptions=("the operating improvement changes the drift",),
                applicable_covenant_classes=_CLASSES,
            ),
            LevelShiftEffect(
                amount=Decimal("3"),
                assumptions=("the action changes the current level",),
                applicable_covenant_classes=_CLASSES,
            ),
        ),
        assumptions=("the four actions are approved together",),
    )

    transformed = combination.apply(_inputs(), "leverage")

    assert transformed == ProjectionInputs(
        current_value=Decimal("13"),
        threshold=Decimal("25"),
        direction=Direction.MAX,
        per_day_drift=Decimal("1"),
        pressure=Decimal("1"),
    )
    assert combination.assumptions == (
        "the four actions are approved together",
        "the action changes the current level",
        "the operating improvement changes the drift",
        "the control reduces observed pressure",
        "the lender approves a threshold amendment",
    )
    assert combination.effect_parameters["order"] == (
        "level_shift",
        "rate_change",
        "pressure_reduction",
        "threshold_relaxation",
    )


def test_catalogue_factory_rejects_unknown_parameter_and_builds_effect() -> None:
    effect = build_effect(
        "pressure_reduction",
        {"fraction": "1"},
        assumptions=_ASSUMPTIONS,
        applicable_covenant_classes=_CLASSES,
    )

    assert isinstance(effect, PressureReductionEffect)
    assert effect.transform(_inputs()).pressure == Decimal("0")

    with pytest.raises(ValueError, match="unknown parameter"):
        build_effect(
            "pressure_reduction",
            {"fraction": "0.5", "typo": "ignored"},
            assumptions=_ASSUMPTIONS,
            applicable_covenant_classes=_CLASSES,
        )
