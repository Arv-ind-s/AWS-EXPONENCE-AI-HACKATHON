"""Transparent covenant-breach probability mapping (contract ``C-36``).

The probability in this module is a calibrated *risk score*, not a claim of
statistical certainty.  It is intentionally deterministic and inspectable so
the forecast trace can show exactly how it was formed.

``distance`` is the remaining, non-negative distance to the covenant
boundary.  A smaller positive distance means greater proximity to breach and
therefore a larger distance signal.  ``velocity`` is the signed rate of
change; a negative value is improving and contributes no deterioration
signal.  ``pressure`` is the non-negative magnitude of sustained evidence
pressure.  Each signal is saturated into ``[0, 1]`` before the configured
weights are applied:

* distance proximity: ``1 / (1 + distance)``;
* velocity and pressure: ``x / (1 + x)`` for their non-negative parts;
* velocity and pressure contributions are multiplied by
  ``horizon_days / (horizon_days + 1)``.

The distance term represents the current state, while velocity and pressure
represent forward-looking deterioration.  The weighted sum is clamped to the
configured maximum, whose default is the contract maximum of ``0.99``.  The
zero-signal tuple ``(0, 0, 0)`` is a deliberate neutral sentinel and returns
zero.  Otherwise, a zero or negative distance denotes an immediate boundary
condition and is fixed at the configured maximum.  An explicit
``already_breached`` flag has the same override and is recorded in the
result.

Weights are supplied by the configuration boundary through :meth:`Weights`
or :meth:`Weights.from_mapping`; this module has no policy weight defaults.
Relative non-negative weights are normalized once at construction, and the
normalized values are the ones retained in every result and trace term.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Final, cast

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_DEFAULT_MAX_PROBABILITY: Final[Decimal] = Decimal("0.99")
_MAPPING_VERSION: Final[str] = "forecast.probability.v1"
_NEUTRAL_REASON: Final[str] = "neutral inputs: distance, velocity and pressure provide no signal"
_CLAMP_REASON: Final[str] = "raw probability exceeded the configured maximum and was clamped"
_BREACH_REASON: Final[str] = (
    "covenant is already in breach; probability is fixed at the configured maximum"
)
_BOUNDARY_REASON: Final[str] = (
    "covenant is at its boundary with a non-neutral signal; "
    "probability is fixed at the configured maximum"
)


@dataclass(frozen=True, slots=True)
class Weights:
    """Configuration for the three-term probability mapping.

    The three weights may be supplied as relative non-negative values.  They
    are normalized to sum to one so the mapping remains bounded before the
    final contract clamp.  ``max_probability`` is also configuration, and
    must be below one so a client can never display certainty.

    ``from_mapping`` accepts either a direct mapping with ``distance``,
    ``velocity`` and ``pressure`` keys, or a configuration section containing
    a nested ``weights`` mapping.  The ``*_weight`` spellings are accepted as
    explicit aliases for adapter convenience.
    """

    distance: Decimal
    velocity: Decimal
    pressure: Decimal
    max_probability: Decimal = _DEFAULT_MAX_PROBABILITY

    def __post_init__(self) -> None:
        distance = _decimal(self.distance, "distance weight")
        velocity = _decimal(self.velocity, "velocity weight")
        pressure = _decimal(self.pressure, "pressure weight")
        maximum = _decimal(self.max_probability, "max_probability")
        values = (distance, velocity, pressure)
        if any(value < _ZERO for value in values):
            raise ValueError("Probability weights must be non-negative.")
        total = sum(values, _ZERO)
        if total <= _ZERO:
            raise ValueError("At least one probability weight must be positive.")
        if not _ZERO < maximum < _ONE:
            raise ValueError("max_probability must be greater than zero and less than one.")

        object.__setattr__(self, "distance", distance / total)
        object.__setattr__(self, "velocity", velocity / total)
        object.__setattr__(self, "pressure", pressure / total)
        object.__setattr__(self, "max_probability", maximum)

    @classmethod
    def from_mapping(cls, config: Mapping[str, object]) -> Weights:
        """Build weights from an adapter-neutral configuration mapping.

        A malformed or incomplete configuration fails closed with a field
        named in the exception.  No default weights are introduced here,
        which keeps replays tied to the configuration captured for their run.
        """

        if not isinstance(config, Mapping):
            raise TypeError("Probability configuration must be a mapping.")

        section: Mapping[str, object] = config
        for section_name in ("probability", "forecast_probability"):
            candidate = config.get(section_name)
            if candidate is not None:
                if not isinstance(candidate, Mapping):
                    raise TypeError(f"{section_name} configuration must be a mapping.")
                section = candidate
                break

        nested_weights = section.get("weights")
        if nested_weights is not None:
            if not isinstance(nested_weights, Mapping):
                raise TypeError("probability.weights must be a mapping.")
            unknown_section_fields = set(section) - {"weights", "max_probability"}
            if unknown_section_fields:
                unknown = ", ".join(sorted(str(name) for name in unknown_section_fields))
                raise ValueError(f"Unknown probability configuration field(s): {unknown}.")
            values = dict(nested_weights)
            if "max_probability" in section:
                if "max_probability" in values:
                    raise ValueError("max_probability must be declared only once.")
                values["max_probability"] = section["max_probability"]
        else:
            values = dict(section)

        canonical: dict[str, object] = {}
        aliases = {
            "distance": "distance",
            "distance_weight": "distance",
            "velocity": "velocity",
            "velocity_weight": "velocity",
            "pressure": "pressure",
            "pressure_weight": "pressure",
            "max_probability": "max_probability",
        }
        for raw_name, value in values.items():
            name = aliases.get(str(raw_name))
            if name is None:
                raise ValueError(f"Unknown probability configuration field {raw_name!r}.")
            if name in canonical:
                raise ValueError(f"Probability configuration field {name!r} is duplicated.")
            canonical[name] = value

        missing = [name for name in ("distance", "velocity", "pressure") if name not in canonical]
        if missing:
            raise ValueError("Probability configuration is missing: " + ", ".join(missing) + ".")
        return cls(
            distance=cast(Decimal, canonical["distance"]),
            velocity=cast(Decimal, canonical["velocity"]),
            pressure=cast(Decimal, canonical["pressure"]),
            max_probability=cast(
                Decimal,
                canonical.get("max_probability", _DEFAULT_MAX_PROBABILITY),
            ),
        )

    @property
    def distance_weight(self) -> Decimal:
        """Return the normalized distance weight."""

        return self.distance

    @property
    def velocity_weight(self) -> Decimal:
        """Return the normalized velocity weight."""

        return self.velocity

    @property
    def pressure_weight(self) -> Decimal:
        """Return the normalized pressure weight."""

        return self.pressure

    def as_mapping(self) -> Mapping[str, Decimal]:
        """Return immutable normalized configuration for persistence or trace."""

        return MappingProxyType(
            {
                "distance": self.distance,
                "velocity": self.velocity,
                "pressure": self.pressure,
                "max_probability": self.max_probability,
            }
        )


@dataclass(frozen=True, slots=True)
class ProbabilityTerm:
    """One fully inspectable term in a probability calculation."""

    name: str
    input_value: Decimal
    normalized_value: Decimal
    weight: Decimal
    horizon_factor: Decimal
    contribution: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Probability term name must not be blank.")
        input_value = _decimal(self.input_value, f"{self.name} input")
        normalized_value = _decimal(self.normalized_value, f"{self.name} normalized value")
        weight = _decimal(self.weight, f"{self.name} weight")
        horizon_factor = _decimal(self.horizon_factor, f"{self.name} horizon factor")
        contribution = _decimal(self.contribution, f"{self.name} contribution")
        if not _ZERO <= normalized_value <= _ONE:
            raise ValueError(f"{self.name} normalized value must be between zero and one.")
        if not _ZERO <= weight <= _ONE:
            raise ValueError(f"{self.name} weight must be between zero and one.")
        if not _ZERO <= horizon_factor <= _ONE:
            raise ValueError(f"{self.name} horizon factor must be between zero and one.")
        if contribution < _ZERO:
            raise ValueError(f"{self.name} contribution must be non-negative.")
        object.__setattr__(self, "input_value", input_value)
        object.__setattr__(self, "normalized_value", normalized_value)
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "horizon_factor", horizon_factor)
        object.__setattr__(self, "contribution", contribution)

    @property
    def raw_value(self) -> Decimal:
        """Compatibility spelling for the original input value."""

        return self.input_value

    @property
    def normalised_value(self) -> Decimal:
        """British spelling used by product documentation."""

        return self.normalized_value

    @property
    def weighted_contribution(self) -> Decimal:
        """Return this term's contribution to the raw score."""

        return self.contribution


