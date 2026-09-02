"""Twenty-two of the twenty-four `spec §R-07` ratio definitions — leverage,
coverage, liquidity, conduct and working-capital (`plan.md §6`'s `C-30`,
`T-027`/`T-028`). The remaining two — the condition-type definitions — live
in `conditions.py`, built from the same `RatioDefinition`/`RatioEntry`/
`FormulaOutcome` shapes this module defines.

A definition is metadata: its code, its human-readable formula, the
statement lines it reads, its unit, its plausible band and which side a
covenant threshold sits on. The metadata is configuration — seeded into
`ratio_definition` (`db/seed/data/ratio_definitions.json`) and editable by a
bank the same way any other reference row is. The *arithmetic* behind each
code is not configuration: it is one pure function below, keyed by code in
`ENTRIES`, and no model or external input is ever consulted to produce a
value.

Every formula reads `lines`, the `{code: Decimal}` mapping `Chart.normalise`
(`T-024`) produces, and (for the definitions `T-028` adds) `facility`, the
`FacilityFacts` a caller assembles from the `facility` and `facility_conduct`
tables (`plan.md §5.2`). No formula ever raises: a missing line, a missing
facility fact, a zero denominator or a denominator whose sign makes the
ratio meaningless all resolve to a `FormulaOutcome` naming why, never to an
arithmetic exception — and, since `T-030`, always as one of
`reasons.NotComputableReason`'s enumerated members, never as a free-text
sentence assembled here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from covenant_radar.domain.ratios.reasons import NotComputableReason

_VALID_DIRECTION_HINTS: Final[frozenset[str]] = frozenset({"min", "max"})
_VALID_KINDS: Final[frozenset[str]] = frozenset({"ratio", "condition"})


@dataclass(frozen=True, slots=True)
class RatioDefinition:
    """One ratio's metadata — the shape `ratio_definition` persists.

    `required_lines` names every input the formula reads and reports as
    missing when absent — ordinarily `Chart` statement-line codes, but for
    the handful of `T-028` definitions computed from `FacilityFacts`
    instead (`utilisation`, `drawing_power_headroom`), the `FacilityFacts`
    field names it reads. Nothing downstream cross-checks this list against
    the statement-line catalog, so either is valid; it exists for
    self-documentation and for the not-computable reason a formula reports.

    `direction_hint` names which side of a covenant threshold this
    definition normally sits on (``"min"``: breach is falling below the
    threshold; ``"max"``: breach is rising above it) — a hint for the UI
    and the covenant engine, not itself a covenant.

    `kind` distinguishes a plain ratio (``"ratio"``, the default — a
    comparable value only) from a covenant condition (``"condition"`` —
    `conditions.py`'s two definitions, whose formula also carries an
    intrinsic pass/fail `FormulaOutcome.outcome`). It is domain-only
    metadata: `ratio_definition` (the seeded/persisted row) carries no
    matching column, because the distinction governs how the *value* this
    module's own pure function produces is interpreted, not the value
    itself.
    """

    code: str
    name: str
    formula_text: str
    required_lines: tuple[str, ...]
    unit: str
    plausible_min: Decimal | None
    plausible_max: Decimal | None
    direction_hint: str | None
    kind: str = "ratio"

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("RatioDefinition.code must be non-empty.")
        if not self.name:
            raise ValueError(f"RatioDefinition {self.code!r}.name must be non-empty.")
        if not self.formula_text:
            raise ValueError(f"RatioDefinition {self.code!r}.formula_text must be non-empty.")
        if not self.required_lines:
            raise ValueError(f"RatioDefinition {self.code!r} must name at least one required line.")
        if not self.unit:
            raise ValueError(f"RatioDefinition {self.code!r}.unit must be non-empty.")
        if self.direction_hint is not None and self.direction_hint not in _VALID_DIRECTION_HINTS:
            raise ValueError(
                f"RatioDefinition {self.code!r}.direction_hint must be 'min', 'max', or None."
            )
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"RatioDefinition {self.code!r}.kind must be 'ratio' or 'condition'.")
        if (
            self.plausible_min is not None
            and self.plausible_max is not None
            and self.plausible_min > self.plausible_max
        ):
            raise ValueError(f"RatioDefinition {self.code!r} has plausible_min > plausible_max.")


@dataclass(frozen=True, slots=True)
class FacilityFacts:
    """Facility and covenant-conduct facts a ratio definition may read,
    assembled by the caller from `facility` and `facility_conduct`
    (`plan.md §5.2`) — one snapshot as of the period a ratio is being tested
    for, never an ORM row, so this module stays free of any persistence
    import.

    Every field is optional: a formula that needs one names it, by field
    name, in a not-computable reason when it is absent, the same way a
    missing statement line is named. Twenty of this file's twenty-two
    definitions are statement-only and ignore `facility` entirely; only
    `utilisation` and `drawing_power_headroom` read it. `compute.py`'s
    dispatch passes `facility` to every formula regardless, so it stays
    uniform across the whole library.
    """

    sanctioned_limit: Decimal | None = None
    outstanding: Decimal | None = None
    drawing_power: Decimal | None = None
    promoter_shareholding_floor_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class FormulaOutcome:
    """What one pure ratio function returns before `compute.py` wraps it.

    Exactly one of "computable with a value" or "not computable with a
    reason" is ever true — enforced here so a formula cannot accidentally
    return both or neither.

    `reason` is one of `reasons.NotComputableReason`'s enumerated members,
    never free text (`T-030`); `reason_context` carries the handful of
    interpolation values — a line name, a denominator's value — the
    translation catalogue's one template for that reason needs to render
    it as a sentence. A formula never composes that sentence itself.

    `outcome` is the intrinsic boolean a condition-type definition
    (`conditions.py`) carries alongside its comparable `value`, so `C-32`
    can evaluate a condition the same way it evaluates a plain ratio,
    without a special case: a plain ratio's formula simply never sets it,
    and it stays `None`. Only meaningful when `computable` is true — a
    condition that cannot be measured has no pass/fail outcome either.
    """

    value: Decimal | None
    computable: bool
    reason: NotComputableReason | None
    inputs_used: Mapping[str, Decimal]
    reason_context: Mapping[str, str] = field(default_factory=dict)
    outcome: bool | None = None

    def __post_init__(self) -> None:
        if self.computable:
            if self.value is None:
                raise ValueError("A computable FormulaOutcome must carry a value.")
            if self.reason is not None:
                raise ValueError("A computable FormulaOutcome must not carry a reason.")
            if self.reason_context:
                raise ValueError("A computable FormulaOutcome must not carry reason context.")
        else:
            if self.value is not None:
                raise ValueError("A not-computable FormulaOutcome must not carry a value.")
            if self.reason is None:
                raise ValueError("A not-computable FormulaOutcome must name a reason.")
            if not isinstance(self.reason, NotComputableReason):
                raise TypeError(
                    "A FormulaOutcome.reason must be a NotComputableReason member, "
                    f"not {type(self.reason).__name__}."
                )
            if self.outcome is not None:
                raise ValueError("A not-computable FormulaOutcome must not carry an outcome.")


RatioFormula = Callable[[Mapping[str, Decimal], "FacilityFacts | None"], FormulaOutcome]


@dataclass(frozen=True, slots=True)
class RatioEntry:
    """One definition paired with the pure function that computes it."""

    definition: RatioDefinition
    formula: RatioFormula


@dataclass(frozen=True, slots=True)
class _Term:
    """One signed statement line inside a formula's numerator or denominator."""

    code: str
    sign: int = 1


