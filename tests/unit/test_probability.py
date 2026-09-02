"""Unit coverage for the transparent forecast probability mapping (T-054)."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest

import covenant_radar.domain.forecast.probability as probability_module
from covenant_radar.domain.forecast import ProbabilityResult, Weights, probability

pytestmark = pytest.mark.unit


def _weights() -> Weights:
    return Weights(
        distance=Decimal("0.5"),
        velocity=Decimal("0.3"),
        pressure=Decimal("0.2"),
    )


def test_hand_worked_mapping() -> None:
    result = probability(
        distance=Decimal("1"),
        velocity=Decimal("1"),
        pressure=Decimal("1"),
        horizon_days=1,
        weights=_weights(),
    )

    assert isinstance(result, ProbabilityResult)
    assert result.normalized_distance == Decimal("0.5")
    assert result.normalized_velocity == Decimal("0.5")
    assert result.normalized_pressure == Decimal("0.5")
    assert result.horizon_factor == Decimal("0.5")
    assert result.terms_by_name["distance"].contribution == Decimal("0.25")
    assert result.terms_by_name["velocity"].contribution == Decimal("0.075")
    assert result.terms_by_name["pressure"].contribution == Decimal("0.05")
    assert result.raw_score == Decimal("0.375")
    assert result.probability == Decimal("0.375")
    assert result.clamped is False
    assert result.formula_inputs["mapping_version"] == "forecast.probability.v1"
    assert result.formula_inputs["terms"]["pressure"]["contribution"] == Decimal("0.05")


def test_weights_are_read_from_configuration_and_normalized() -> None:
    weights = Weights.from_mapping(
        {
            "probability": {
                "weights": {
                    "distance": Decimal("2"),
                    "velocity": Decimal("3"),
                    "pressure": Decimal("5"),
                },
                "max_probability": Decimal("0.97"),
            }
        }
    )

    assert weights.distance == Decimal("0.2")
    assert weights.velocity == Decimal("0.3")
    assert weights.pressure == Decimal("0.5")
    assert weights.max_probability == Decimal("0.97")


def test_clamped_below_one_and_recorded() -> None:
    result = probability(
        distance=Decimal("0.000001"),
        velocity=Decimal("1000000"),
        pressure=Decimal("1000000"),
        horizon_days=365,
        weights=_weights(),
    )

    assert result.raw_score > result.max_probability
    assert result.probability == Decimal("0.99")
    assert result.probability < Decimal("1")
    assert result.clamped is True
    assert result.clamp_reason is not None
    assert "clamped" in result.clamp_reason
    assert result.formula_inputs["clamped"] is True


def test_already_breached_at_clamp_with_reason() -> None:
    for horizon in (0, 30, 60, 90):
        result = probability(
            distance=Decimal("-0.01"),
            velocity=Decimal("0"),
            pressure=Decimal("0"),
            horizon_days=horizon,
            weights=_weights(),
        )

        assert result.probability == Decimal("0.99")
        assert result.clamped is True
        assert result.already_breached is True
        assert result.reason is not None
        assert "already in breach" in result.reason
        assert result.clamp_reason is not None


def test_neutral_inputs_documented_value() -> None:
    result = probability(
        distance=Decimal("0"),
        velocity=Decimal("0"),
        pressure=Decimal("0"),
        horizon_days=90,
        weights=_weights(),
    )

    assert result.probability == Decimal("0")
    assert result.raw_score == Decimal("0")
    assert result.normalized_distance == Decimal("0")
    assert result.clamped is False
    assert result.already_breached is False
    assert result.reason is not None
    assert "neutral" in result.reason


def test_no_weight_literal_in_module() -> None:
    source = inspect.getsource(probability_module)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if any("weight" in name.lower() for name in names):
            value = node.value
            assert not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "Decimal"
            ), f"weight policy must not be a Decimal literal: {names}"