@dataclass(frozen=True, slots=True)
class ProbabilityResult:
    """Probability plus all facts required to explain or persist it."""

    probability: Decimal
    raw_score: Decimal
    max_probability: Decimal
    clamped: bool
    clamp_reason: str | None
    reason: str | None
    already_breached: bool
    distance: Decimal
    velocity: Decimal
    pressure: Decimal
    horizon_days: int
    horizon_factor: Decimal
    normalized_distance: Decimal
    normalized_velocity: Decimal
    normalized_pressure: Decimal
    weights: Weights
    terms: tuple[ProbabilityTerm, ...]
    formula_inputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        probability_value = _decimal(self.probability, "probability")
        raw_score = _decimal(self.raw_score, "raw_score")
        maximum = _decimal(self.max_probability, "max_probability")
        if not _ZERO <= probability_value <= maximum:
            raise ValueError("probability must be between zero and max_probability.")
        if raw_score < _ZERO:
            raise ValueError("raw_score must be non-negative.")
        if not _ZERO < maximum < _ONE:
            raise ValueError("max_probability must be greater than zero and less than one.")
        if not isinstance(self.clamped, bool):
            raise TypeError("clamped must be a boolean.")
        if self.clamped and not self.clamp_reason:
            raise ValueError("A clamped probability must carry a clamp_reason.")
        if not isinstance(self.already_breached, bool):
            raise TypeError("already_breached must be a boolean.")
        if self.already_breached and probability_value != maximum:
            raise ValueError("An already breached covenant must be at max_probability.")
        if not isinstance(self.horizon_days, int) or isinstance(self.horizon_days, bool):
            raise TypeError("horizon_days must be a non-negative integer.")
        if self.horizon_days < 0:
            raise ValueError("horizon_days must be non-negative.")
        if not isinstance(self.weights, Weights):
            raise TypeError("weights must be a Weights instance.")
        if len(self.terms) != 3 or {term.name for term in self.terms} != {
            "distance",
            "velocity",
            "pressure",
        }:
            raise ValueError(
                "ProbabilityResult must contain exactly distance, velocity and pressure terms."
            )
        if not isinstance(self.formula_inputs, Mapping):
            raise TypeError("formula_inputs must be a mapping.")
        object.__setattr__(self, "probability", probability_value)
        object.__setattr__(self, "raw_score", raw_score)
        object.__setattr__(self, "max_probability", maximum)
        object.__setattr__(self, "formula_inputs", _freeze(self.formula_inputs))

    @property
    def value(self) -> Decimal:
        """Return the score for callers that use generic value terminology."""

        return self.probability

    @property
    def probability_value(self) -> Decimal:
        """Return the Decimal value without discarding the trace object."""

        return self.probability

    @property
    def was_clamped(self) -> bool:
        """Return whether the output was forced to its configured maximum."""

        return self.clamped

    @property
    def terms_by_name(self) -> Mapping[str, ProbabilityTerm]:
        """Return immutable name-based access to the three trace terms."""

        return MappingProxyType({term.name: term for term in self.terms})

    @property
    def term_map(self) -> Mapping[str, ProbabilityTerm]:
        """Alias for :attr:`terms_by_name`."""

        return self.terms_by_name