def _sum_terms(lines: Mapping[str, Decimal], terms: tuple[_Term, ...]) -> Decimal:
    return sum((lines[term.code] * term.sign for term in terms), start=Decimal(0))


def _terms_label(terms: tuple[_Term, ...]) -> str:
    parts: list[str] = []
    for index, term in enumerate(terms):
        if index == 0:
            parts.append(term.code if term.sign >= 0 else f"-{term.code}")
        else:
            parts.append(f" + {term.code}" if term.sign >= 0 else f" - {term.code}")
    return "".join(parts)


def _missing_lines_outcome(
    missing: Iterable[str], inputs_used: Mapping[str, Decimal]
) -> FormulaOutcome:
    names = ", ".join(sorted(missing))
    return FormulaOutcome(
        value=None,
        computable=False,
        reason=NotComputableReason.MISSING_LINE,
        inputs_used=inputs_used,
        reason_context={"names": names},
    )


def _missing_facility_facts_outcome(missing: Iterable[str]) -> FormulaOutcome:
    names = ", ".join(sorted(missing))
    return FormulaOutcome(
        value=None,
        computable=False,
        reason=NotComputableReason.FACILITY_FACTS_ABSENT,
        inputs_used={},
        reason_context={"names": names},
    )


def _zero_denominator_outcome(
    denominator_label: str, inputs_used: Mapping[str, Decimal]
) -> FormulaOutcome:
    return FormulaOutcome(
        value=None,
        computable=False,
        reason=NotComputableReason.ZERO_DENOMINATOR,
        inputs_used=inputs_used,
        reason_context={"denominator": denominator_label},
    )


