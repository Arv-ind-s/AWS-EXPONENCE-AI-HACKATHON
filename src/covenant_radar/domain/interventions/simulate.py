"""Deterministic counterfactual intervention simulation (T-063).

The simulator is deliberately an orchestration module, not a second forecast
engine.  It reconstructs the stored projection inputs, applies one validated
effect, and sends the counterfactual through the existing forecast path,
crossing, and probability functions.  The supplied projection remains the
baseline path when the stored horizon is used; its crossing and probability
are derived by those same domain functions.  That makes a displayed delta
independently reproducible from the facts retained by a forecast run.

No result is mutable or dependent on an object identity.  This is important
for two reasons: a simulation is persisted by the following task, and a
repeated simulation with the same inputs must have the same content hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date as CalendarDate
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Final, cast

from covenant_radar.domain.forecast import (
    CrossingResult,
    Direction,
    Observation,
    PathPoint,
    PressureResult,
    ProbabilityResult,
    Projection,
    ThresholdChange,
    Weights,
    first_crossing,
    probability,
    project,
)
from covenant_radar.domain.interventions.applicability import (
    normalize_covenant_class,
)
from covenant_radar.domain.interventions.effects import (
    InterventionFacts,
    ProjectionInputs,
)

MAX_COMPARISON_OPTIONS: Final[int] = 4
_ZERO: Final[Decimal] = Decimal("0")
_ACCEPTED_PARAMETER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "as_of",
        "as_of_date",
        "covenant_class",
        "covenant_type",
        "current_date",
        "horizon_days",
        "probability",
        "probability_weights",
        "threshold_changes",
        "threshold_schedule",
        "weights",
    }
)
_AT_LEAST: Final[str] = "at_least"
_AT_MOST: Final[str] = "at_most"
_EXACT: Final[str] = "exact"


@dataclass(frozen=True, slots=True)
class BaselineResult:
    """The do-nothing forecast used as the comparison reference."""

    projection: Projection
    crossing: CrossingResult
    probability_result: ProbabilityResult | None

    @property
    def path(self) -> tuple[PathPoint, ...]:
        """Return the stored/recomputed baseline path."""

        return self.projection.path

    @property
    def crossing_date(self) -> CalendarDate | None:
        return self.crossing.crossing_date

    @property
    def projected_cross_date(self) -> CalendarDate | None:
        return self.crossing_date

    @property
    def crossing_day(self) -> int | None:
        return self.crossing.crossing_day

    @property
    def probability(self) -> Decimal | None:
        return None if self.probability_result is None else self.probability_result.probability


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """One intervention's counterfactual and its reconciliation to baseline."""

    intervention: InterventionFacts
    projection: Projection
    crossing: CrossingResult
    probability_result: ProbabilityResult | None
    baseline: BaselineResult
    transformed_inputs: ProjectionInputs
    assumptions: tuple[str, ...]
    delta_days: int | None
    delta_days_qualifier: str
    delta_probability: Decimal | None
    effect_status: str
    parameters: Mapping[str, object]
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.intervention, InterventionFacts):
            raise TypeError("intervention must be an InterventionFacts instance.")
        if not isinstance(self.projection, Projection):
            raise TypeError("projection must be a Projection instance.")
        if not isinstance(self.crossing, CrossingResult):
            raise TypeError("crossing must be a CrossingResult instance.")
        if self.probability_result is not None and not isinstance(
            self.probability_result, ProbabilityResult
        ):
            raise TypeError("probability_result must be a ProbabilityResult or None.")
        if not self.assumptions or any(
            not isinstance(value, str) or not value.strip() for value in self.assumptions
        ):
            raise ValueError("assumptions must contain at least one non-blank statement.")
        if self.delta_days_qualifier not in {_EXACT, _AT_LEAST, _AT_MOST}:
            raise ValueError("delta_days_qualifier must be exact, at_least, or at_most.")
        if self.delta_days is None and self.delta_days_qualifier != _EXACT:
            raise ValueError("a censored delta must carry a day bound.")
        if self.delta_probability is not None:
            _decimal(self.delta_probability, "delta_probability")
        if self.effect_status not in {"applied", "no_effect"}:
            raise ValueError("effect_status must be applied or no_effect.")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters must be a mapping.")
        if not isinstance(self.content_hash, str) or len(self.content_hash) != 64:
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest.")
        try:
            int(self.content_hash, 16)
        except ValueError as error:
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest.") from error
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        object.__setattr__(self, "parameters", _freeze(self.parameters))

    @property
    def path(self) -> tuple[PathPoint, ...]:
        """Return the counterfactual daily path."""

        return self.projection.path

    @property
    def counterfactual_path(self) -> tuple[PathPoint, ...]:
        return self.path

    @property
    def crossing_date(self) -> CalendarDate | None:
        return self.crossing.crossing_date

    @property
    def projected_cross_date(self) -> CalendarDate | None:
        return self.crossing_date

    @property
    def crossing_day(self) -> int | None:
        return self.crossing.crossing_day

    @property
    def probability(self) -> Decimal | None:
        return None if self.probability_result is None else self.probability_result.probability

    @property
    def probability_value(self) -> Decimal | None:
        return self.probability

    @property
    def intervention_code(self) -> str:
        return self.intervention.code

    @property
    def baseline_projection(self) -> Projection:
        return self.baseline.projection

    @property
    def baseline_crossing(self) -> CrossingResult:
        return self.baseline.crossing

    @property
    def baseline_probability(self) -> Decimal | None:
        return self.baseline.probability

    @property
    def delta_days_at_least(self) -> bool:
        """Whether ``delta_days`` is a lower bound, not an exact date delta."""

        return self.delta_days_qualifier == _AT_LEAST

    @property
    def delta_days_is_at_least(self) -> bool:
        """Descriptive alias for API and template callers."""

        return self.delta_days_at_least

    @property
    def no_effect(self) -> bool:
        """Whether the valid intervention changes no observable result."""

        return self.effect_status == "no_effect"

    @property
    def has_effect(self) -> bool:
        return not self.no_effect

    @property
    def status(self) -> str:
        return self.effect_status

    @property
    def effect_applied(self) -> bool:
        """Whether the intervention passed applicability and was evaluated."""

        return True

    @property
    def already_breached(self) -> bool:
        return self.baseline.crossing.crossing_day == 0

    @property
    def cure_path(self) -> bool:
        return self.already_breached

    @property
    def is_cure_path(self) -> bool:
        return self.cure_path

    def to_mapping(self) -> Mapping[str, object]:
        """Return the stable, persistence-neutral result shape."""

        return MappingProxyType(
            {
                "intervention_code": self.intervention_code,
                "effect_status": self.effect_status,
                "path": tuple(
                    {
                        "day": point.day,
                        "value": point.value,
                        "trend_component": point.trend_component,
                        "pressure_component": point.pressure_component,
                    }
                    for point in self.path
                ),
                "crossing_date": self.crossing_date,
                "crossing_day": self.crossing_day,
                "probability": self.probability,
                "delta_days": self.delta_days,
                "delta_days_qualifier": self.delta_days_qualifier,
                "delta_probability": self.delta_probability,
                "assumptions": self.assumptions,
                "parameters": self.parameters,
                "content_hash": self.content_hash,
            }
        )

    as_mapping = to_mapping
    to_dict = to_mapping