def probability(
    distance: Decimal,
    velocity: Decimal,
    pressure: Decimal,
    horizon_days: int,
    weights: Weights | Mapping[str, object],
    *,
    already_breached: bool = False,
) -> ProbabilityResult:
    """Return a bounded, traceable breach probability for one horizon.

    ``distance`` is remaining distance to the threshold, so decreasing it
    increases the distance-proximity signal.  ``velocity`` may be signed;
    negative velocity means improvement and contributes zero.  ``pressure``
    is an evidence-pressure magnitude and must not be negative.

    The explicit zero-signal tuple is neutral and returns zero.  A negative
    distance or an explicit ``already_breached`` flag takes precedence over
    the mapping and returns the configured maximum for every horizon.  A zero
    distance with any non-neutral signal is treated as an immediate boundary
    for consistency with the covenant engine's inclusive boundary convention.
    """

    configured_weights = weights if isinstance(weights, Weights) else Weights.from_mapping(weights)
    distance_value = _decimal(distance, "distance")
    velocity_value = _decimal(velocity, "velocity")
    pressure_value = _decimal(pressure, "pressure")
    horizon = _horizon(horizon_days)
    if pressure_value < _ZERO:
        raise ValueError("pressure must be non-negative.")
    if not isinstance(already_breached, bool):
        raise TypeError("already_breached must be a boolean.")

    positive_velocity = max(velocity_value, _ZERO)
    neutral = (
        distance_value == _ZERO
        and velocity_value == _ZERO
        and pressure_value == _ZERO
        and not already_breached
    )
    boundary_breach = distance_value <= _ZERO and not neutral
    breached = already_breached or distance_value < _ZERO or boundary_breach

    normalized_distance = _normalise_distance(distance_value, neutral=neutral)
    normalized_velocity = _saturate(positive_velocity)
    normalized_pressure = _saturate(pressure_value)
    horizon_factor = _horizon_factor(horizon)
    terms = (
        _term(
            "distance",
            distance_value,
            normalized_distance,
            configured_weights.distance,
            _ONE,
        ),
        _term(
            "velocity",
            velocity_value,
            normalized_velocity,
            configured_weights.velocity,
            horizon_factor,
        ),
        _term(
            "pressure",
            pressure_value,
            normalized_pressure,
            configured_weights.pressure,
            horizon_factor,
        ),
    )
    raw_score = sum((term.contribution for term in terms), _ZERO)

    if neutral:
        result = _ZERO
        clamped = False
        clamp_reason = None
        reason = _NEUTRAL_REASON
    elif breached:
        result = configured_weights.max_probability
        clamped = True
        clamp_reason = _BREACH_REASON
        reason = _BREACH_REASON if distance_value < _ZERO or already_breached else _BOUNDARY_REASON
    elif raw_score > configured_weights.max_probability:
        result = configured_weights.max_probability
        clamped = True
        clamp_reason = _CLAMP_REASON
        reason = _CLAMP_REASON
    else:
        result = raw_score
        clamped = False
        clamp_reason = None
        reason = None

    formula_inputs = {
        "mapping_version": _MAPPING_VERSION,
        "distance": distance_value,
        "velocity": velocity_value,
        "pressure": pressure_value,
        "horizon_days": horizon,
        "horizon_factor": horizon_factor,
        "normalized_distance": normalized_distance,
        "normalized_velocity": normalized_velocity,
        "normalized_pressure": normalized_pressure,
        "weights": configured_weights.as_mapping(),
        "raw_score": raw_score,
        "max_probability": configured_weights.max_probability,
        "probability": result,
        "clamped": clamped,
        "clamp_reason": clamp_reason,
        "already_breached": already_breached,
        "breach_override": breached,
        "reason": reason,
        "terms": {
            term.name: {
                "input_value": term.input_value,
                "normalized_value": term.normalized_value,
                "weight": term.weight,
                "horizon_factor": term.horizon_factor,
                "contribution": term.contribution,
            }
            for term in terms
        },
    }
    return ProbabilityResult(
        probability=result,
        raw_score=raw_score,
        max_probability=configured_weights.max_probability,
        clamped=clamped,
        clamp_reason=clamp_reason,
        reason=reason,
        already_breached=breached,
        distance=distance_value,
        velocity=velocity_value,
        pressure=pressure_value,
        horizon_days=horizon,
        horizon_factor=horizon_factor,
        normalized_distance=normalized_distance,
        normalized_velocity=normalized_velocity,
        normalized_pressure=normalized_pressure,
        weights=configured_weights,
        terms=terms,
        formula_inputs=formula_inputs,
    )


