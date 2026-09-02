"""The two condition-type definitions of `spec §R-07`'s twenty-four
(`plan.md §6`'s `C-30`, `T-028`).

A covenant condition differs from a plain ratio only in what its result
*means*: alongside the same comparable `value` every ratio returns, a
condition's formula also carries an intrinsic pass/fail
`FormulaOutcome.outcome`, computed by the formula itself rather than by a
covenant's own threshold — the covenant engine (`C-32`) reads `outcome`
directly for these two, exactly the way it reads a threshold comparison for
every other definition, with no special case in between.

Kept in this file, distinct from `definitions.py`, precisely because that
meaning is different: a reviewer scanning this module sees every place the
library hands back a boolean, without having to check each of the other
twenty-two for one first.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from covenant_radar.domain.ratios.definitions import (
    FacilityFacts,
    FormulaOutcome,
    RatioDefinition,
    RatioEntry,
)
from covenant_radar.domain.ratios.reasons import NotComputableReason

_ZERO: Final[Decimal] = Decimal(0)
_PERCENTAGE_MIN: Final[Decimal] = Decimal("0")
_PERCENTAGE_MAX: Final[Decimal] = Decimal("100")


def _compute_dividend_restriction(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """No dividend may be paid without lender consent — a self-contained
    condition needing no external threshold: the covenant is that
    `dividend_paid` is zero, so the pass/fail outcome is a zero test on the
    same value a bank would want to see, not a comparison this module
    invents a reference for."""
    code = "dividend_paid"
    if code not in lines:
        return FormulaOutcome(
            value=None,
            computable=False,
            reason=NotComputableReason.MISSING_LINE,
            inputs_used={},
            reason_context={"names": code},
        )
    value = lines[code]
    return FormulaOutcome(
        value=value,
        computable=True,
        reason=None,
        inputs_used={code: value},
        outcome=value == _ZERO,
    )


def _compute_promoter_shareholding_floor(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """Promoter shareholding must not fall below the floor a facility's own
    terms fix — read from `FacilityFacts.promoter_shareholding_floor_pct`,
    a fact about the deal, the same way `sanctioned_limit` is, rather than
    a constant this module would otherwise have to invent and apply to
    every borrower alike. `value` is the measured shareholding a covenant
    threshold can still be tested against; `outcome` is whether this
    specific facility's own floor was met."""
    missing_lines = [name for name in ("promoter_shareholding",) if name not in lines]
    inputs_used = {name: lines[name] for name in ("promoter_shareholding",) if name in lines}
    if missing_lines:
        return FormulaOutcome(
            value=None,
            computable=False,
            reason=NotComputableReason.MISSING_LINE,
            inputs_used=inputs_used,
            reason_context={"names": ", ".join(missing_lines)},
        )
    missing_facts = [
        name
        for name in ("promoter_shareholding_floor_pct",)
        if facility is None or getattr(facility, name) is None
    ]
    if missing_facts:
        return FormulaOutcome(
            value=None,
            computable=False,
            reason=NotComputableReason.FACILITY_FACTS_ABSENT,
            inputs_used=inputs_used,
            reason_context={"names": ", ".join(missing_facts)},
        )
    assert facility is not None  # narrowed by the `missing_facts` check above
    floor_pct = facility.promoter_shareholding_floor_pct
    assert floor_pct is not None
    value = lines["promoter_shareholding"]
    inputs_used = {
        "promoter_shareholding": value,
        "facility.promoter_shareholding_floor_pct": floor_pct,
    }
    return FormulaOutcome(
        value=value,
        computable=True,
        reason=None,
        inputs_used=inputs_used,
        outcome=value >= floor_pct,
    )


ENTRIES: Final[tuple[RatioEntry, ...]] = (
    RatioEntry(
        definition=RatioDefinition(
            code="dividend_restriction",
            name="Dividend restriction",
            formula_text="dividend_paid",
            required_lines=("dividend_paid",),
            unit="₹ crore",
            plausible_min=_PERCENTAGE_MIN,
            plausible_max=None,
            direction_hint="max",
            kind="condition",
        ),
        formula=_compute_dividend_restriction,
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="promoter_shareholding_floor",
            name="Promoter shareholding floor",
            formula_text="promoter_shareholding",
            required_lines=("promoter_shareholding", "promoter_shareholding_floor_pct"),
            unit="%",
            plausible_min=_PERCENTAGE_MIN,
            plausible_max=_PERCENTAGE_MAX,
            direction_hint="min",
            kind="condition",
        ),
        formula=_compute_promoter_shareholding_floor,
    ),
)


__all__ = ["ENTRIES"]
