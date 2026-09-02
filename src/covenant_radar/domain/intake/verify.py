"""The six code verifications, failing closed (`spec §R-06`, `plan.md §8`'s
`T-095`).

`domain/intake/proposal.py` (`T-094`) parses and normalises what the model
proposed; it decides nothing. This module is the disproof: six independent,
deterministic checks that either reproduce the model's claim from this
borrower's own stored facts or refuse it. No model call happens here, and no
covenant is ever built here — this module's only output is a
:class:`VerificationReport` naming what passed and what did not; a caller
(`services/intake.py`, `T-096`) decides what to do with that report.

The six checks (`spec §R-06`):

1. schema validity — the stage-1 reply matched its declared output shape
   (`T-094`'s own strict parse, consumed here as ``proposal.parseable``);
2. the proposed definition is a known ratio-library entry, or a
   syntactically valid custom formula, and not both or neither;
3. that definition is **actually recomputable against this borrower's own
   stored statements** — the check this module exists for, and the one no
   competitor was found to do: a proposal can name a real ratio and still
   fail here because the statement this borrower actually filed is missing
   the line the ratio needs;
4. the threshold falls within the named definition's plausible band;
5. the proposed unit and currency agree with what the definition and the
   facility actually use;
6. the testing frequency is unambiguous and the effective date is allowed
   and consistent with the facility's own sanction date.

All six always run, and all six results are always collected — never
short-circuited at the first failure — so a reviewer sees every reason a
proposal was refused in one pass rather than one at a time across several
correction attempts. ``VerificationReport.all_passed`` is ``True`` only when
every one of the six passed.

Every failure names its check, by a closed machine code (`C-06`'s
``failed_checks[]``), and carries a human-readable detail — never a bare
boolean a reviewer has to interpret.

This module has no I/O and no dependency on any ORM model, the web layer or
a model provider: everything it needs about the borrower, the facility and
the statement period is assembled by the caller into a
:class:`VerificationContext` first. It also never imports
``covenant_radar.ai`` — the domain-purity import-linter contract forbids
it — so the one stage-1 concern that *is* about the model boundary, whether
the clause text itself was shaped like an attempt to redirect the model
(`spec §R-06.c`), is not this module's job: `ai/shapes.py` owns that check
and combines it with this module's report, because only that layer may
depend on both.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Final

from covenant_radar.domain.covenants.model import is_valid_custom_formula
from covenant_radar.domain.intake.proposal import StageOneProposal
from covenant_radar.domain.ratios.compute import RatioResult, UnknownDefinition, compute_ratio
from covenant_radar.domain.ratios.definitions import FacilityFacts, RatioDefinition
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason

__all__ = [
    "CheckOutcome",
    "VerificationCheckName",
    "VerificationContext",
    "VerificationReport",
    "verify_proposal",
]

#: `RatioDefinition.unit` (the library's own vocabulary) mapped to
#: `domain.intake.proposal.UNIT_KINDS` (the stage-1 prompt's own, deliberately
#: separate, vocabulary) — the correspondence check 5 tests a proposal
#: against. Every unit the library actually uses today is covered; a
#: definition added later with an uncovered unit fails check 5 rather than
#: silently passing, which is the point of keeping this closed rather than a
#: fallback guess.
_DEFINITION_UNIT_TO_PROPOSAL_UNIT: Final[Mapping[str, str]] = {
    "x": "ratio",
    "%": "percent",
    "₹ crore": "currency",
    "days": "days",
}


class VerificationCheckName(str, Enum):
    """The closed set of six check names — `C-06`'s ``failed_checks[]``
    vocabulary, stable across a release the way `NotComputableReason` is."""

    SCHEMA_VALID = "schema_valid"
    DEFINITION_KNOWN = "definition_known"
    RECOMPUTABLE = "recomputable"
    THRESHOLD_PLAUSIBLE = "threshold_plausible"
    UNIT_CURRENCY_CONSISTENT = "unit_currency_consistent"
    FREQUENCY_DATES_CONSISTENT = "frequency_dates_consistent"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One check's verdict: which check, whether it passed, and why —
    never a bare boolean, since a refused proposal must be explainable
    without re-deriving the check by hand."""

    check: VerificationCheckName
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.check, VerificationCheckName):
            raise TypeError("CheckOutcome.check must be a VerificationCheckName.")
        if not isinstance(self.passed, bool):
            raise TypeError("CheckOutcome.passed must be a boolean.")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("CheckOutcome.detail must be non-empty text.")