@dataclass(frozen=True, slots=True)
class SimulationComparison:
    """The baseline plus at most four ordered intervention options."""

    baseline: BaselineResult
    options: tuple[SimulationResult, ...]
    parameters: Mapping[str, object]
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.baseline, BaselineResult):
            raise TypeError("baseline must be a BaselineResult instance.")
        if len(self.options) > MAX_COMPARISON_OPTIONS:
            raise ValueError(
                f"At most four ({MAX_COMPARISON_OPTIONS}) interventions may be compared."
            )
        if any(not isinstance(option, SimulationResult) for option in self.options):
            raise TypeError("options must contain SimulationResult values.")
        codes = tuple(option.intervention_code for option in self.options)
        if len(codes) != len(set(codes)):
            raise ValueError("an intervention may occur only once in a comparison.")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters must be a mapping.")
        if not isinstance(self.content_hash, str) or len(self.content_hash) != 64:
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest.")
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "parameters", _freeze(self.parameters))

    @property
    def results(self) -> tuple[BaselineResult | SimulationResult, ...]:
        """Return the baseline first, followed by options in caller order."""

        return (self.baseline, *self.options)

    @property
    def simulations(self) -> tuple[SimulationResult, ...]:
        return self.options

    @property
    def interventions(self) -> tuple[SimulationResult, ...]:
        return self.options

    @property
    def option_count(self) -> int:
        return len(self.options)

    def to_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "baseline": {
                    "crossing_date": self.baseline.crossing_date,
                    "crossing_day": self.baseline.crossing_day,
                    "probability": self.baseline.probability,
                    "path": tuple(
                        {
                            "day": point.day,
                            "value": point.value,
                            "trend_component": point.trend_component,
                            "pressure_component": point.pressure_component,
                        }
                        for point in self.baseline.path
                    ),
                },
                "options": tuple(option.to_mapping() for option in self.options),
                "parameters": self.parameters,
                "content_hash": self.content_hash,
            }
        )

    as_mapping = to_mapping
    to_dict = to_mapping