def _sign_meaningless_denominator_outcome(
    denominator_label: str, value: Decimal, inputs_used: Mapping[str, Decimal]
) -> FormulaOutcome:
    return FormulaOutcome(
        value=None,
        computable=False,
        reason=NotComputableReason.SIGN_MEANINGLESS_DENOMINATOR,
        inputs_used=inputs_used,
        reason_context={"denominator": denominator_label, "value": str(value)},
    )


def _make_division_formula(
    *,
    numerator: tuple[_Term, ...],
    denominator: tuple[_Term, ...],
    scale: Decimal = Decimal(1),
) -> RatioFormula:
    """Build a pure ratio function for `(sum of numerator) / (sum of
    denominator) * scale`, with the shared not-computable behaviour every
    definition in this file needs: a missing line names itself, a zero
    denominator is refused as zero, and a negative denominator is refused
    as sign-meaningless — never an arithmetic exception."""
    required_codes = tuple(dict.fromkeys(term.code for term in (*numerator, *denominator)))
    denominator_label = _terms_label(denominator)

    def formula(lines: Mapping[str, Decimal], facility: FacilityFacts | None) -> FormulaOutcome:
        missing = [code for code in required_codes if code not in lines]
        inputs_used = {code: lines[code] for code in required_codes if code in lines}
        if missing:
            return _missing_lines_outcome(missing, inputs_used)

        denominator_value = _sum_terms(lines, denominator)
        if denominator_value == 0:
            return _zero_denominator_outcome(denominator_label, inputs_used)
        if denominator_value < 0:
            return _sign_meaningless_denominator_outcome(
                denominator_label, denominator_value, inputs_used
            )

        numerator_value = _sum_terms(lines, numerator)
        value = (numerator_value / denominator_value) * scale
        return FormulaOutcome(value=value, computable=True, reason=None, inputs_used=inputs_used)

    return formula


