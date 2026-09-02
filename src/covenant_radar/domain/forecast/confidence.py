"""Confidence and probability-display guard for forecast results (``C-37``).

Confidence is deliberately separate from breach probability.  The forecast
can calculate a deterministic probability, but that number is not shown when
the supporting data is insufficient.  This module computes the confidence
product and returns the display decision together with every factor that led
to it.

The three factors are:

* ``completeness``: the fraction of required periods that are complete and
  usable;
* ``evidence_support``: the fraction of the forecast's supporting evidence
  that passed the evidence-stage support checks; and
* ``staleness_factor``: ``1 / (1 + staleness_days)``.  Fresh data therefore
  contributes one, while increasingly stale data can only reduce confidence.

The product is clamped to ``[0, 1]`` after validation.  T2 is inclusive:
confidence equal to the configured floor is shown.  A zero completeness
factor is a stronger suppression condition than the floor and always marks
the probability absent, including when a caller supplies a zero floor.

The domain accepts a small T2-shaped configuration object or mapping and does
not import the configuration adapter.  When no threshold is supplied, the
standalone default matches the shipped T2 value; services should pass the
active threshold snapshot so the decision is replayable against that run's
configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Final

_T2_NAME: Final[str] = "T2"
_CONFIDENCE_FLOOR_FIELD: Final[str] = "confidence_floor"
_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_DEFAULT_CONFIDENCE_FLOOR: Final[Decimal] = Decimal("0.50")
_MAPPING_VERSION: Final[str] = "forecast.confidence.v1"
_FACTOR_ORDER: Final[tuple[str, ...]] = (
    "completeness",
    "evidence_support",
    "staleness",
)

DEFAULT_CONFIDENCE_FLOOR: Final[Decimal] = _DEFAULT_CONFIDENCE_FLOOR


@dataclass(frozen=True, slots=True)
class ConfidenceThresholds:
    """Validated T2 configuration for the probability display guard."""

    confidence_floor: Decimal

    def __post_init__(self) -> None:
        floor = _decimal(self.confidence_floor, _CONFIDENCE_FLOOR_FIELD)
        if not _ZERO <= floor <= _ONE:
            raise ValueError("confidence_floor must be between zero and one inclusive.")
        object.__setattr__(self, "confidence_floor", floor)

    @classmethod
    def from_store(cls, store: object) -> ConfidenceThresholds:
        """Read T2 from a threshold store or adapter-neutral mapping.

        Supported shapes are ``ThresholdStore.get('T2')``,
        ``{'T2': {'confidence_floor': Decimal(...)}}`` and a direct T2 field
        mapping/object.  Missing or malformed configuration is rejected
        rather than silently replaced with a policy value.
        """

        if isinstance(store, cls):
            return store
        section = _threshold_section(store)
        value = _read(section, _CONFIDENCE_FLOOR_FIELD)
        return cls(value)


@dataclass(frozen=True, slots=True)
class ConfidenceFactor:
    """One named confidence factor retained for the explainability trace."""

    name: str
    value: Decimal
    description: str

    def __post_init__(self) -> None:
        if self.name not in _FACTOR_ORDER:
            raise ValueError(f"Unknown confidence factor {self.name!r}.")
        value = _decimal(self.value, f"{self.name} factor")
        if not _ZERO <= value <= _ONE:
            raise ValueError(f"{self.name} factor must be between zero and one.")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError(f"{self.name} factor description must not be blank.")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    """Confidence product and the decision that guards probability display."""

    confidence: Decimal
    raw_product: Decimal
    completeness: Decimal
    evidence_support: Decimal
    staleness_days: int
    completeness_factor: Decimal
    evidence_support_factor: Decimal
    staleness_factor: Decimal
    confidence_floor: Decimal
    below_confidence_floor: bool
    probability_suppressed: bool
    limiting_factor: str
    limiting_value: Decimal
    reason: str
    factors: tuple[ConfidenceFactor, ...]
    formula_inputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        confidence_value = _decimal(self.confidence, "confidence")
        raw_product = _decimal(self.raw_product, "raw_product")
        floor = _decimal(self.confidence_floor, _CONFIDENCE_FLOOR_FIELD)
        completeness = _fraction(self.completeness, "completeness")
        evidence_support = _fraction(self.evidence_support, "evidence_support")
        completeness_factor = _fraction(self.completeness_factor, "completeness factor")
        evidence_support_factor = _fraction(
            self.evidence_support_factor,
            "evidence_support factor",
        )
        staleness_factor = _fraction(self.staleness_factor, "staleness factor")
        if not _ZERO <= confidence_value <= _ONE:
            raise ValueError("confidence must be between zero and one.")
        if raw_product < _ZERO:
            raise ValueError("raw_product must be non-negative.")
        if not _ZERO <= floor <= _ONE:
            raise ValueError("confidence_floor must be between zero and one inclusive.")
        if not isinstance(self.staleness_days, int) or isinstance(self.staleness_days, bool):
            raise TypeError("staleness_days must be a non-negative integer.")
        if self.staleness_days < 0:
            raise ValueError("staleness_days must be non-negative.")
        expected_staleness_factor = _ONE / (_ONE + Decimal(self.staleness_days))
        if staleness_factor != expected_staleness_factor:
            raise ValueError("staleness_factor must equal 1 / (1 + staleness_days).")
        if completeness_factor != completeness:
            raise ValueError("completeness_factor must match completeness.")
        if evidence_support_factor != evidence_support:
            raise ValueError("evidence_support_factor must match evidence_support.")
        expected_product = completeness_factor * evidence_support_factor * staleness_factor
        if raw_product != expected_product:
            raise ValueError("raw_product must match the product of the confidence factors.")
        expected_confidence = min(_ONE, max(_ZERO, raw_product))
        if confidence_value != expected_confidence:
            raise ValueError("confidence must match the clamped raw_product.")
        expected_below_floor = confidence_value < floor
        if self.below_confidence_floor != expected_below_floor:
            raise ValueError("below_confidence_floor must match the inclusive T2 comparison.")
        expected_suppressed = expected_below_floor or completeness == _ZERO
        if self.probability_suppressed != expected_suppressed:
            raise ValueError("probability_suppressed must match confidence and completeness.")
        if self.limiting_factor not in (*_FACTOR_ORDER, "none"):
            raise ValueError(f"Unknown limiting factor {self.limiting_factor!r}.")
        limiting_value = _fraction(self.limiting_value, "limiting_value")
        if not isinstance(self.below_confidence_floor, bool):
            raise TypeError("below_confidence_floor must be a boolean.")
        if not isinstance(self.probability_suppressed, bool):
            raise TypeError("probability_suppressed must be a boolean.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Confidence results must carry a reason.")
        if not all(isinstance(factor, ConfidenceFactor) for factor in self.factors):
            raise TypeError("factors must contain ConfidenceFactor values.")
        if len(self.factors) != len(_FACTOR_ORDER) or {
            factor.name for factor in self.factors
        } != set(_FACTOR_ORDER):
            raise ValueError("ConfidenceResult must contain all three named factors.")
        expected_factors = {
            "completeness": completeness_factor,
            "evidence_support": evidence_support_factor,
            "staleness": staleness_factor,
        }
        actual_factors = {factor.name: factor.value for factor in self.factors}
        if actual_factors != expected_factors:
            raise ValueError("Confidence factors must match the result factor fields.")
        expected_limiting_factor, expected_limiting_value = _limiting_factor(self.factors)
        if (self.limiting_factor, limiting_value) != (
            expected_limiting_factor,
            expected_limiting_value,
        ):
            raise ValueError("limiting_factor must identify the smallest confidence factor.")
        if not isinstance(self.formula_inputs, Mapping):
            raise TypeError("formula_inputs must be a mapping.")
        object.__setattr__(self, "confidence", confidence_value)
        object.__setattr__(self, "raw_product", raw_product)
        object.__setattr__(self, "completeness", completeness)
        object.__setattr__(self, "evidence_support", evidence_support)
        object.__setattr__(self, "completeness_factor", completeness_factor)
        object.__setattr__(self, "evidence_support_factor", evidence_support_factor)
        object.__setattr__(self, "staleness_factor", staleness_factor)
        object.__setattr__(self, "confidence_floor", floor)
        object.__setattr__(self, "limiting_value", limiting_value)
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(self, "formula_inputs", _freeze(self.formula_inputs))

    @property
    def value(self) -> Decimal:
        """Return the confidence value for generic scoring consumers."""

        return self.confidence

    @property
    def raw_score(self) -> Decimal:
        """Compatibility spelling for the un-clamped product."""

        return self.raw_product

    @property
    def shown(self) -> bool:
        """Whether a probability is permitted to appear on a surface."""

        return not self.probability_suppressed

    @property
    def probability_absent(self) -> bool:
        """Return the explicit absence state consumed by forecast surfaces."""

        return self.probability_suppressed

    @property
    def factors_by_name(self) -> Mapping[str, ConfidenceFactor]:
        """Return immutable name-based access to all confidence factors."""

        return MappingProxyType({factor.name: factor for factor in self.factors})

    @property
    def limiting_factor_name(self) -> str:
        """Compatibility spelling for clients that avoid enum-like fields."""

        return self.limiting_factor


def confidence(
    completeness: Decimal,
    evidence_support: Decimal,
    staleness_days: int,
    thresholds: object | None = None,
    *,
    confidence_floor: Decimal | None = None,
) -> ConfidenceResult:
    """Compute confidence and decide whether a probability may be shown.

    The first three parameters are the C-37 contract.  ``thresholds`` may be
    supplied positionally as a T2 store/mapping, or ``confidence_floor`` may
    be supplied directly by an adapter that has already resolved T2.  Supplying
    both is rejected to prevent an ambiguous display decision.  If neither is
    supplied, the default is the shipped T2 floor and callers should use the
    configured form for persisted scoring runs.
    """

    completeness_value = _fraction(completeness, "completeness")
    support_value = _fraction(evidence_support, "evidence_support")
    staleness = _staleness_days(staleness_days)
    configured = _resolve_thresholds(thresholds, confidence_floor)

    staleness_factor = _ONE / (_ONE + Decimal(staleness))
    raw_product = completeness_value * support_value * staleness_factor
    result_confidence = min(_ONE, max(_ZERO, raw_product))
    below_floor = result_confidence < configured.confidence_floor
    no_complete_periods = completeness_value == _ZERO
    probability_suppressed = below_floor or no_complete_periods

    factors = (
        ConfidenceFactor(
            "completeness",
            completeness_value,
            "fraction of required periods that are complete and usable",
        ),
        ConfidenceFactor(
            "evidence_support",
            support_value,
            "fraction of supporting evidence that passed support checks",
        ),
        ConfidenceFactor(
            "staleness",
            staleness_factor,
            "reciprocal freshness factor 1 / (1 + staleness_days)",
        ),
    )
    limiting_factor, limiting_value = _limiting_factor(factors)
    reason = _reason(
        completeness=completeness_value,
        confidence=result_confidence,
        floor=configured.confidence_floor,
        below_floor=below_floor,
        probability_suppressed=probability_suppressed,
        limiting_factor=limiting_factor,
    )
    formula_inputs = {
        "mapping_version": _MAPPING_VERSION,
        "completeness": completeness_value,
        "evidence_support": support_value,
        "staleness_days": staleness,
        "completeness_factor": completeness_value,
        "evidence_support_factor": support_value,
        "staleness_factor": staleness_factor,
        "confidence_floor": configured.confidence_floor,
        "raw_product": raw_product,
        "confidence": result_confidence,
        "below_confidence_floor": below_floor,
        "probability_suppressed": probability_suppressed,
        "limiting_factor": limiting_factor,
        "limiting_value": limiting_value,
        "reason": reason,
        "factors": {
            factor.name: {
                "value": factor.value,
                "description": factor.description,
            }
            for factor in factors
        },
    }
    return ConfidenceResult(
        confidence=result_confidence,
        raw_product=raw_product,
        completeness=completeness_value,
        evidence_support=support_value,
        staleness_days=staleness,
        completeness_factor=completeness_value,
        evidence_support_factor=support_value,
        staleness_factor=staleness_factor,
        confidence_floor=configured.confidence_floor,
        below_confidence_floor=below_floor,
        probability_suppressed=probability_suppressed,
        limiting_factor=limiting_factor,
        limiting_value=limiting_value,
        reason=reason,
        factors=factors,
        formula_inputs=formula_inputs,
    )


def confidence_value(
    completeness: Decimal,
    evidence_support: Decimal,
    staleness_days: int,
    thresholds: object | None = None,
    *,
    confidence_floor: Decimal | None = None,
) -> Decimal:
    """Return only the Decimal confidence for persistence-facing adapters."""

    return confidence(
        completeness,
        evidence_support,
        staleness_days,
        thresholds,
        confidence_floor=confidence_floor,
    ).confidence


def _resolve_thresholds(
    thresholds: object | None,
    confidence_floor: Decimal | None,
) -> ConfidenceThresholds:
    if thresholds is not None and confidence_floor is not None:
        raise ValueError("Provide thresholds or confidence_floor, not both.")
    if confidence_floor is not None:
        return ConfidenceThresholds(confidence_floor)
    if thresholds is not None:
        return ConfidenceThresholds.from_store(thresholds)
    return ConfidenceThresholds(_DEFAULT_CONFIDENCE_FLOOR)


def _limiting_factor(
    factors: tuple[ConfidenceFactor, ...],
) -> tuple[str, Decimal]:
    minimum = min(factor.value for factor in factors)
    if minimum == _ONE:
        return "none", _ONE
    for factor in factors:
        if factor.value == minimum:
            return factor.name, minimum
    raise RuntimeError("Confidence factors must contain a limiting factor.")


def _reason(
    *,
    completeness: Decimal,
    confidence: Decimal,
    floor: Decimal,
    below_floor: bool,
    probability_suppressed: bool,
    limiting_factor: str,
) -> str:
    if completeness == _ZERO:
        return "no complete periods available; confidence is zero and probability is absent"
    if probability_suppressed:
        if below_floor:
            return (
                f"confidence {confidence} is below the T2 floor {floor}; "
                f"probability is absent because {limiting_factor} is the limiting factor"
            )
        return "probability is absent because no complete periods are available"
    if limiting_factor == "none":
        return "all confidence factors are at their maximum; probability may be shown"
    return f"confidence meets the inclusive T2 floor; {limiting_factor} is the limiting factor"


def _threshold_section(store: object) -> object:
    if isinstance(store, ConfidenceThresholds):
        return {_CONFIDENCE_FLOOR_FIELD: store.confidence_floor}
    if isinstance(store, Mapping):
        if _T2_NAME in store:
            return store[_T2_NAME]
        return store
    getter = getattr(store, "get", None)
    if callable(getter):
        try:
            section = getter(_T2_NAME)
        except (KeyError, TypeError):
            section = None
        if section is not None:
            return section
    for name in (_T2_NAME, "t2", "confidence"):
        section = getattr(store, name, None)
        if section is not None:
            return section
    raise ValueError("T2 threshold store is missing.")


def _read(section: object, field_name: str) -> Decimal:
    if isinstance(section, Mapping):
        if field_name not in section:
            raise ValueError(f"T2 threshold is missing {field_name!r}.")
        value = section[field_name]
    else:
        marker = object()
        value = getattr(section, field_name, marker)
        if value is marker:
            raise ValueError(f"T2 threshold is missing {field_name!r}.")
    if not isinstance(value, Decimal):
        raise TypeError(f"T2 threshold {field_name!r} must be a Decimal.")
    return value


def _fraction(value: object, field_name: str) -> Decimal:
    result = _decimal(value, field_name)
    if not _ZERO <= result <= _ONE:
        raise ValueError(f"{field_name} must be between zero and one inclusive.")
    return result


def _staleness_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("staleness_days must be a non-negative integer.")
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
    "ConfidenceFactor",
    "ConfidenceResult",
    "ConfidenceThresholds",
    "DEFAULT_CONFIDENCE_FLOOR",
    "confidence",
    "confidence_value",
]