@dataclass(frozen=True, slots=True)
class _SimulationParameters:
    covenant_class: str
    weights: Weights
    horizon_days: int
    as_of_date: CalendarDate | None
    threshold_changes: tuple[ThresholdChange | Mapping[str, object] | Sequence[object], ...]
    persisted: Mapping[str, object]


def simulate(
    projection: Projection,
    intervention: InterventionFacts,
    parameters: Mapping[str, object],
) -> SimulationResult:
    """Run one intervention against one stored projection.

    ``parameters`` must identify the covenant class and probability weights.
    The class is required because a projection intentionally contains no
    catalogue metadata; weights are required because probability policy is a
    configuration input, never a code default.  For compatibility with
    stored forecast rows, both may be recovered from their captured formula
    inputs when the caller does not repeat them.
    """

    return _simulate_with_context(_context(projection, parameters), intervention)


def compare(
    projection: Projection,
    interventions: Iterable[InterventionFacts],
    parameters: Mapping[str, object],
) -> SimulationComparison:
    """Compare up to four interventions against one shared baseline."""

    if isinstance(interventions, str | bytes | bytearray):
        raise TypeError("interventions must be an iterable of InterventionFacts, not text.")
    try:
        values = tuple(interventions)
    except TypeError as error:
        raise TypeError("interventions must be an iterable of InterventionFacts.") from error
    if len(values) > MAX_COMPARISON_OPTIONS:
        raise ValueError(
            f"At most four ({MAX_COMPARISON_OPTIONS}) interventions may be compared; "
            f"received {len(values)}."
        )
    if any(not isinstance(item, InterventionFacts) for item in values):
        raise TypeError("interventions must contain InterventionFacts values.")
    context = _context(projection, parameters)
    options = tuple(_simulate_with_context(context, intervention) for intervention in values)
    content_hash = _comparison_hash(context.baseline, options, context.persisted)
    return SimulationComparison(
        baseline=context.baseline,
        options=options,
        parameters=context.persisted,
        content_hash=content_hash,
    )


compare_interventions = compare
simulate_intervention = simulate