def _compute_tnw_floor(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """Tangible net worth, read directly off the reported line — the figure
    a covenant that names "TNW" tests against as-extracted."""
    code = "tangible_net_worth"
    if code not in lines:
        return _missing_lines_outcome((code,), {})
    value = lines[code]
    return FormulaOutcome(value=value, computable=True, reason=None, inputs_used={code: value})


def _compute_minimum_net_worth(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """Net worth derived structurally as `total_assets - total_liabilities`,
    distinct from `tnw_floor`: a covenant that names "Net Worth" (rather
    than the reported tangible-net-worth line) is tested against the
    balance-sheet structure directly, so it stays computable even when the
    `tangible_net_worth` line itself is absent, and — deliberately — is not
    forced to agree with it when the two are not."""
    codes = ("total_assets", "total_liabilities")
    missing = [code for code in codes if code not in lines]
    inputs_used = {code: lines[code] for code in codes if code in lines}
    if missing:
        return _missing_lines_outcome(missing, inputs_used)
    value = lines["total_assets"] - lines["total_liabilities"]
    return FormulaOutcome(value=value, computable=True, reason=None, inputs_used=inputs_used)


#: ₹ crore magnitude sanity bounds for a balance-sheet net-worth figure —
#: wide enough that a genuinely distressed, negative-net-worth borrower is
#: still flagged as computed, not excluded; only a figure larger than
#: India's largest listed company's net worth is implausible as *data*.
_NET_WORTH_PLAUSIBLE_MIN: Final[Decimal] = Decimal("-100000")
_NET_WORTH_PLAUSIBLE_MAX: Final[Decimal] = Decimal("2000000")

_PERCENTAGE_SCALE: Final[Decimal] = Decimal(100)
_DAYS_IN_YEAR: Final[Decimal] = Decimal(365)


def _make_line_read_formula(code: str) -> RatioFormula:
    """Build a pure ratio function that reads exactly one statement line
    as-is — `tnw_floor`'s shape (`T-027`), reused for the `T-028`
    definitions (`minimum_liquidity`, `maximum_capex`) that are likewise a
    bare figure a covenant tests directly, not a computed ratio."""

    def formula(lines: Mapping[str, Decimal], facility: FacilityFacts | None) -> FormulaOutcome:
        if code not in lines:
            return _missing_lines_outcome((code,), {})
        value = lines[code]
        return FormulaOutcome(value=value, computable=True, reason=None, inputs_used={code: value})

    return formula


def _compute_working_capital_gap(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """Working-capital gap — total current assets less current liabilities
    other than bank borrowings (the MPBF-II convention): `current_assets -
    payables - other_current_liabilities`, deliberately read from the
    primitive lines rather than through the derived `current_liabilities`
    total, so a bank's own short-term borrowing is excluded by construction
    rather than added back after the fact."""
    codes = ("current_assets", "payables", "other_current_liabilities")
    missing = [code for code in codes if code not in lines]
    inputs_used = {code: lines[code] for code in codes if code in lines}
    if missing:
        return _missing_lines_outcome(missing, inputs_used)
    value = lines["current_assets"] - lines["payables"] - lines["other_current_liabilities"]
    return FormulaOutcome(value=value, computable=True, reason=None, inputs_used=inputs_used)


def _compute_cash_conversion_cycle(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """Cash conversion cycle — receivable days plus inventory days less
    payable days — computed as one formula rather than by composing the
    three day-count ratios below, so a single not-computable reason (a zero
    or negative revenue or cost of goods sold) is reported once rather than
    three times over."""
    codes = ("receivables", "revenue", "inventory", "cost_of_goods_sold", "payables")
    missing = [code for code in codes if code not in lines]
    inputs_used = {code: lines[code] for code in codes if code in lines}
    if missing:
        return _missing_lines_outcome(missing, inputs_used)

    revenue = lines["revenue"]
    cost_of_goods_sold = lines["cost_of_goods_sold"]
    if revenue == 0:
        return _zero_denominator_outcome("revenue", inputs_used)
    if revenue < 0:
        return _sign_meaningless_denominator_outcome("revenue", revenue, inputs_used)
    if cost_of_goods_sold == 0:
        return _zero_denominator_outcome("cost_of_goods_sold", inputs_used)
    if cost_of_goods_sold < 0:
        return _sign_meaningless_denominator_outcome(
            "cost_of_goods_sold", cost_of_goods_sold, inputs_used
        )

    receivable_days = (lines["receivables"] / revenue) * _DAYS_IN_YEAR
    inventory_days = (lines["inventory"] / cost_of_goods_sold) * _DAYS_IN_YEAR
    payable_days = (lines["payables"] / cost_of_goods_sold) * _DAYS_IN_YEAR
    value = receivable_days + inventory_days - payable_days
    return FormulaOutcome(value=value, computable=True, reason=None, inputs_used=inputs_used)


def _compute_utilisation(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """Facility utilisation — `outstanding / sanctioned_limit * 100`, read
    from `FacilityFacts` rather than trusting `facility_conduct`'s own
    pre-rounded `utilisation_pct`, so the library's one exact-`Decimal`
    computation is never a second, potentially divergent, source of truth.
    Deliberately uncapped: a borrower drawn beyond its sanctioned limit is
    over 100% utilised, in fact, and that fact is what the plausible-band
    check (`plausible_max=100`) exists to flag, not to hide by clamping."""
    required = ("sanctioned_limit", "outstanding")
    missing = [name for name in required if facility is None or getattr(facility, name) is None]
    if missing:
        return _missing_facility_facts_outcome(missing)
    assert facility is not None  # narrowed by the `missing` check above
    sanctioned_limit = facility.sanctioned_limit
    outstanding = facility.outstanding
    assert sanctioned_limit is not None and outstanding is not None
    inputs_used = {
        "facility.sanctioned_limit": sanctioned_limit,
        "facility.outstanding": outstanding,
    }
    if sanctioned_limit == 0:
        return _zero_denominator_outcome("facility.sanctioned_limit", inputs_used)
    if sanctioned_limit < 0:
        return _sign_meaningless_denominator_outcome(
            "facility.sanctioned_limit", sanctioned_limit, inputs_used
        )
    value = (outstanding / sanctioned_limit) * _PERCENTAGE_SCALE
    return FormulaOutcome(value=value, computable=True, reason=None, inputs_used=inputs_used)


def _compute_drawing_power_headroom(
    lines: Mapping[str, Decimal], facility: FacilityFacts | None
) -> FormulaOutcome:
    """Drawing-power headroom — `drawing_power - outstanding`. Negative is
    a real and important state (an excess drawing beyond the bank-assessed
    drawing power), returned as measured, never clamped to zero."""
    required = ("drawing_power", "outstanding")
    missing = [name for name in required if facility is None or getattr(facility, name) is None]
    if missing:
        return _missing_facility_facts_outcome(missing)
    assert facility is not None  # narrowed by the `missing` check above
    drawing_power = facility.drawing_power
    outstanding = facility.outstanding
    assert drawing_power is not None and outstanding is not None
    inputs_used = {
        "facility.drawing_power": drawing_power,
        "facility.outstanding": outstanding,
    }
    value = drawing_power - outstanding
    return FormulaOutcome(value=value, computable=True, reason=None, inputs_used=inputs_used)


ENTRIES: Final[tuple[RatioEntry, ...]] = (
    RatioEntry(
        definition=RatioDefinition(
            code="leverage_ratio",
            name="Leverage ratio",
            formula_text="total_debt / tangible_net_worth",
            required_lines=("total_debt", "tangible_net_worth"),
            unit="x",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("6"),
            direction_hint="max",
        ),
        formula=_make_division_formula(
            numerator=(_Term("total_debt"),),
            denominator=(_Term("tangible_net_worth"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="dscr",
            name="Debt service coverage ratio",
            formula_text="cash_flow_debt_service / finance_cost",
            required_lines=("cash_flow_debt_service", "finance_cost"),
            unit="x",
            plausible_min=Decimal("-5"),
            plausible_max=Decimal("20"),
            direction_hint="min",
        ),
        formula=_make_division_formula(
            numerator=(_Term("cash_flow_debt_service"),),
            denominator=(_Term("finance_cost"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="interest_coverage_ratio",
            name="Interest coverage ratio",
            formula_text="ebit / finance_cost",
            required_lines=("ebit", "finance_cost"),
            unit="x",
            plausible_min=Decimal("-5"),
            plausible_max=Decimal("50"),
            direction_hint="min",
        ),
        formula=_make_division_formula(
            numerator=(_Term("ebit"),),
            denominator=(_Term("finance_cost"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="fixed_charge_coverage_ratio",
            name="Fixed-charge coverage ratio",
            formula_text="ebitda / finance_cost",
            required_lines=("ebitda", "finance_cost"),
            unit="x",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("30"),
            direction_hint="min",
        ),
        formula=_make_division_formula(
            numerator=(_Term("ebitda"),),
            denominator=(_Term("finance_cost"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="current_ratio",
            name="Current ratio",
            formula_text="current_assets / current_liabilities",
            required_lines=("current_assets", "current_liabilities"),
            unit="x",
            plausible_min=Decimal("1"),
            plausible_max=Decimal("4"),
            direction_hint="min",
        ),
        formula=_make_division_formula(
            numerator=(_Term("current_assets"),),
            denominator=(_Term("current_liabilities"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="quick_ratio",
            name="Quick ratio",
            formula_text="(current_assets - inventory) / current_liabilities",
            required_lines=("current_assets", "inventory", "current_liabilities"),
            unit="x",
            plausible_min=Decimal("0.5"),
            plausible_max=Decimal("3"),
            direction_hint="min",
        ),
        formula=_make_division_formula(
            numerator=(_Term("current_assets"), _Term("inventory", sign=-1)),
            denominator=(_Term("current_liabilities"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="tol_tnw",
            name="Total outside liabilities / tangible net worth",
            formula_text="total_liabilities / tangible_net_worth",
            required_lines=("total_liabilities", "tangible_net_worth"),
            unit="x",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("6"),
            direction_hint="max",
        ),
        formula=_make_division_formula(
            numerator=(_Term("total_liabilities"),),
            denominator=(_Term("tangible_net_worth"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="debt_to_ebitda",
            name="Debt / EBITDA",
            formula_text="total_debt / ebitda",
            required_lines=("total_debt", "ebitda"),
            unit="x",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("5"),
            direction_hint="max",
        ),
        formula=_make_division_formula(
            numerator=(_Term("total_debt"),),
            denominator=(_Term("ebitda"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="net_debt_to_ebitda",
            name="Net debt / EBITDA",
            formula_text="(total_debt - cash_and_bank) / ebitda",
            required_lines=("total_debt", "cash_and_bank", "ebitda"),
            unit="x",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("5"),
            direction_hint="max",
        ),
        formula=_make_division_formula(
            numerator=(_Term("total_debt"), _Term("cash_and_bank", sign=-1)),
            denominator=(_Term("ebitda"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="ebitda_margin",
            name="EBITDA margin",
            formula_text="ebitda / revenue * 100",
            required_lines=("ebitda", "revenue"),
            unit="%",
            plausible_min=Decimal("5"),
            plausible_max=Decimal("60"),
            direction_hint="min",
        ),
        formula=_make_division_formula(
            numerator=(_Term("ebitda"),),
            denominator=(_Term("revenue"),),
            scale=_PERCENTAGE_SCALE,
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="tnw_floor",
            name="Tangible net worth floor",
            formula_text="tangible_net_worth",
            required_lines=("tangible_net_worth",),
            unit="₹ crore",
            plausible_min=_NET_WORTH_PLAUSIBLE_MIN,
            plausible_max=_NET_WORTH_PLAUSIBLE_MAX,
            direction_hint="min",
        ),
        formula=_compute_tnw_floor,
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="minimum_net_worth",
            name="Minimum net worth",
            formula_text="total_assets - total_liabilities",
            required_lines=("total_assets", "total_liabilities"),
            unit="₹ crore",
            plausible_min=_NET_WORTH_PLAUSIBLE_MIN,
            plausible_max=_NET_WORTH_PLAUSIBLE_MAX,
            direction_hint="min",
        ),
        formula=_compute_minimum_net_worth,
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="utilisation",
            name="Facility utilisation",
            formula_text="facility.outstanding / facility.sanctioned_limit * 100",
            required_lines=("sanctioned_limit", "outstanding"),
            unit="%",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("100"),
            direction_hint="max",
        ),
        formula=_compute_utilisation,
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="drawing_power_headroom",
            name="Drawing-power headroom",
            formula_text="facility.drawing_power - facility.outstanding",
            required_lines=("drawing_power", "outstanding"),
            unit="₹ crore",
            plausible_min=_NET_WORTH_PLAUSIBLE_MIN,
            plausible_max=_NET_WORTH_PLAUSIBLE_MAX,
            direction_hint="min",
        ),
        formula=_compute_drawing_power_headroom,
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="receivable_days",
            name="Receivable days",
            formula_text="receivables / revenue * 365",
            required_lines=("receivables", "revenue"),
            unit="days",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("180"),
            direction_hint="max",
        ),
        formula=_make_division_formula(
            numerator=(_Term("receivables"),),
            denominator=(_Term("revenue"),),
            scale=_DAYS_IN_YEAR,
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="inventory_days",
            name="Inventory days",
            formula_text="inventory / cost_of_goods_sold * 365",
            required_lines=("inventory", "cost_of_goods_sold"),
            unit="days",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("180"),
            direction_hint="max",
        ),
        formula=_make_division_formula(
            numerator=(_Term("inventory"),),
            denominator=(_Term("cost_of_goods_sold"),),
            scale=_DAYS_IN_YEAR,
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="payable_days",
            name="Payable days",
            formula_text="payables / cost_of_goods_sold * 365",
            required_lines=("payables", "cost_of_goods_sold"),
            unit="days",
            plausible_min=Decimal("0"),
            plausible_max=Decimal("180"),
            direction_hint=None,
        ),
        formula=_make_division_formula(
            numerator=(_Term("payables"),),
            denominator=(_Term("cost_of_goods_sold"),),
            scale=_DAYS_IN_YEAR,
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="cash_conversion_cycle",
            name="Cash conversion cycle",
            formula_text=(
                "receivables / revenue * 365 + inventory / cost_of_goods_sold * 365 "
                "- payables / cost_of_goods_sold * 365"
            ),
            required_lines=(
                "receivables",
                "revenue",
                "inventory",
                "cost_of_goods_sold",
                "payables",
            ),
            unit="days",
            plausible_min=Decimal("-90"),
            plausible_max=Decimal("365"),
            direction_hint="max",
        ),
        formula=_compute_cash_conversion_cycle,
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="working_capital_gap",
            name="Working-capital gap",
            formula_text="current_assets - payables - other_current_liabilities",
            required_lines=("current_assets", "payables", "other_current_liabilities"),
            unit="₹ crore",
            plausible_min=_NET_WORTH_PLAUSIBLE_MIN,
            plausible_max=_NET_WORTH_PLAUSIBLE_MAX,
            direction_hint="max",
        ),
        formula=_compute_working_capital_gap,
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="asset_cover_ratio",
            name="Asset cover ratio",
            formula_text="total_assets / total_debt",
            required_lines=("total_assets", "total_debt"),
            unit="x",
            plausible_min=Decimal("1"),
            plausible_max=Decimal("10"),
            direction_hint="min",
        ),
        formula=_make_division_formula(
            numerator=(_Term("total_assets"),),
            denominator=(_Term("total_debt"),),
        ),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="minimum_liquidity",
            name="Minimum liquidity",
            formula_text="cash_and_bank",
            required_lines=("cash_and_bank",),
            unit="₹ crore",
            plausible_min=Decimal("0"),
            plausible_max=_NET_WORTH_PLAUSIBLE_MAX,
            direction_hint="min",
        ),
        formula=_make_line_read_formula("cash_and_bank"),
    ),
    RatioEntry(
        definition=RatioDefinition(
            code="maximum_capex",
            name="Maximum capital expenditure",
            formula_text="capex",
            required_lines=("capex",),
            unit="₹ crore",
            plausible_min=Decimal("0"),
            plausible_max=_NET_WORTH_PLAUSIBLE_MAX,
            direction_hint="max",
        ),
        formula=_make_line_read_formula("capex"),
    ),
)


__all__ = [
    "ENTRIES",
    "FacilityFacts",
    "FormulaOutcome",
    "RatioDefinition",
    "RatioEntry",
    "RatioFormula",
]