@dataclass(frozen=True, slots=True)
class VerificationContext:
    """Everything :func:`verify_proposal` needs about this borrower's
    stored statements and this facility, assembled by the caller from
    already-loaded rows — this module performs no I/O of its own.

    ``statement_lines`` and ``period_complete`` are exactly
    `domain.statements.chart.NormalisationResult.lines`/``is_complete`` for
    the period the covenant would be tested against: the same shape
    `compute_ratio` (`C-30`) already consumes, so check 3 recomputes a
    proposal exactly the way the covenant engine will later test it, not
    through a second, potentially divergent, path.
    """

    statement_lines: Mapping[str, Decimal]
    period_complete: bool
    facility_facts: FacilityFacts
    facility_sanction_date: date
    facility_currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.statement_lines, Mapping):
            raise TypeError("VerificationContext.statement_lines must be a mapping.")
        for code, value in self.statement_lines.items():
            if not isinstance(code, str) or not code:
                raise ValueError("VerificationContext.statement_lines keys must be line codes.")
            if not isinstance(value, Decimal):
                raise TypeError(
                    f"VerificationContext.statement_lines[{code!r}] must be a Decimal."
                )
        object.__setattr__(self, "statement_lines", dict(self.statement_lines))
        if not isinstance(self.period_complete, bool):
            raise TypeError("VerificationContext.period_complete must be a boolean.")
        if not isinstance(self.facility_facts, FacilityFacts):
            raise TypeError("VerificationContext.facility_facts must be a FacilityFacts.")
        if isinstance(self.facility_sanction_date, datetime) or not isinstance(
            self.facility_sanction_date, date
        ):
            raise TypeError("VerificationContext.facility_sanction_date must be a calendar date.")
        if not isinstance(self.facility_currency, str) or not self.facility_currency.strip():
            raise ValueError("VerificationContext.facility_currency must be non-empty text.")
        object.__setattr__(self, "facility_currency", self.facility_currency.strip().upper())


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """The outcome of running all six checks against one stage-1 proposal.

    Always carries exactly six results, one per :class:`VerificationCheckName`
    member, in the order the enum declares them — `test_all_six_run_and_collect`
    depends on this never being fewer, regardless of how early a check could
    have told the whole story on its own.
    """

    checks: tuple[CheckOutcome, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))
        seen = tuple(outcome.check for outcome in self.checks)
        expected = tuple(VerificationCheckName)
        if seen != expected:
            raise ValueError(
                "VerificationReport.checks must carry exactly the six checks, once each, "
                "in VerificationCheckName order."
            )

    @property
    def all_passed(self) -> bool:
        """Whether every one of the six checks passed."""
        return all(outcome.passed for outcome in self.checks)

    @property
    def failed_checks(self) -> tuple[str, ...]:
        """The machine-readable names of every failed check — `C-06`'s
        ``failed_checks[]``, in check-declaration order."""
        return tuple(outcome.check.value for outcome in self.checks if not outcome.passed)

    def detail_for(self, check: VerificationCheckName) -> str:
        """The human-readable detail one named check produced."""
        for outcome in self.checks:
            if outcome.check is check:
                return outcome.detail
        raise KeyError(check)  # pragma: no cover - unreachable given __post_init__


@dataclass(frozen=True, slots=True)
class _ResolvedDefinition:
    """What check 2 resolved, for checks 3-5 to consume without re-deriving
    it: a known library definition, a syntactically valid custom formula
    naming no library definition, or neither."""

    definition: RatioDefinition | None
    is_custom_formula: bool


def verify_proposal(proposal: StageOneProposal, context: VerificationContext) -> VerificationReport:
    """Run all six code verifications against one stage-1 proposal.

    Never raises for a malformed or incomplete proposal — an unparseable
    proposal, a proposal naming no definition, a proposal missing a
    threshold entirely — every one of those is a normal, named failure of
    one or more checks, not a caller error. Only a wrong argument type
    raises.
    """
    if not isinstance(proposal, StageOneProposal):
        raise TypeError("verify_proposal requires a StageOneProposal.")
    if not isinstance(context, VerificationContext):
        raise TypeError("verify_proposal requires a VerificationContext.")

    resolved = _resolve_definition(proposal)
    checks = (
        _check_schema_valid(proposal),
        _check_definition_known(proposal, resolved),
        _check_recomputable(resolved, context),
        _check_threshold_plausible(proposal, resolved),
        _check_unit_currency_consistent(proposal, resolved, context),
        _check_frequency_dates_consistent(proposal, context),
    )
    return VerificationReport(checks=checks)