def _simulate_with_context(
    context: _SimulationContext,
    intervention: InterventionFacts,
) -> SimulationResult:
    if not isinstance(intervention, InterventionFacts):
        raise TypeError("intervention must be an InterventionFacts instance.")
    transformed_inputs = intervention.apply(
        _stored_inputs(context.source_projection),
        context.covenant_class,
    )
    counterfactual = _recompute_projection(
        context.source_projection,
        transformed_inputs,
        context.horizon_days,
    )
    counter_crossing = _crossing(counterfactual, context)
    counter_probability = _probability(counterfactual, counter_crossing, context.weights)
    assumptions = intervention.assumptions
    if context.baseline.crossing.crossing_day == 0:
        assumptions = _append_assumption(
            assumptions,
            "the already breached covenant is simulated on its cure path",
        )
    delta_days, delta_qualifier = _crossing_delta(
        context.baseline.crossing.crossing_day,
        counter_crossing.crossing_day,
        context.horizon_days,
    )
    delta_probability = _probability_delta(context.baseline.probability, counter_probability)
    status = (
        "no_effect"
        if _observable_signature(counterfactual, counter_crossing, counter_probability)
        == _observable_signature(
            context.baseline.projection,
            context.baseline.crossing,
            context.baseline.probability_result,
        )
        else "applied"
    )
    content_hash = _simulation_hash(
        intervention,
        transformed_inputs,
        context.baseline,
        counterfactual,
        counter_crossing,
        counter_probability,
        assumptions,
        delta_days,
        delta_qualifier,
        delta_probability,
        status,
        context.persisted,
    )
    return SimulationResult(
        intervention=intervention,
        projection=counterfactual,
        crossing=counter_crossing,
        probability_result=counter_probability,
        baseline=context.baseline,
        transformed_inputs=transformed_inputs,
        assumptions=assumptions,
        delta_days=delta_days,
        delta_days_qualifier=delta_qualifier,
        delta_probability=delta_probability,
        effect_status=status,
        parameters=context.persisted,
        content_hash=content_hash,
    )


@dataclass(frozen=True, slots=True)
class _SimulationContext:
    source_projection: Projection
    baseline: BaselineResult
    covenant_class: str
    weights: Weights
    horizon_days: int
    as_of_date: CalendarDate | None
    threshold_changes: tuple[ThresholdChange | Mapping[str, object] | Sequence[object], ...]
    persisted: Mapping[str, object]


def _context(projection: Projection, parameters: Mapping[str, object]) -> _SimulationContext:
    if not isinstance(projection, Projection):
        raise TypeError("projection must be a Projection instance.")
    options = _parameters(projection, parameters)
    baseline_projection = (
        projection
        if options.horizon_days == projection.horizon_days
        else _recompute_projection(
            projection,
            _stored_inputs(projection),
            options.horizon_days,
        )
    )
    baseline_crossing = _crossing(baseline_projection, options)
    baseline_probability = _probability(baseline_projection, baseline_crossing, options.weights)
    return _SimulationContext(
        source_projection=projection,
        baseline=BaselineResult(
            projection=baseline_projection,
            crossing=baseline_crossing,
            probability_result=baseline_probability,
        ),
        covenant_class=options.covenant_class,
        weights=options.weights,
        horizon_days=options.horizon_days,
        as_of_date=options.as_of_date,
        threshold_changes=options.threshold_changes,
        persisted=options.persisted,
    )