def probability_value(
    distance: Decimal,
    velocity: Decimal,
    pressure: Decimal,
    horizon_days: int,
    weights: Weights | Mapping[str, object],
    *,
    already_breached: bool = False,
) -> Decimal:
    """Return only the Decimal value for persistence-facing adapters."""

    return probability(
        distance,
        velocity,
        pressure,
        horizon_days,
        weights,
        already_breached=already_breached,
    ).probability


def _term(
    name: str,
    input_value: Decimal,
    normalized_value: Decimal,
    weight: Decimal,
    horizon_factor: Decimal,
) -> ProbabilityTerm:
    return ProbabilityTerm(
        name=name,
        input_value=input_value,
        normalized_value=normalized_value,
        weight=weight,
        horizon_factor=horizon_factor,
        contribution=normalized_value * weight * horizon_factor,
    )


def _normalise_distance(value: Decimal, *, neutral: bool) -> Decimal:
    if neutral:
        return _ZERO
    if value <= _ZERO:
        return _ONE
    return _ONE / (_ONE + value)


def _saturate(value: Decimal) -> Decimal:
    return value / (_ONE + value)


def _horizon_factor(horizon_days: int) -> Decimal:
    return Decimal(horizon_days) / (Decimal(horizon_days) + _ONE)


def _horizon(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("horizon_days must be a non-negative integer.")
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


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = [
    "ProbabilityResult",
    "ProbabilityTerm",
    "Weights",
    "probability",
    "probability_value",
]