def _check_schema_valid(proposal: StageOneProposal) -> CheckOutcome:
    if proposal.parseable:
        return CheckOutcome(
            VerificationCheckName.SCHEMA_VALID,
            True,
            "The stage-1 reply matched its declared output shape.",
        )
    detail = proposal.parse_error or "The stage-1 reply did not match its declared output shape."
    return CheckOutcome(VerificationCheckName.SCHEMA_VALID, False, detail)


def _resolve_definition(proposal: StageOneProposal) -> _ResolvedDefinition:
    ref = proposal.definition_ref
    formula = proposal.custom_formula
    if ref is not None and formula is not None:
        return _ResolvedDefinition(definition=None, is_custom_formula=False)
    if ref is not None:
        return _ResolvedDefinition(definition=LIBRARY.get(ref), is_custom_formula=False)
    if formula is not None:
        return _ResolvedDefinition(
            definition=None, is_custom_formula=is_valid_custom_formula(formula)
        )
    return _ResolvedDefinition(definition=None, is_custom_formula=False)


def _check_definition_known(
    proposal: StageOneProposal, resolved: _ResolvedDefinition
) -> CheckOutcome:
    ref = proposal.definition_ref
    formula = proposal.custom_formula
    name = VerificationCheckName.DEFINITION_KNOWN
    if ref is None and formula is None:
        return CheckOutcome(name, False, "No definition or custom formula was proposed.")
    if ref is not None and formula is not None:
        return CheckOutcome(
            name,
            False,
            "Both a library definition and a custom formula were proposed; "
            "exactly one is required.",
        )
    if ref is not None:
        if resolved.definition is None:
            return CheckOutcome(name, False, f"{ref!r} names no ratio in the library.")
        return CheckOutcome(name, True, f"{ref!r} is a known ratio-library definition.")
    assert formula is not None  # narrowed: ref is None and formula is not None here
    if not resolved.is_custom_formula:
        return CheckOutcome(
            name,
            False,
            "The custom formula is not syntactically valid: only literals, statement-line "
            "names, the four arithmetic operators and parentheses are permitted, and it must "
            "reference at least one name.",
        )
    return CheckOutcome(name, True, "The custom formula is syntactically valid.")


def _check_recomputable(
    resolved: _ResolvedDefinition, context: VerificationContext
) -> CheckOutcome:
    name = VerificationCheckName.RECOMPUTABLE
    definition = resolved.definition
    if definition is None:
        if resolved.is_custom_formula:
            return CheckOutcome(
                name,
                False,
                "A custom formula cannot yet be recomputed against this borrower's stored "
                "statements in this build; it is treated as unverified.",
            )
        return CheckOutcome(name, False, "No definition was resolved to recompute.")

    try:
        result: RatioResult = compute_ratio(
            definition,
            context.statement_lines,
            context.facility_facts,
            period_complete=context.period_complete,
        )
    except UnknownDefinition:
        return CheckOutcome(name, False, f"{definition.code!r} has no registered ratio formula.")

    if not result.computable:
        assert result.reason is not None  # a not-computable RatioResult always names one
        detail = _not_computable_detail(result.reason, result.reason_context)
        return CheckOutcome(name, False, detail)
    return CheckOutcome(
        name,
        True,
        f"{definition.code!r} recomputed to {result.value} from this borrower's "
        "own stored statements.",
    )


def _not_computable_detail(reason: NotComputableReason, context: Mapping[str, str]) -> str:
    if reason is NotComputableReason.MISSING_LINE:
        return f"cannot be recomputed: missing statement line(s): {context.get('names', '?')}."
    if reason is NotComputableReason.ZERO_DENOMINATOR:
        return f"cannot be recomputed: {context.get('denominator', 'the denominator')} is zero."
    if reason is NotComputableReason.SIGN_MEANINGLESS_DENOMINATOR:
        return (
            f"cannot be recomputed: {context.get('denominator', 'the denominator')} is not "
            f"positive ({context.get('value', '?')})."
        )
    if reason is NotComputableReason.PERIOD_INCOMPLETE:
        return (
            "cannot be recomputed: the statement period failed a balance-sheet or "
            "profit-and-loss identity check and is not a sound basis for this ratio."
        )
    if reason is NotComputableReason.FACILITY_FACTS_ABSENT:
        return f"cannot be recomputed: missing facility fact(s): {context.get('names', '?')}."
    return "cannot be recomputed: this definition's formula could not produce a value."