def _parameters(projection: Projection, raw: Mapping[str, object]) -> _SimulationParameters:
    if not isinstance(raw, Mapping):
        raise TypeError("simulation parameters must be a mapping.")
    unknown = sorted(set(str(key) for key in raw) - _ACCEPTED_PARAMETER_NAMES)
    if unknown:
        raise ValueError(f"Unknown simulation parameter {unknown[0]!r}.")

    covenant_value = _aliased_value(raw, "covenant_class", "covenant_type")
    if covenant_value is None:
        covenant_value = _formula_value(projection, "covenant_class", "covenant_type")
    if covenant_value is None:
        raise ValueError("simulation parameters require covenant_class.")
    covenant_class = normalize_covenant_class(cast(str, covenant_value))

    supplied_weights = _aliased_value(raw, "weights", "probability_weights", "probability")
    if supplied_weights is None:
        supplied_weights = _formula_value(projection, "probability")
    weights = _resolve_weights(supplied_weights)
    if weights is None:
        raise ValueError("simulation parameters require probability weights.")

    raw_horizon = raw.get("horizon_days", projection.horizon_days)
    if isinstance(raw_horizon, bool) or not isinstance(raw_horizon, int) or raw_horizon < 0:
        raise ValueError("horizon_days must be a non-negative integer.")
    if raw_horizon > projection.horizon_days:
        raise ValueError(
            f"horizon_days cannot exceed the stored projection horizon {projection.horizon_days}."
        )

    as_of_value = _aliased_value(raw, "as_of_date", "as_of", "current_date")
    as_of_date = None if as_of_value is None else _calendar_date(as_of_value, "as_of_date")

    changes_value = _aliased_value(raw, "threshold_changes", "threshold_schedule")
    changes = _normalise_changes(changes_value)
    persisted = MappingProxyType(
        {
            "covenant_class": covenant_class,
            "weights": weights.as_mapping(),
            "horizon_days": raw_horizon,
            "as_of_date": as_of_date,
            "threshold_changes": tuple(_change_mapping(item) for item in changes),
        }
    )
    return _SimulationParameters(
        covenant_class=covenant_class,
        weights=weights,
        horizon_days=raw_horizon,
        as_of_date=as_of_date,
        threshold_changes=changes,
        persisted=persisted,
    )


def _stored_inputs(projection: Projection) -> ProjectionInputs:
    pressure = projection.requested_pressure
    return ProjectionInputs(
        current_value=projection.current_value,
        threshold=projection.threshold,
        direction=projection.direction,
        per_day_drift=projection.per_day_drift,
        pressure=pressure,
    )


def _recompute_projection(
    source: Projection,
    inputs: ProjectionInputs,
    horizon_days: int,
) -> Projection:
    observations = source.usable_observations
    pressure: Decimal | PressureResult = inputs.pressure
    if (
        inputs.pressure == source.requested_pressure
        and inputs.direction is source.direction
        and source.pressure_result is not None
    ):
        pressure = source.pressure_result

    if len(observations) >= 2:
        period_days = source.trend.period_length_days
        if period_days is None or period_days <= _ZERO:
            period_days = _calendar_gap(observations[-2].observed_on, observations[-1].observed_on)
        if inputs.current_value is None:
            series: tuple[Observation, ...] = ()
        else:
            first, last = observations[-2:]
            first_value = inputs.current_value - inputs.per_day_drift * period_days
            series = (
                Observation(
                    observed_on=first.observed_on,
                    value=first_value,
                    source_id=first.source_id,
                ),
                Observation(
                    observed_on=last.observed_on,
                    value=inputs.current_value,
                    source_id=last.source_id,
                ),
            )
        return project(
            series,
            pressure,
            horizon_days,
            inputs.threshold,
            inputs.direction,
            period_days=period_days,
        )

    if len(observations) == 1:
        observation = observations[0]
        return project(
            (
                Observation(
                    observed_on=observation.observed_on,
                    value=inputs.current_value,
                    source_id=observation.source_id,
                ),
            ),
            pressure,
            horizon_days,
            inputs.threshold,
            inputs.direction,
        )

    return project(
        (),
        pressure,
        horizon_days,
        inputs.threshold,
        inputs.direction,
    )


def _crossing(
    projection: Projection,
    options: _SimulationParameters | _SimulationContext,
) -> CrossingResult:
    return first_crossing(
        projection,
        as_of_date=options.as_of_date,
        threshold_changes=options.threshold_changes,
    )


