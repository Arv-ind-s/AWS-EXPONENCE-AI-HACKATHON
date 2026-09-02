"""Applicability rules for intervention effect models.

Applicability is deliberately separate from the arithmetic in
``effects.py``.  An effect that produces a mathematically valid transform can
still be contractually meaningless for a covenant class.  The public
``require_applicable`` function is the fail-closed boundary used by callers
before applying a counterfactual.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from enum import StrEnum
from typing import ClassVar, Final, cast

from covenant_radar.core.errors import ValidationError

_MAX_CLASS_LENGTH: Final[int] = 100


class CovenantClass(StrEnum):
    """The broad covenant classes used by the default domain vocabulary.

    Persisted interventions may also name a concrete ratio definition or a
    bank-specific class.  The enum exists to prevent spelling drift for the
    standard classes; applicability itself remains open to configured
    classes so the domain does not need a database-backed registry.
    """

    LEVERAGE = "leverage"
    COVERAGE = "coverage"
    LIQUIDITY = "liquidity"
    CONDUCT = "conduct"
    WORKING_CAPITAL = "working_capital"
    NET_WORTH = "net_worth"
    CAPEX = "capex"
    CUSTOM = "custom"


class InterventionNotApplicable(ValidationError):
    """An otherwise valid intervention cannot apply to this covenant class."""

    code: ClassVar[str] = "intervention_not_applicable"

    def __init__(
        self,
        message: str,
        *,
        covenant_class: str | None = None,
        effect_model: str | None = None,
    ) -> None:
        super().__init__(message, field="intervention.applicable_covenant_classes")
        self.covenant_class = covenant_class
        self.effect_model = effect_model


# A concrete ratio can be used in an intervention catalogue in place of a
# broad class.  These aliases allow both forms to interoperate without making
# a configured intervention depend on the ratio library's import graph.
_CONCRETE_CLASS_ALIASES: Final[dict[str, str]] = {
    "leverage_ratio": CovenantClass.LEVERAGE.value,
    "tol_tnw": CovenantClass.LEVERAGE.value,
    "debt_to_ebitda": CovenantClass.LEVERAGE.value,
    "net_debt_to_ebitda": CovenantClass.LEVERAGE.value,
    "dscr": CovenantClass.COVERAGE.value,
    "interest_coverage_ratio": CovenantClass.COVERAGE.value,
    "fixed_charge_coverage_ratio": CovenantClass.COVERAGE.value,
    "asset_cover_ratio": CovenantClass.COVERAGE.value,
    "current_ratio": CovenantClass.LIQUIDITY.value,
    "quick_ratio": CovenantClass.LIQUIDITY.value,
    "minimum_liquidity": CovenantClass.LIQUIDITY.value,
    "drawing_power_headroom": CovenantClass.LIQUIDITY.value,
    "utilisation": CovenantClass.CONDUCT.value,
    "dividend_restriction": CovenantClass.CONDUCT.value,
    "promoter_shareholding_floor": CovenantClass.NET_WORTH.value,
    "tnw_floor": CovenantClass.NET_WORTH.value,
    "minimum_net_worth": CovenantClass.NET_WORTH.value,
    "receivable_days": CovenantClass.WORKING_CAPITAL.value,
    "inventory_days": CovenantClass.WORKING_CAPITAL.value,
    "payable_days": CovenantClass.WORKING_CAPITAL.value,
    "cash_conversion_cycle": CovenantClass.WORKING_CAPITAL.value,
    "working_capital_gap": CovenantClass.WORKING_CAPITAL.value,
    "maximum_capex": CovenantClass.CAPEX.value,
}

_WILDCARDS: Final[frozenset[str]] = frozenset({"*", "all", "any"})


def normalize_covenant_class(value: CovenantClass | str) -> str:
    """Return a bounded, case-insensitive covenant-class identifier."""

    if isinstance(value, CovenantClass):
        return value.value
    if not isinstance(value, str):
        raise TypeError("covenant_class must be text or a CovenantClass.")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("covenant_class must not be blank.")
    if len(normalized) > _MAX_CLASS_LENGTH:
        raise ValueError(f"covenant_class must be at most {_MAX_CLASS_LENGTH} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("covenant_class contains a control character.")
    return normalized


def normalize_covenant_classes(
    values: Iterable[CovenantClass | str],
    *,
    field_name: str = "applicable_covenant_classes",
) -> frozenset[str]:
    """Normalize and validate a non-empty collection of covenant classes."""

    if isinstance(values, str | bytes | bytearray):
        raise TypeError(f"{field_name} must be an iterable of class names, not text.")
    try:
        normalized = frozenset(normalize_covenant_class(value) for value in values)
    except TypeError as error:
        raise TypeError(f"{field_name} must be an iterable of class names.") from error
    if not normalized:
        raise ValueError(f"{field_name} must contain at least one covenant class.")
    return normalized


def _class_family(value: str) -> str:
    return _CONCRETE_CLASS_ALIASES.get(value, value)


def is_applicable(
    effect_or_classes: object,
    covenant_class: CovenantClass | str,
) -> bool:
    """Return whether an effect or class collection applies to a class.

    Both exact configured class identifiers and the standard concrete-ratio
    aliases are considered.  A wildcard is supported for an explicitly
    configured global action; it is never introduced by this module itself.
    """

    requested = normalize_covenant_class(covenant_class)
    if isinstance(effect_or_classes, str | bytes | bytearray):
        classes = normalize_covenant_classes(
            cast(Iterable[CovenantClass | str], (effect_or_classes,))
        )
    elif isinstance(effect_or_classes, Collection):
        classes = normalize_covenant_classes(
            cast(Collection[CovenantClass | str], effect_or_classes)
        )
    else:
        raw_classes = getattr(effect_or_classes, "applicable_covenant_classes", None)
        if raw_classes is None:
            raise TypeError(
                "effect_or_classes must expose applicable_covenant_classes "
                "or be a collection of class names."
            )
        classes = normalize_covenant_classes(cast(Iterable[CovenantClass | str], raw_classes))

    if classes & _WILDCARDS:
        return True
    requested_family = _class_family(requested)
    return any(
        configured == requested
        or configured == requested_family
        or _class_family(configured) == requested_family
        for configured in classes
    )


def require_applicable(
    effect_or_classes: object,
    covenant_class: CovenantClass | str,
    *,
    effect_model: str | None = None,
) -> None:
    """Raise ``InterventionNotApplicable`` unless the effect fits the class."""

    requested = normalize_covenant_class(covenant_class)
    if is_applicable(effect_or_classes, requested):
        return

    raw_classes = getattr(effect_or_classes, "applicable_covenant_classes", effect_or_classes)
    classes = normalize_covenant_classes(cast(Iterable[CovenantClass | str], raw_classes))
    supported = ", ".join(sorted(classes))
    model = effect_model or getattr(effect_or_classes, "model_type", "intervention")
    model_text = getattr(model, "value", model)
    raise InterventionNotApplicable(
        f"{model_text!r} is not applicable to covenant class {requested!r}; "
        f"applicable classes: {supported}.",
        covenant_class=requested,
        effect_model=str(model_text),
    )


# Descriptive aliases make the read-side API natural for service callers.
applicable = is_applicable
check_applicability = require_applicable


__all__ = [
    "CovenantClass",
    "InterventionNotApplicable",
    "applicable",
    "check_applicability",
    "is_applicable",
    "normalize_covenant_class",
    "normalize_covenant_classes",
    "require_applicable",
]
