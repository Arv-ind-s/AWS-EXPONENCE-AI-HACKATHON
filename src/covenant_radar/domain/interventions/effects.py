"""Immutable intervention effect models (T-062).

The forecast stage is a deterministic function of its inputs.  An
intervention therefore changes only an explicitly named input and returns a
new ``ProjectionInputs`` value; it never mutates a stored forecast or edits a
path in place.  T-063 can pass the returned inputs to the existing forecast
and crossing functions.

The five effect types are intentionally closed:

* ``LevelShiftEffect`` adds a signed amount to the current input level.
* ``RateChangeEffect`` multiplies the fitted per-day drift.
* ``ThresholdRelaxationEffect`` moves the threshold in the favourable
  direction for the covenant side.
* ``PressureReductionEffect`` removes a fraction of directional pressure.
* ``CombinationEffect`` applies those components in the documented order
  ``level shift -> rate change -> pressure reduction -> threshold
  relaxation``.

Every effect carries non-empty assumptions and a non-empty applicability set.
The values are normalized and frozen at construction so a catalogue row
cannot change meaning after validation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Final, cast

from covenant_radar.domain.forecast import Direction
from covenant_radar.domain.interventions.applicability import (
    CovenantClass,
    InterventionNotApplicable,
    is_applicable,
    normalize_covenant_classes,
    require_applicable,
)

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_MAX_PARAMETER_MAGNITUDE: Final[Decimal] = Decimal("1000000")
_MAX_TEXT_LENGTH: Final[int] = 500
_MAX_CODE_LENGTH: Final[int] = 100


class EffectModelType(StrEnum):
    """The closed set of effect models supported by the simulator."""

    LEVEL_SHIFT = "level_shift"
    RATE_CHANGE = "rate_change"
    THRESHOLD_RELAXATION = "threshold_relaxation"
    PRESSURE_REDUCTION = "pressure_reduction"
    COMBINATION = "combination"

    @classmethod
    def from_value(cls, value: EffectModelType | str) -> EffectModelType:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("effect_model must be an EffectModelType or text.")
        try:
            return cls(value.strip().lower())
        except ValueError as error:
            options = ", ".join(item.value for item in cls)
            raise ValueError(f"effect_model must be one of: {options}.") from error


# Alternate vocabulary used by some service callers and persisted fixtures.
EffectType = EffectModelType


@dataclass(frozen=True, slots=True)
class ProjectionInputs:
    """The validated scalar inputs an intervention is permitted to change."""

    current_value: Decimal | None
    threshold: Decimal
    direction: Direction | str
    per_day_drift: Decimal
    pressure: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "current_value",
            _optional_decimal(self.current_value, "current_value"),
        )
        object.__setattr__(self, "threshold", _decimal(self.threshold, "threshold"))
        object.__setattr__(self, "direction", Direction.from_value(self.direction))
        object.__setattr__(self, "per_day_drift", _decimal(self.per_day_drift, "per_day_drift"))
        pressure = _decimal(self.pressure, "pressure")
        if pressure < _ZERO:
            raise ValueError("pressure must be non-negative.")
        object.__setattr__(self, "pressure", pressure)

    @property
    def signed_pressure(self) -> Decimal:
        """Return pressure with the covenant's deterioration side applied."""

        return self.pressure if self.direction is Direction.MAX else -self.pressure

    @property
    def net_per_day_drift(self) -> Decimal:
        """Return the drift plus the directional pressure term."""

        return self.per_day_drift + self.signed_pressure

    @property
    def drift(self) -> Decimal:
        """Compatibility spelling for ``per_day_drift``."""

        return self.per_day_drift

    @property
    def pressure_term(self) -> Decimal:
        """Compatibility spelling for the signed pressure term."""

        return self.signed_pressure


class InterventionEffect:
    """Common interface for all immutable effect models."""

    model_type: ClassVar[EffectModelType]
    assumptions: tuple[str, ...]
    applicable_covenant_classes: frozenset[str]

    def transform(self, inputs: ProjectionInputs) -> ProjectionInputs:
        """Apply the pure arithmetic transform without an applicability check."""

        raise NotImplementedError

    def apply(
        self,
        inputs: ProjectionInputs,
        covenant_class: CovenantClass | str,
    ) -> ProjectionInputs:
        """Validate applicability, then return transformed inputs."""

        require_applicable(self, covenant_class, effect_model=self.model_type.value)
        return self.transform(inputs)

    @property
    def effect_model(self) -> EffectModelType:
        return self.model_type

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        """Return canonical, read-only parameters for persistence and tracing."""

        raise NotImplementedError

    def parameters(self) -> Mapping[str, object]:
        """Method form retained for service code that treats effects as callables."""

        return self.effect_parameters