def _probability(
    projection: Projection,
    crossing: CrossingResult,
    weights: Weights,
) -> ProbabilityResult | None:
    endpoint = projection.path[-1].value if projection.path else None
    if endpoint is None:
        return None
    threshold = (
        crossing.threshold_path[-1].threshold if crossing.threshold_path else projection.threshold
    )
    distance = _distance_to_boundary(endpoint, threshold, projection.direction)
    velocity = (
        projection.net_per_day_drift
        if projection.direction is Direction.MAX
        else -projection.net_per_day_drift
    )
    return probability(
        distance,
        velocity,
        projection.pressure,
        projection.horizon_days,
        weights,
        already_breached=crossing.crossing_day == 0,
    )


def _distance_to_boundary(value: Decimal, threshold: Decimal, direction: Direction) -> Decimal:
    if direction is Direction.MAX:
        return max(_ZERO, threshold - value)
    return max(_ZERO, value - threshold)


def _crossing_delta(
    baseline_day: int | None,
    counterfactual_day: int | None,
    horizon_days: int,
) -> tuple[int, str]:
    if baseline_day is not None and counterfactual_day is not None:
        return counterfactual_day - baseline_day, _EXACT
    if baseline_day is not None:
        return horizon_days + 1 - baseline_day, _AT_LEAST
    if counterfactual_day is not None:
        return counterfactual_day - (horizon_days + 1), _AT_MOST
    return 0, _EXACT


def _probability_delta(
    baseline: Decimal | None,
    counterfactual: ProbabilityResult | None,
) -> Decimal | None:
    if baseline is None or counterfactual is None:
        return None
    return counterfactual.probability - baseline


def _observable_signature(
    projection: Projection,
    crossing: CrossingResult,
    probability_result: ProbabilityResult | None,
) -> tuple[object, ...]:
    probability_value = None if probability_result is None else probability_result.probability
    return (
        projection.threshold,
        projection.direction.value,
        projection.values,
        crossing.crossing_day,
        crossing.crossing_date,
        crossing.threshold_used,
        probability_value,
    )


def _append_assumption(values: tuple[str, ...], assumption: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*values, assumption)))


def _resolve_weights(value: object) -> Weights | None:
    if value is None:
        return None
    if isinstance(value, Weights):
        return value
    if isinstance(value, ProbabilityResult):
        return value.weights
    if isinstance(value, Mapping):
        return Weights.from_mapping(cast(Mapping[str, object], value))
    raise TypeError("probability weights must be a Weights instance or mapping.")


def _formula_value(projection: Projection, *names: str) -> object | None:
    values = projection.formula_inputs
    for name in names:
        candidate = values.get(name)
        if candidate is not None:
            return candidate
    probability_inputs = values.get("probability")
    if "probability" in names and probability_inputs is not None:
        return probability_inputs
    return None


def _aliased_value(raw: Mapping[str, object], *names: str) -> object | None:
    supplied = [(name, raw[name]) for name in names if name in raw]
    if not supplied:
        return None
    first_name, first_value = supplied[0]
    for name, value in supplied[1:]:
        if value != first_value:
            raise ValueError(f"{first_name} and {name} must identify the same value.")
    return first_value


def _normalise_changes(
    value: object,
) -> tuple[ThresholdChange | Mapping[str, object] | Sequence[object], ...]:
    if value is None:
        return ()
    if isinstance(value, ThresholdChange | Mapping):
        return (cast(ThresholdChange | Mapping[str, object], value),)
    if isinstance(value, str | bytes | bytearray):
        raise TypeError("threshold_changes must be an iterable of change records, not text.")
    try:
        records = tuple(cast(Iterable[object], value))
    except TypeError as error:
        raise TypeError("threshold_changes must be an iterable of change records.") from error
    return cast(tuple[ThresholdChange | Mapping[str, object] | Sequence[object], ...], records)


def _change_mapping(
    change: ThresholdChange | Mapping[str, object] | Sequence[object],
) -> Mapping[str, object] | tuple[object, ...]:
    if isinstance(change, ThresholdChange):
        return MappingProxyType(
            {
                "threshold": change.threshold,
                "effective_day": change.effective_day,
                "effective_date": change.effective_date,
                "reason": change.reason,
            }
        )
    if isinstance(change, Mapping):
        return MappingProxyType({str(key): value for key, value in change.items()})
    return tuple(change)


