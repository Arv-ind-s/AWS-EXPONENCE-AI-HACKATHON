"""`compute_ratio` — `plan.md §6`'s `C-30` (`T-027`).

Dispatches a `RatioDefinition` to its pure formula by code, wraps the
result as a `RatioResult`, and flags a plausible-band breach. Every path is
covered: a definition whose code the library does not implement raises
`UnknownDefinition`; every other case — missing line, zero or
sign-meaningless denominator, a value outside the plausible band — is
returned, never raised, because a not-computable ratio is a fact about the
statement, not a defect in the request. Since `T-030`, a not-computable
`RatioResult` always names one of `reasons.NotComputableReason`'s
enumerated members, never a free-text sentence, so the engine, the screens
and the memo (`spec §R-07.b`, `R-07.c`, `R-08.d`) read the one translated
sentence the catalogue renders for it.

No model is involved anywhere in this module or anything it imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.ratios.definitions import FacilityFacts, RatioDefinition
from covenant_radar.domain.ratios.library import FORMULAS
from covenant_radar.domain.ratios.reasons import NotComputableReason

__all__ = ["FacilityFacts", "RatioResult", "UnknownDefinition", "compute_ratio"]


class UnknownDefinition(ValidationError):
    """`compute_ratio` was asked to compute a definition code the library
    has no pure formula for."""

    code: ClassVar[str] = "unknown_ratio_definition"

    def __init__(self, definition_code: str) -> None:
        super().__init__(
            f"No ratio formula is registered for definition code {definition_code!r}.",
            field="definition.code",
        )
        self.definition_code = definition_code


@dataclass(frozen=True, slots=True)
class RatioResult:
    """The outcome of computing one `RatioDefinition` against one period's
    lines — `plan.md §6`'s `C-30` return shape.

    `inputs_used` carries every statement line the formula actually read,
    each with its value, whether or not the computation succeeded: the
    trace and the why-panel (`spec §17.6`) name their inputs from here, and
    an explanation that cannot name what it read is not one.

    `reason` is one of `reasons.NotComputableReason`'s enumerated members,
    never free text (`T-030`); `reason_context` carries the interpolation
    values — a line name, a denominator's value — the translation
    catalogue's one template for that reason needs to render it as a
    sentence, identically wherever it is shown.

    `band_breached` is `False` whenever the result is not computable, or
    the definition carries no plausible band — there is nothing to flag.

    `outcome` is the intrinsic pass/fail a condition-type definition
    (`domain/ratios/conditions.py`) carries alongside `value` — `None` for
    a plain ratio. Carrying it here, uniformly, on the one result type
    every definition returns, is what lets `C-32` evaluate a condition the
    same way it evaluates a threshold-compared ratio, without a special
    case for either.
    """

    code: str
    value: Decimal | None
    computable: bool
    reason: NotComputableReason | None
    inputs_used: Mapping[str, Decimal]
    band_breached: bool
    reason_context: Mapping[str, str] = field(default_factory=dict)
    outcome: bool | None = None

    def __post_init__(self) -> None:
        if self.computable:
            if self.value is None:
                raise ValueError("A computable RatioResult must carry a value.")
            if self.reason is not None:
                raise ValueError("A computable RatioResult must not carry a reason.")
            if self.reason_context:
                raise ValueError("A computable RatioResult must not carry reason context.")
        else:
            if self.value is not None:
                raise ValueError("A not-computable RatioResult must not carry a value.")
            if self.reason is None:
                raise ValueError("A not-computable RatioResult must name a reason.")
            if not isinstance(self.reason, NotComputableReason):
                raise TypeError(
                    "A RatioResult.reason must be a NotComputableReason member, "
                    f"not {type(self.reason).__name__}."
                )
            if self.band_breached:
                raise ValueError("A not-computable RatioResult cannot have a band breach.")
            if self.outcome is not None:
                raise ValueError("A not-computable RatioResult must not carry an outcome.")


def _band_breached(definition: RatioDefinition, value: Decimal | None) -> bool:
    if value is None:
        return False
    if definition.plausible_min is not None and value < definition.plausible_min:
        return True
    if definition.plausible_max is not None and value > definition.plausible_max:
        return True
    return False


def compute_ratio(
    definition: RatioDefinition,
    lines: Mapping[str, Decimal],
    facility: FacilityFacts | None,
    *,
    period_complete: bool = True,
) -> RatioResult:
    """Compute `definition` against `lines` (and `facility`, for the
    definitions `T-028` adds). Dispatches on `definition.code` against the
    library's own formula registry — never the caller's own notion of what
    that code means — so a `RatioDefinition` with an unregistered code is
    refused rather than silently skipped.

    `period_complete` is the caller's own `NormalisationResult.is_complete`
    (`domain/statements/chart.py`) — an identity that failed beyond
    tolerance makes the whole period an unsound basis for any ratio, so a
    caller passing `False` short-circuits before any formula runs, with
    `NotComputableReason.PERIOD_INCOMPLETE` rather than a value derived
    from data already known to be inconsistent.
    """
    formula = FORMULAS.get(definition.code)
    if formula is None:
        raise UnknownDefinition(definition.code)

    if not period_complete:
        return RatioResult(
            code=definition.code,
            value=None,
            computable=False,
            reason=NotComputableReason.PERIOD_INCOMPLETE,
            inputs_used={},
            band_breached=False,
        )

    outcome = formula(lines, facility)
    return RatioResult(
        code=definition.code,
        value=outcome.value,
        computable=outcome.computable,
        reason=outcome.reason,
        inputs_used=outcome.inputs_used,
        band_breached=_band_breached(definition, outcome.value),
        reason_context=outcome.reason_context,
        outcome=outcome.outcome,
    )