def _check_threshold_plausible(
    proposal: StageOneProposal, resolved: _ResolvedDefinition
) -> CheckOutcome:
    name = VerificationCheckName.THRESHOLD_PLAUSIBLE
    if proposal.threshold_ambiguous:
        return CheckOutcome(
            name,
            False,
            "The threshold is ambiguous in the source text and was not resolved to a number.",
        )
    if proposal.threshold is None:
        return CheckOutcome(name, False, "No threshold value was proposed.")
    definition = resolved.definition
    if definition is None:
        return CheckOutcome(
            name,
            False,
            "No library definition was resolved against which to check the threshold's "
            "plausible band.",
        )
    threshold = proposal.threshold
    if definition.plausible_min is not None and threshold < definition.plausible_min:
        return CheckOutcome(
            name,
            False,
            f"{threshold} is below {definition.code!r}'s plausible minimum of "
            f"{definition.plausible_min}.",
        )
    if definition.plausible_max is not None and threshold > definition.plausible_max:
        return CheckOutcome(
            name,
            False,
            f"{threshold} is above {definition.code!r}'s plausible maximum of "
            f"{definition.plausible_max}.",
        )
    return CheckOutcome(name, True, f"{threshold} is within {definition.code!r}'s plausible band.")


def _check_unit_currency_consistent(
    proposal: StageOneProposal, resolved: _ResolvedDefinition, context: VerificationContext
) -> CheckOutcome:
    name = VerificationCheckName.UNIT_CURRENCY_CONSISTENT
    definition = resolved.definition
    if definition is None:
        return CheckOutcome(
            name,
            False,
            "No library definition was resolved against which to check unit and currency.",
        )
    expected_unit = _DEFINITION_UNIT_TO_PROPOSAL_UNIT.get(definition.unit)
    if expected_unit is None:
        return CheckOutcome(
            name,
            False,
            f"{definition.code!r}'s unit {definition.unit!r} has no recognised proposal-unit "
            "mapping.",
        )
    if proposal.unit is None:
        return CheckOutcome(name, False, "No unit was proposed for the threshold.")
    if proposal.unit != expected_unit:
        return CheckOutcome(
            name,
            False,
            f"{definition.code!r} expects a {expected_unit!r} threshold, not {proposal.unit!r}.",
        )
    if expected_unit == "currency":
        if proposal.currency is not None and proposal.currency != context.facility_currency:
            return CheckOutcome(
                name,
                False,
                f"{definition.code!r} is denominated in {context.facility_currency}, "
                f"not {proposal.currency}.",
            )
        return CheckOutcome(
            name, True, f"The proposed unit and currency match {definition.code!r}."
        )
    if proposal.currency is not None:
        return CheckOutcome(
            name,
            False,
            f"{definition.code!r} is a {expected_unit} definition and does not take a currency.",
        )
    return CheckOutcome(name, True, f"The proposed unit matches {definition.code!r}.")


def _check_frequency_dates_consistent(
    proposal: StageOneProposal, context: VerificationContext
) -> CheckOutcome:
    name = VerificationCheckName.FREQUENCY_DATES_CONSISTENT
    if proposal.frequency_ambiguous:
        return CheckOutcome(
            name,
            False,
            "The testing frequency is ambiguous in the source text and was not resolved.",
        )
    if proposal.frequency is None:
        return CheckOutcome(name, False, "No testing frequency was proposed.")
    if proposal.effective_from is None:
        return CheckOutcome(name, False, "No effective-from date was proposed.")
    sanction_date = context.facility_sanction_date
    if proposal.effective_from < sanction_date:
        return CheckOutcome(
            name,
            False,
            f"The proposed effective date {proposal.effective_from} precedes the facility's "
            f"sanction date {sanction_date}.",
        )
    if proposal.effective_to is not None and proposal.effective_to <= proposal.effective_from:
        return CheckOutcome(
            name,
            False,
            f"The proposed effective_to {proposal.effective_to} does not come after "
            f"effective_from {proposal.effective_from}.",
        )
    return CheckOutcome(
        name,
        True,
        f"The {proposal.frequency} testing frequency and effective date "
        f"{proposal.effective_from} are consistent with the facility.",
    )