def _calendar_date(value: object, field_name: str) -> CalendarDate:
    if isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a calendar date, not a datetime.")
    if isinstance(value, CalendarDate):
        return value
    if isinstance(value, str):
        try:
            return CalendarDate.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO calendar date.") from error
    raise TypeError(f"{field_name} must be a calendar date or ISO date text.")


def _calendar_gap(first: CalendarDate, last: CalendarDate) -> Decimal:
    days = (last - first).days
    if days <= 0:
        raise ValueError("stored observations must span at least one calendar day.")
    return Decimal(days)


def _simulation_hash(
    intervention: InterventionFacts,
    transformed_inputs: ProjectionInputs,
    baseline: BaselineResult,
    counterfactual: Projection,
    counter_crossing: CrossingResult,
    counter_probability: ProbabilityResult | None,
    assumptions: tuple[str, ...],
    delta_days: int | None,
    delta_qualifier: str,
    delta_probability: Decimal | None,
    status: str,
    parameters: Mapping[str, object],
) -> str:
    payload = {
        "intervention": {
            "code": intervention.code,
            "text": intervention.text,
            "effect_model": intervention.model_type.value,
            "effect_parameters": intervention.effect_parameters,
            "applicable_covenant_classes": tuple(sorted(intervention.applicable_covenant_classes)),
        },
        "transformed_inputs": {
            "current_value": transformed_inputs.current_value,
            "threshold": transformed_inputs.threshold,
            "direction": Direction.from_value(transformed_inputs.direction).value,
            "per_day_drift": transformed_inputs.per_day_drift,
            "pressure": transformed_inputs.pressure,
        },
        "baseline": _forecast_payload(
            baseline.projection,
            baseline.crossing,
            baseline.probability_result,
        ),
        "counterfactual": _forecast_payload(
            counterfactual,
            counter_crossing,
            counter_probability,
        ),
        "assumptions": assumptions,
        "delta_days": delta_days,
        "delta_days_qualifier": delta_qualifier,
        "delta_probability": delta_probability,
        "effect_status": status,
        "parameters": parameters,
    }
    return _hash_payload(payload)


def _comparison_hash(
    baseline: BaselineResult,
    options: Sequence[SimulationResult],
    parameters: Mapping[str, object],
) -> str:
    payload = {
        "baseline": _forecast_payload(
            baseline.projection,
            baseline.crossing,
            baseline.probability_result,
        ),
        "options": tuple(option.content_hash for option in options),
        "parameters": parameters,
    }
    return _hash_payload(payload)


def _forecast_payload(
    projection: Projection,
    crossing: CrossingResult,
    probability_result: ProbabilityResult | None,
) -> Mapping[str, object]:
    return {
        "threshold": projection.threshold,
        "direction": projection.direction.value,
        "horizon_days": projection.horizon_days,
        "path": tuple(
            {
                "day": point.day,
                "value": point.value,
                "trend_component": point.trend_component,
                "pressure_component": point.pressure_component,
            }
            for point in projection.path
        ),
        "crossing_day": crossing.crossing_day,
        "crossing_date": crossing.crossing_date,
        "crossing_value": crossing.crossing_value,
        "threshold_used": crossing.threshold_used,
        "probability": None if probability_result is None else probability_result.probability,
    }


def _hash_payload(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, CalendarDate):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, frozenset | set):
        return sorted((_json_safe(item) for item in value), key=str)
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"simulation content contains unsupported value {type(value).__name__}.")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
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


__all__ = [
    "BaselineResult",
    "MAX_COMPARISON_OPTIONS",
    "SimulationComparison",
    "SimulationResult",
    "compare",
    "compare_interventions",
    "simulate",
    "simulate_intervention",
]
