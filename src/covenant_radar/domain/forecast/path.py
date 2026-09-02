"""Daily deterministic forecast paths (contract ``C-35``).

The path is intentionally a small arithmetic projection: the latest usable
observation is day zero, the fitted per-day trend is one term, and the
directional pressure from sustained evidence is the other.  Every intermediate
term is returned so callers can persist an explainable trace and later stages
can find crossings without re-fitting or calling a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from covenant_radar.domain.forecast.trend import (
    Direction,
    Observation,
    PressureResult,
    PressureTerm,
    TrendResult,
    evidence_pressure,
    fit_trend,
)

_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class PathPoint:
    """One day in the stored forecast path."""

    day: int
    value: Decimal | None
    trend_component: Decimal
    pressure_component: Decimal

    @property
    def day_offset(self) -> int:
        return self.day

    @property
    def projected_value(self) -> Decimal | None:
        return self.value

    @property
    def trend(self) -> Decimal:
        return self.trend_component

    @property
    def pressure(self) -> Decimal:
        return self.pressure_component


@dataclass(frozen=True, slots=True)
class Projection:
    """Complete, explainable output of :func:`project`."""

    current_value: Decimal | None
    threshold: Decimal
    direction: Direction
    horizon_days: int
    slope: Decimal
    per_day_drift: Decimal
    pressure: Decimal
    pressure_term: Decimal
    net_per_day_drift: Decimal
    path: tuple[PathPoint, ...]
    trend: TrendResult
    pressure_result: PressureResult | None = None
    requested_pressure: Decimal = _ZERO
    reason: str | None = None
    formula_inputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.horizon_days < 0:
            raise ValueError("horizon_days must be non-negative.")
        if len(self.path) != self.horizon_days + 1:
            raise ValueError("A projection path must contain horizon_days + 1 points.")
        if self.path and self.path[0].day != 0:
            raise ValueError("A projection path must start at day zero.")
        if not isinstance(self.formula_inputs, Mapping):
            raise TypeError("formula_inputs must be a mapping.")
        object.__setattr__(self, "formula_inputs", MappingProxyType(dict(self.formula_inputs)))

    @property
    def daily_path(self) -> tuple[PathPoint, ...]:
        return self.path

    @property
    def points(self) -> tuple[PathPoint, ...]:
        return self.path

    @property
    def values(self) -> tuple[Decimal | None, ...]:
        return tuple(point.value for point in self.path)

    @property
    def slope_per_period(self) -> Decimal:
        return self.slope

    @property
    def signed_pressure(self) -> Decimal:
        return self.pressure_term

    @property
    def total_drift(self) -> Decimal:
        return self.net_per_day_drift

    @property
    def usable_observations(self) -> tuple[Observation, ...]:
        return self.trend.usable_observations

    @property
    def excluded_observations(self) -> tuple[Observation, ...]:
        return self.trend.excluded_observations

    @property
    def pressure_terms(self) -> tuple[PressureTerm, ...]:
        return self.pressure_result.terms if self.pressure_result is not None else ()


def project(
    series: Sequence[Observation | Mapping[str, object] | object],
    pressure: Decimal | PressureResult,
    horizon_days: int,
    threshold: Decimal,
    direction: Direction | str,
    *,
    recent_periods: int | None = None,
    period_days: int | Decimal | None = None,
) -> Projection:
    """Project a dated series through ``horizon_days`` inclusive.

    ``pressure`` is a non-negative magnitude.  For a ``max`` covenant,
    deterioration increases the projected value; for a ``min`` covenant it
    decreases the projected value.  A :class:`PressureResult` may be supplied
    to preserve per-evidence terms; a scalar remains supported by the C-35
    contract.

    If fewer than two usable observations exist, the trend and path are flat.
    The requested pressure is retained in ``formula_inputs`` but is not
    applied without a usable trend baseline, preventing an invented trajectory
    from a single or unavailable observation.
    """

    normalized_direction = Direction.from_value(direction)
    validated_horizon = _horizon(horizon_days)
    validated_threshold = _decimal(threshold, "threshold")
    trend = fit_trend(
        series,
        recent_periods=recent_periods,
        period_days=period_days,
    )
    pressure_result, requested_pressure, signed_pressure = _pressure_values(
        pressure,
        normalized_direction,
    )
    sufficient_trend = trend.has_sufficient_observations
    effective_pressure_term = signed_pressure if sufficient_trend else _ZERO
    net_drift = trend.per_day_drift + effective_pressure_term
    points: tuple[PathPoint, ...]
    points = tuple(
        PathPoint(
            day=day,
            value=(
                None
                if trend.current_value is None
                else trend.current_value + net_drift * Decimal(day)
            ),
            trend_component=trend.per_day_drift * Decimal(day),
            pressure_component=effective_pressure_term * Decimal(day),
        )
        for day in range(validated_horizon + 1)
    )

    reason = trend.reason
    if reason is None and not sufficient_trend and requested_pressure != _ZERO:
        reason = "pressure not applied because fewer than two usable observations exist"
    formula_inputs = {
        "current_value": trend.current_value,
        "threshold": validated_threshold,
        "direction": normalized_direction.value,
        "horizon_days": validated_horizon,
        "slope_per_period": trend.slope,
        "period_length_days": trend.period_length_days,
        "per_day_drift": trend.per_day_drift,
        "requested_pressure": requested_pressure,
        "pressure_term": effective_pressure_term,
        "net_per_day_drift": net_drift,
        "usable_observation_count": len(trend.usable_observations),
        "excluded_observation_count": len(trend.excluded_observations),
        "pressure_terms": [
            {
                "evidence_id": term.evidence_id,
                "materiality": term.materiality,
                "decay_factor": term.decay_factor,
                "contribution": term.contribution,
                "signed_contribution": term.signed_contribution,
                "included": term.included,
                "reason": term.reason,
            }
            for term in (pressure_result.terms if pressure_result is not None else ())
        ],
    }
    return Projection(
        current_value=trend.current_value,
        threshold=validated_threshold,
        direction=normalized_direction,
        horizon_days=validated_horizon,
        slope=trend.slope,
        per_day_drift=trend.per_day_drift,
        pressure=requested_pressure,
        pressure_term=effective_pressure_term,
        net_per_day_drift=net_drift,
        path=points,
        trend=trend,
        pressure_result=pressure_result,
        requested_pressure=requested_pressure,
        reason=reason,
        formula_inputs=formula_inputs,
    )


def project_with_evidence(
    series: Sequence[Observation | Mapping[str, object] | object],
    evidence: Iterable[Mapping[str, object] | object],
    horizon_days: int,
    threshold: Decimal,
    direction: Direction | str,
    *,
    recent_periods: int | None = None,
    period_days: int | Decimal | None = None,
) -> Projection:
    """Compute pressure from evidence and project in one explicit operation."""

    normalized_direction = Direction.from_value(direction)
    pressure = evidence_pressure(evidence, normalized_direction)
    return project(
        series,
        pressure,
        horizon_days,
        threshold,
        normalized_direction,
        recent_periods=recent_periods,
        period_days=period_days,
    )


def _pressure_values(
    pressure: Decimal | PressureResult,
    direction: Direction,
) -> tuple[PressureResult | None, Decimal, Decimal]:
    if isinstance(pressure, PressureResult):
        if pressure.direction is direction:
            magnitude = pressure.magnitude
        else:
            magnitude = abs(pressure.signed)
        return (
            pressure,
            _non_negative(magnitude, "pressure"),
            _signed_pressure(magnitude, direction),
        )
    magnitude = _non_negative(_decimal(pressure, "pressure"), "pressure")
    return None, magnitude, _signed_pressure(magnitude, direction)


def _signed_pressure(magnitude: Decimal, direction: Direction) -> Decimal:
    return magnitude if direction is Direction.MAX else -magnitude


def _horizon(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("horizon_days must be a non-negative integer.")
    return value


def _non_negative(value: Decimal, field_name: str) -> Decimal:
    if value < _ZERO:
        raise ValueError(f"{field_name} must be non-negative.")
    return value


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field_name} must be a finite Decimal.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite Decimal.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal.")
    return result


# The C-35 name is intentionally the primary public entry point; these aliases
# keep callers using noun-first terminology on the same implementation.
projection = project
build_path = project
project_path = project


__all__ = [
    "Direction",
    "Observation",
    "PathPoint",
    "PressureResult",
    "PressureTerm",
    "Projection",
    "build_path",
    "project",
    "project_path",
    "project_with_evidence",
    "projection",
]
