"""Unit coverage for deterministic trend and pressure projections (T-052)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import (
    Direction,
    Observation,
    evidence_pressure,
    fit_trend,
    project,
    project_with_evidence,
)

pytestmark = pytest.mark.unit

_START = date(2026, 1, 1)


def _series(*values: str, computable: tuple[bool, ...] | None = None) -> tuple[Observation, ...]:
    flags = computable or (True,) * len(values)
    return tuple(
        Observation(
            observed_on=_START + timedelta(days=index),
            value=Decimal(value) if flag else None,
            computable=flag,
            source_id=f"observation-{index}",
        )
        for index, (value, flag) in enumerate(zip(values, flags, strict=True))
    )


def test_slope_hand_worked() -> None:
    result = fit_trend(_series("10", "12", "16", "19"))

    assert result.slope == Decimal("3.1")
    assert result.per_day_drift == Decimal("3.1")
    assert result.intercept == Decimal("9.6")
    assert result.current_value == Decimal("19")


def test_fewer_than_two_observations_flat_with_reason() -> None:
    result = project(
        _series("19"),
        pressure=Decimal("2"),
        horizon_days=5,
        threshold=Decimal("25"),
        direction=Direction.MAX,
    )

    assert result.slope == Decimal("0")
    assert result.reason == "fewer than two usable observations"
    assert result.pressure == Decimal("2")
    assert result.pressure_term == Decimal("0")
    assert result.values == (Decimal("19"),) * 6


def test_not_computable_observation_excluded_and_recorded() -> None:
    series = (
        Observation(observed_on=_START, value=Decimal("10"), source_id="usable-1"),
        Observation(
            observed_on=_START + timedelta(days=1),
            value=None,
            computable=False,
            reason="ratio denominator is zero",
            source_id="uncomputable",
        ),
        Observation(
            observed_on=_START + timedelta(days=2),
            value=Decimal("14"),
            source_id="usable-2",
        ),
    )

    result = fit_trend(series)

    assert result.slope == Decimal("4")
    assert result.per_day_drift == Decimal("2")
    assert len(result.usable_observations) == 2
    assert len(result.excluded_observations) == 1
    assert result.excluded_observations[0].source_id == "uncomputable"
    assert result.excluded_observations[0].exclusion_reason == "ratio denominator is zero"


def test_no_sustained_evidence_is_trend_only() -> None:
    result = project_with_evidence(
        _series("10", "11", "12"),
        evidence=(
            {
                "id": "transient",
                "state": "transient",
                "materiality_pct": "100",
                "decay_factor": "1",
            },
            {
                "id": "disputed",
                "state": "disputed",
                "materiality_pct": "100",
                "decay_factor": "1",
            },
        ),
        horizon_days=3,
        threshold=Decimal("20"),
        direction=Direction.MAX,
    )

    assert result.pressure == Decimal("0")
    assert result.pressure_term == Decimal("0")
    assert result.net_per_day_drift == Decimal("1")
    assert result.values == (Decimal("12"), Decimal("13"), Decimal("14"), Decimal("15"))
    assert all(term.included is False for term in result.pressure_terms)


def test_pressure_can_oppose_trend_and_both_visible() -> None:
    result = project_with_evidence(
        _series("10", "11", "12"),
        evidence=(
            {
                "id": "evidence-a",
                "state": "sustained",
                "materiality_pct": "100",
                "decay_factor": "1",
            },
            {
                "id": "evidence-b",
                "state": "sustained",
                "materiality_pct": "100",
                "decay_factor": "1",
            },
        ),
        horizon_days=2,
        threshold=Decimal("5"),
        direction=Direction.MIN,
    )

    assert result.per_day_drift == Decimal("1")
    assert result.pressure == Decimal("2")
    assert result.pressure_term == Decimal("-2")
    assert result.net_per_day_drift == Decimal("-1")
    assert result.values == (Decimal("12"), Decimal("11"), Decimal("10"))
    assert [term.signed_contribution for term in result.pressure_terms] == [
        Decimal("-1"),
        Decimal("-1"),
    ]
    assert result.formula_inputs["per_day_drift"] == Decimal("1")
    assert result.formula_inputs["pressure_term"] == Decimal("-2")


def test_path_length_is_horizon_plus_one() -> None:
    result = project(
        _series("10", "11"),
        pressure=Decimal("0"),
        horizon_days=7,
        threshold=Decimal("20"),
        direction="max",
    )

    assert len(result.path) == 8
    assert tuple(point.day for point in result.path) == tuple(range(8))
    assert result.path[0].value == Decimal("11")


def test_no_current_value_keeps_path_shape_without_inventing_values() -> None:
    result = project(
        (
            Observation(
                observed_on=_START,
                value=None,
                computable=False,
                reason="missing denominator",
            ),
        ),
        pressure=Decimal("1"),
        horizon_days=4,
        threshold=Decimal("20"),
        direction=Direction.MAX,
    )

    assert len(result.path) == 5
    assert result.values == (None,) * 5
    assert result.reason == "no usable complete observations"


def test_pressure_terms_are_explainable_and_directional() -> None:
    result = evidence_pressure(
        (
            {
                "id": "included",
                "state": "sustained",
                "materiality_pct": "10",
                "decay_factor": "0.5",
            },
            {"id": "missing-decay", "state": "sustained", "materiality_pct": "10"},
        ),
        Direction.MAX,
    )

    assert result.magnitude == Decimal("0.05")
    assert result.signed == Decimal("0.05")
    assert result.terms[0].contribution == Decimal("0.05")
    assert result.terms[1].included is False
    assert result.terms[1].reason == "decay factor is unavailable"
