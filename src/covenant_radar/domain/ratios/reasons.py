"""The enumerated not-computable reason vocabulary the ratio library, the
covenant engine and every screen share (`spec §R-07.b`, `R-07.c`, `R-08.d`,
`T-030`).

Before this module, a formula that could not produce a value built its own
English sentence inline (`f"{label} is zero"`, `f"missing required
statement line(s): {names}"`) — a different sentence, phrased slightly
differently, wherever it happened to be written. Two auditors reading two
of those sentences for what is really the same condition reasonably ask
which is right.

`NotComputableReason` is the fixed, closed set every formula reports
instead of composed prose: a stable machine code, never assembled text.
The one sentence each code renders as lives in the translation catalogue
(`covenant_radar.i18n`, keyed by `TRANSLATION_KEYS`), so the engine, the
screens and the memo all read the same template; the per-instance detail a
reason needs (which line, which denominator, what value) travels beside it
as `reason_context` — the placeholders that template fills — never baked
into a string the domain layer hands back.
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class NotComputableReason(str, Enum):
    """Every way a `FormulaOutcome`/`RatioResult` can fail to produce a
    value — closed and exhaustive by design (`T-030`). A formula that
    discovers a new failure mode extends this enum, here, rather than
    composing a fresh sentence at the call site.
    """

    #: One or more statement lines the formula reads are absent from `lines`.
    MISSING_LINE = "missing_line"
    #: The formula's denominator summed to exactly zero.
    ZERO_DENOMINATOR = "zero_denominator"
    #: The formula's denominator is negative, so the ratio it would
    #: produce carries no meaning (e.g. a negative net worth as a leverage
    #: denominator).
    SIGN_MEANINGLESS_DENOMINATOR = "sign_meaningless_denominator"
    #: The statement period itself failed a balance-sheet or profit-and-loss
    #: identity beyond tolerance (`domain.statements.identities`) and so is
    #: not a sound basis for any ratio computed from it.
    PERIOD_INCOMPLETE = "period_incomplete"
    #: One or more `FacilityFacts` fields the formula reads are absent.
    FACILITY_FACTS_ABSENT = "facility_facts_absent"
    #: The formula cannot produce a value for a reason none of the above
    #: names — reserved for a bank-defined custom formula (`T-031`/`T-032`);
    #: no definition in the built-in library returns it today.
    FORMULA_NOT_COMPUTABLE = "formula_not_computable"


#: The translation-catalogue key each reason renders through
#: (`covenant_radar.i18n`'s default catalogue carries the English entry for
#: every one; `test_every_reason_has_a_translation` fails the build the day
#: it does not).
TRANSLATION_KEYS: Final[dict[NotComputableReason, str]] = {
    reason: f"ratio.reason.{reason.value}" for reason in NotComputableReason
}


def translation_key(reason: NotComputableReason) -> str:
    """Return the catalogue key `reason` renders through."""
    return TRANSLATION_KEYS[reason]


__all__ = ["NotComputableReason", "TRANSLATION_KEYS", "translation_key"]