@dataclass(frozen=True, slots=True)
class LevelShiftEffect(InterventionEffect):
    """Add a signed amount to the current trajectory level."""

    amount: Decimal
    assumptions: tuple[str, ...]
    applicable_covenant_classes: frozenset[str]
    model_type: ClassVar[EffectModelType] = EffectModelType.LEVEL_SHIFT

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _bounded_decimal(self.amount, "amount"))
        _set_common_fields(self, self.assumptions, self.applicable_covenant_classes)

    def transform(self, inputs: ProjectionInputs) -> ProjectionInputs:
        current = None if inputs.current_value is None else inputs.current_value + self.amount
        return _replace_inputs(inputs, current_value=current)

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        return MappingProxyType({"amount": self.amount})


@dataclass(frozen=True, slots=True)
class RateChangeEffect(InterventionEffect):
    """Multiply the fitted per-day drift by ``multiplier``."""

    multiplier: Decimal
    assumptions: tuple[str, ...]
    applicable_covenant_classes: frozenset[str]
    model_type: ClassVar[EffectModelType] = EffectModelType.RATE_CHANGE

    def __post_init__(self) -> None:
        multiplier = _decimal(self.multiplier, "multiplier")
        if not _ZERO <= multiplier <= Decimal("2"):
            raise ValueError("multiplier must be between 0 and 2 inclusive.")
        object.__setattr__(self, "multiplier", multiplier)
        _set_common_fields(self, self.assumptions, self.applicable_covenant_classes)

    def transform(self, inputs: ProjectionInputs) -> ProjectionInputs:
        return _replace_inputs(inputs, per_day_drift=inputs.per_day_drift * self.multiplier)

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        return MappingProxyType({"multiplier": self.multiplier})


@dataclass(frozen=True, slots=True)
class ThresholdRelaxationEffect(InterventionEffect):
    """Move the threshold away from the breach side by a non-negative amount."""

    amount: Decimal
    assumptions: tuple[str, ...]
    applicable_covenant_classes: frozenset[str]
    model_type: ClassVar[EffectModelType] = EffectModelType.THRESHOLD_RELAXATION

    def __post_init__(self) -> None:
        amount = _bounded_decimal(self.amount, "amount")
        if amount < _ZERO:
            raise ValueError("amount must be non-negative for threshold relaxation.")
        object.__setattr__(self, "amount", amount)
        _set_common_fields(self, self.assumptions, self.applicable_covenant_classes)

    def transform(self, inputs: ProjectionInputs) -> ProjectionInputs:
        threshold = (
            inputs.threshold + self.amount
            if inputs.direction is Direction.MAX
            else inputs.threshold - self.amount
        )
        return _replace_inputs(inputs, threshold=threshold)

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        return MappingProxyType({"amount": self.amount})


@dataclass(frozen=True, slots=True)
class PressureReductionEffect(InterventionEffect):
    """Remove a fraction of the evidence-pressure magnitude."""

    fraction: Decimal
    assumptions: tuple[str, ...]
    applicable_covenant_classes: frozenset[str]
    model_type: ClassVar[EffectModelType] = EffectModelType.PRESSURE_REDUCTION

    def __post_init__(self) -> None:
        fraction = _decimal(self.fraction, "fraction")
        if not _ZERO <= fraction <= _ONE:
            raise ValueError("fraction must be between 0 and 1 inclusive.")
        object.__setattr__(self, "fraction", fraction)
        _set_common_fields(self, self.assumptions, self.applicable_covenant_classes)

    def transform(self, inputs: ProjectionInputs) -> ProjectionInputs:
        return _replace_inputs(inputs, pressure=inputs.pressure * (_ONE - self.fraction))

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        return MappingProxyType({"fraction": self.fraction})


_COMBINATION_ORDER: Final[tuple[EffectModelType, ...]] = (
    EffectModelType.LEVEL_SHIFT,
    EffectModelType.RATE_CHANGE,
    EffectModelType.PRESSURE_REDUCTION,
    EffectModelType.THRESHOLD_RELAXATION,
)
COMBINATION_ORDER: tuple[EffectModelType, ...] = _COMBINATION_ORDER


@dataclass(frozen=True, slots=True)
class CombinationEffect(InterventionEffect):
    """Compose effects in one stable, explicitly documented order."""

    components: tuple[InterventionEffect, ...]
    assumptions: tuple[str, ...]
    applicable_covenant_classes: frozenset[str] = frozenset()
    model_type: ClassVar[EffectModelType] = EffectModelType.COMBINATION

    def __post_init__(self) -> None:
        components = tuple(self.components)
        if len(components) < 2:
            raise ValueError("components must contain at least two effects.")
        if any(not isinstance(component, InterventionEffect) for component in components):
            raise TypeError("components must contain InterventionEffect instances.")
        if any(component.model_type is EffectModelType.COMBINATION for component in components):
            raise ValueError("a combination cannot contain another combination effect.")
        if any(component.model_type not in _COMBINATION_ORDER for component in components):
            raise ValueError("components must use one of the closed effect model types.")
        normalized_assumptions = _normalize_assumptions(self.assumptions)
        object.__setattr__(self, "assumptions", normalized_assumptions)
        declared = self.applicable_covenant_classes
        component_classes = set(components[0].applicable_covenant_classes)
        for component in components[1:]:
            component_classes.update(component.applicable_covenant_classes)
        if not declared:
            compatible_classes = frozenset(
                candidate
                for candidate in component_classes
                if all(is_applicable(component, candidate) for component in components)
            )
            if not compatible_classes:
                raise ValueError("components have no common applicable covenant class.")
            normalized_classes = compatible_classes
        else:
            normalized_classes = normalize_covenant_classes(declared)
            if not all(
                is_applicable(component, candidate)
                for candidate in normalized_classes
                if candidate not in {"*", "all", "any"}
                for component in components
            ) and not normalized_classes & frozenset({"*", "all", "any"}):
                raise ValueError(
                    "applicable_covenant_classes must be supported by every component."
                )
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "applicable_covenant_classes", normalized_classes)

        # The combination's assumptions include its own statement followed by
        # every component assumption in execution order, with duplicates
        # removed while preserving the first occurrence.
        ordered_components = tuple(
            component
            for effect_type in _COMBINATION_ORDER
            for component in components
            if component.model_type is effect_type
        )
        component_assumptions = _unique_assumptions(
            assumption for item in ordered_components for assumption in item.assumptions
        )
        all_assumptions = _unique_assumptions((*self.assumptions, *component_assumptions))
        if not all_assumptions:
            raise ValueError("assumptions must contain at least one non-blank assumption.")
        object.__setattr__(self, "assumptions", all_assumptions)

    def transform(self, inputs: ProjectionInputs) -> ProjectionInputs:
        transformed = inputs
        for effect_type in _COMBINATION_ORDER:
            for component in self.components:
                if component.model_type is effect_type:
                    transformed = component.transform(transformed)
        return transformed

    def apply(
        self,
        inputs: ProjectionInputs,
        covenant_class: CovenantClass | str,
    ) -> ProjectionInputs:
        require_applicable(self, covenant_class, effect_model=self.model_type.value)
        transformed = inputs
        for effect_type in _COMBINATION_ORDER:
            for component in self.components:
                if component.model_type is effect_type:
                    # Check every component as well as the aggregate set.  A
                    # malformed catalogue row must never bypass a component's
                    # own applicability rule.
                    transformed = component.apply(transformed, covenant_class)
        return transformed

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "order": tuple(effect_type.value for effect_type in _COMBINATION_ORDER),
                "components": tuple(
                    MappingProxyType(
                        {
                            "effect_model": component.model_type.value,
                            "parameters": component.effect_parameters,
                        }
                    )
                    for component in self.components
                ),
            }
        )


InterventionEffectModel = InterventionEffect
EffectModel = InterventionEffect
LevelShift = LevelShiftEffect
LevelShiftModel = LevelShiftEffect
RateChange = RateChangeEffect
RateChangeModel = RateChangeEffect
ThresholdRelaxation = ThresholdRelaxationEffect
ThresholdRelaxationModel = ThresholdRelaxationEffect
PressureReduction = PressureReductionEffect
PressureReductionModel = PressureReductionEffect
Combination = CombinationEffect
CombinationModel = CombinationEffect
ProjectionInput = ProjectionInputs


@dataclass(frozen=True, slots=True, init=False)
class InterventionFacts:
    """Immutable intervention snapshot consumed by the simulator contract."""

    code: str
    effect: InterventionEffect
    text: str | None

    def __init__(
        self,
        code: str | None = None,
        effect: InterventionEffect | None = None,
        *,
        intervention_id: str | None = None,
        effect_model: EffectModelType | str | InterventionEffect | None = None,
        effect_parameters: Mapping[str, object] | None = None,
        assumptions: Iterable[str] | None = None,
        applicable_covenant_classes: Iterable[CovenantClass | str] | None = None,
        text: str | None = None,
    ) -> None:
        normalized_code = _identifier(code if code is not None else intervention_id, "code")
        if code is not None and intervention_id is not None:
            if normalized_code != _identifier(intervention_id, "intervention_id"):
                raise ValueError("code and intervention_id must identify the same intervention.")

        if effect is not None and not isinstance(effect, InterventionEffect):
            raise TypeError("effect must be an InterventionEffect instance.")
        if effect is not None and effect_model is not None:
            raise ValueError("provide effect or effect_model, not both.")
        if effect is None:
            if isinstance(effect_model, InterventionEffect):
                effect = effect_model
            elif effect_model is None:
                raise ValueError("an intervention requires an effect model.")
            else:
                effect = build_effect(
                    effect_model,
                    effect_parameters or {},
                    assumptions=assumptions,
                    applicable_covenant_classes=applicable_covenant_classes,
                )
        elif assumptions is not None or applicable_covenant_classes is not None:
            raise ValueError(
                "assumptions and applicable_covenant_classes belong to the effect model."
            )
        normalized_text = None if text is None else _bounded_text(text, "text", _MAX_TEXT_LENGTH)
        object.__setattr__(self, "code", normalized_code)
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "text", normalized_text)

    @property
    def model_type(self) -> EffectModelType:
        return self.effect.model_type

    @property
    def effect_model(self) -> EffectModelType:
        return self.effect.model_type

    @property
    def effect_parameters(self) -> Mapping[str, object]:
        return self.effect.effect_parameters

    @property
    def assumptions(self) -> tuple[str, ...]:
        return self.effect.assumptions

    @property
    def applicable_covenant_classes(self) -> frozenset[str]:
        return self.effect.applicable_covenant_classes

    def apply(
        self,
        inputs: ProjectionInputs,
        covenant_class: CovenantClass | str,
    ) -> ProjectionInputs:
        return self.effect.apply(inputs, covenant_class)


def build_effect(
    effect_model: EffectModelType | str,
    parameters: Mapping[str, object],
    *,
    assumptions: Iterable[str] | None,
    applicable_covenant_classes: Iterable[CovenantClass | str] | None,
) -> InterventionEffect:
    """Build one closed-set effect from catalogue-shaped data.

    The factory is the boundary for JSON/ORM-shaped configuration.  It
    rejects unknown parameters rather than ignoring them, which prevents a
    misspelled parameter from silently becoming a no-op intervention.
    """

    model = EffectModelType.from_value(effect_model)
    if not isinstance(parameters, Mapping):
        raise TypeError("effect_parameters must be a mapping.")
    if assumptions is None:
        raise ValueError("assumptions must be supplied for an effect model.")
    if applicable_covenant_classes is None:
        raise ValueError("applicable_covenant_classes must be supplied for an effect model.")
    if isinstance(assumptions, str | bytes | bytearray):
        raise TypeError("assumptions must be an iterable of statements, not text.")
    normalized_assumptions = tuple(assumptions)
    normalized_classes = normalize_covenant_classes(applicable_covenant_classes)
    if model is EffectModelType.LEVEL_SHIFT:
        values = _only_parameters(parameters, {"amount"}, model)
        return LevelShiftEffect(
            amount=cast(Decimal, values["amount"]),
            assumptions=normalized_assumptions,
            applicable_covenant_classes=normalized_classes,
        )
    if model is EffectModelType.RATE_CHANGE:
        values = _only_parameters(parameters, {"multiplier"}, model)
        return RateChangeEffect(
            multiplier=cast(Decimal, values["multiplier"]),
            assumptions=normalized_assumptions,
            applicable_covenant_classes=normalized_classes,
        )
    if model is EffectModelType.THRESHOLD_RELAXATION:
        values = _only_parameters(parameters, {"amount"}, model)
        return ThresholdRelaxationEffect(
            amount=cast(Decimal, values["amount"]),
            assumptions=normalized_assumptions,
            applicable_covenant_classes=normalized_classes,
        )
    if model is EffectModelType.PRESSURE_REDUCTION:
        values = _only_parameters(parameters, {"fraction"}, model)
        return PressureReductionEffect(
            fraction=cast(Decimal, values["fraction"]),
            assumptions=normalized_assumptions,
            applicable_covenant_classes=normalized_classes,
        )

    values = _only_parameters(parameters, {"components"}, model)
    raw_components = values["components"]
    if isinstance(raw_components, str | bytes | bytearray) or not isinstance(
        raw_components, Sequence
    ):
        raise TypeError("combination components must be a sequence of effect models.")
    components: list[InterventionEffect] = []
    for index, component in enumerate(raw_components):
        if not isinstance(component, InterventionEffect):
            raise TypeError(f"combination component {index} must be an InterventionEffect.")
        components.append(component)
    return CombinationEffect(
        components=tuple(components),
        assumptions=normalized_assumptions,
        applicable_covenant_classes=normalized_classes,
    )


def effect_from_value(
    effect_model: EffectModelType | str,
    parameters: Mapping[str, object],
    *,
    assumptions: Iterable[str] | None,
    applicable_covenant_classes: Iterable[CovenantClass | str] | None,
) -> InterventionEffect:
    """Descriptive alias for :func:`build_effect`."""

    return build_effect(
        effect_model,
        parameters,
        assumptions=assumptions,
        applicable_covenant_classes=applicable_covenant_classes,
    )


def _only_parameters(
    parameters: Mapping[str, object], expected: set[str], model: EffectModelType
) -> dict[str, object]:
    unknown = sorted(set(parameters) - expected)
    missing = sorted(expected - set(parameters))
    if unknown:
        raise ValueError(f"{model.value} has unknown parameter {unknown[0]!r}.")
    if missing:
        raise ValueError(f"{model.value} requires parameter {missing[0]!r}.")
    return {key: parameters[key] for key in expected}


def _set_common_fields(
    instance: InterventionEffect,
    assumptions: Iterable[str],
    applicable_covenant_classes: Iterable[CovenantClass | str],
) -> None:
    normalized_assumptions = _normalize_assumptions(assumptions)
    normalized_classes = normalize_covenant_classes(applicable_covenant_classes)
    object.__setattr__(instance, "assumptions", normalized_assumptions)
    object.__setattr__(instance, "applicable_covenant_classes", normalized_classes)


def _normalize_assumptions(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("assumptions must be an iterable of statements, not text.")
    try:
        normalized = tuple(_bounded_text(value, "assumption", _MAX_TEXT_LENGTH) for value in values)
    except TypeError as error:
        raise TypeError("assumptions must be an iterable of statements.") from error
    if not normalized:
        raise ValueError("assumptions must contain at least one non-blank assumption.")
    return _unique_assumptions(normalized)


def _unique_assumptions(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _replace_inputs(inputs: ProjectionInputs, **changes: object) -> ProjectionInputs:
    if set(changes) - {
        "current_value",
        "threshold",
        "per_day_drift",
        "pressure",
    }:
        raise ValueError("only supported projection input fields may be transformed.")
    if "current_value" in changes:
        return replace(inputs, current_value=cast(Decimal | None, changes["current_value"]))
    if "threshold" in changes:
        return replace(inputs, threshold=cast(Decimal, changes["threshold"]))
    if "per_day_drift" in changes:
        return replace(inputs, per_day_drift=cast(Decimal, changes["per_day_drift"]))
    if "pressure" in changes:
        return replace(inputs, pressure=cast(Decimal, changes["pressure"]))
    raise ValueError("at least one projection input field must be transformed.")


def _optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field_name)


def _bounded_decimal(value: object, field_name: str) -> Decimal:
    result = _decimal(value, field_name)
    if not -_MAX_PARAMETER_MAGNITUDE <= result <= _MAX_PARAMETER_MAGNITUDE:
        raise ValueError(
            f"{field_name} must be between {-_MAX_PARAMETER_MAGNITUDE} and "
            f"{_MAX_PARAMETER_MAGNITUDE} inclusive."
        )
    return result


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field_name} must be a finite Decimal.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite Decimal.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal.")
    return result


def _identifier(value: object, field_name: str) -> str:
    return _bounded_text(value, field_name, _MAX_CODE_LENGTH)


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains a control character.")
    return normalized


__all__ = [
    "Combination",
    "CombinationEffect",
    "CombinationModel",
    "COMBINATION_ORDER",
    "EffectModel",
    "EffectModelType",
    "EffectType",
    "InterventionEffect",
    "InterventionEffectModel",
    "InterventionFacts",
    "InterventionNotApplicable",
    "LevelShift",
    "LevelShiftEffect",
    "LevelShiftModel",
    "PressureReduction",
    "PressureReductionEffect",
    "PressureReductionModel",
    "ProjectionInput",
    "ProjectionInputs",
    "RateChange",
    "RateChangeEffect",
    "RateChangeModel",
    "ThresholdRelaxation",
    "ThresholdRelaxationEffect",
    "ThresholdRelaxationModel",
    "build_effect",
    "effect_from_value",
]
