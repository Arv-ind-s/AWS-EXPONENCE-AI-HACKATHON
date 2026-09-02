"""Unit tests for `T-030` — not-computable and missing-line behaviour
across the ratio library (`plan.md §6`'s `C-30`, `spec §R-07.b`, `R-07.c`,
`R-08.d`).

Every one of the twenty-four definitions is driven into each not-computable
mode that applies to it and must report one of `NotComputableReason`'s
enumerated members — never a free-text sentence assembled inline. The
static checks below (`test_no_free_text_reason_in_source`) and the
catalogue check (`test_every_reason_has_a_translation`) are this task's own
build check, in the same style `test_ratios_1.py`'s
`test_no_float_in_computation_path` already uses for its invariant.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterable
from decimal import Decimal
from types import ModuleType

import pytest

from covenant_radar.domain.ratios import compute, conditions, definitions
from covenant_radar.domain.ratios.compute import FacilityFacts, compute_ratio
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason, translation_key
from covenant_radar.i18n import default_catalogue

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# A statement extract and facility that together make every one of the
# twenty-four definitions computable — the starting point every case below
# knocks exactly one thing out of.
# ---------------------------------------------------------------------------

_FULL_LINES: dict[str, Decimal] = {
    "total_debt": Decimal("400"),
    "tangible_net_worth": Decimal("200"),
    "cash_flow_debt_service": Decimal("180"),
    "finance_cost": Decimal("40"),
    "ebit": Decimal("210"),
    "ebitda": Decimal("250"),
    "current_assets": Decimal("400"),
    "current_liabilities": Decimal("200"),
    "inventory": Decimal("120"),
    "total_liabilities": Decimal("500"),
    "revenue": Decimal("1000"),
    "cost_of_goods_sold": Decimal("500"),
    "receivables": Decimal("100"),
    "payables": Decimal("50"),
    "other_current_liabilities": Decimal("90"),
    "total_assets": Decimal("760"),
    "cash_and_bank": Decimal("80"),
    "capex": Decimal("55"),
    "dividend_paid": Decimal("0"),
    "promoter_shareholding": Decimal("60"),
}

_FULL_FACILITY = FacilityFacts(
    sanctioned_limit=Decimal("500"),
    outstanding=Decimal("350"),
    drawing_power=Decimal("400"),
    promoter_shareholding_floor_pct=Decimal("51"),
)


def _without(*codes: str) -> dict[str, Decimal]:
    return {code: value for code, value in _FULL_LINES.items() if code not in codes}


def _with_value(code: str, value: Decimal) -> dict[str, Decimal]:
    return {**_FULL_LINES, code: value}


# Every division-formula (and structural-subtraction) definition's one
# representative required statement line, whose absence must be reported
# as `MISSING_LINE`. `utilisation` and `drawing_power_headroom` read no
# statement line at all — they are facility-only — so they are absent here
# and covered instead under facility-facts-absent below.
_MISSING_LINE_PROBE: dict[str, str] = {
    "leverage_ratio": "total_debt",
    "dscr": "cash_flow_debt_service",
    "interest_coverage_ratio": "ebit",
    "fixed_charge_coverage_ratio": "ebitda",
    "current_ratio": "current_assets",
    "quick_ratio": "inventory",
    "tol_tnw": "total_liabilities",
    "debt_to_ebitda": "total_debt",
    "net_debt_to_ebitda": "cash_and_bank",
    "ebitda_margin": "ebitda",
    "tnw_floor": "tangible_net_worth",
    "minimum_net_worth": "total_assets",
    "receivable_days": "receivables",
    "inventory_days": "inventory",
    "payable_days": "payables",
    "cash_conversion_cycle": "receivables",
    "working_capital_gap": "current_assets",
    "asset_cover_ratio": "total_assets",
    "minimum_liquidity": "cash_and_bank",
    "maximum_capex": "capex",
    "dividend_restriction": "dividend_paid",
    "promoter_shareholding_floor": "promoter_shareholding",
}

# Every definition with a single-line denominator, and that line's code.
_DENOMINATOR_CODE: dict[str, str] = {
    "leverage_ratio": "tangible_net_worth",
    "dscr": "finance_cost",
    "interest_coverage_ratio": "finance_cost",
    "fixed_charge_coverage_ratio": "finance_cost",
    "current_ratio": "current_liabilities",
    "quick_ratio": "current_liabilities",
    "tol_tnw": "tangible_net_worth",
    "debt_to_ebitda": "ebitda",
    "net_debt_to_ebitda": "ebitda",
    "ebitda_margin": "revenue",
    "receivable_days": "revenue",
    "inventory_days": "cost_of_goods_sold",
    "payable_days": "cost_of_goods_sold",
    "asset_cover_ratio": "total_debt",
}

#: Each entry is `(code, lines, facility, expected_reason, mode_label)` —
#: one applicable not-computable mode driven for one definition.
_NotComputableCase = tuple[str, dict[str, Decimal], FacilityFacts | None, NotComputableReason, str]

_MISSING_LINE_CASES: list[_NotComputableCase] = [
    (code, _without(probe), _FULL_FACILITY, NotComputableReason.MISSING_LINE, "missing-line")
    for code, probe in _MISSING_LINE_PROBE.items()
]

_ZERO_DENOMINATOR_CASES: list[_NotComputableCase] = [
    (
        code,
        _with_value(denom, Decimal("0")),
        _FULL_FACILITY,
        NotComputableReason.ZERO_DENOMINATOR,
        "zero-denominator",
    )
    for code, denom in _DENOMINATOR_CODE.items()
]
_ZERO_DENOMINATOR_CASES += [
    (
        "cash_conversion_cycle",
        _with_value("revenue", Decimal("0")),
        _FULL_FACILITY,
        NotComputableReason.ZERO_DENOMINATOR,
        "zero-denominator",
    ),
    (
        "utilisation",
        _FULL_LINES,
        FacilityFacts(sanctioned_limit=Decimal("0"), outstanding=Decimal("100")),
        NotComputableReason.ZERO_DENOMINATOR,
        "zero-denominator",
    ),
]

_SIGN_MEANINGLESS_CASES: list[_NotComputableCase] = [
    (
        code,
        _with_value(denom, Decimal("-10")),
        _FULL_FACILITY,
        NotComputableReason.SIGN_MEANINGLESS_DENOMINATOR,
        "sign-meaningless",
    )
    for code, denom in _DENOMINATOR_CODE.items()
]
_SIGN_MEANINGLESS_CASES += [
    (
        "cash_conversion_cycle",
        _with_value("revenue", Decimal("-10")),
        _FULL_FACILITY,
        NotComputableReason.SIGN_MEANINGLESS_DENOMINATOR,
        "sign-meaningless",
    ),
    (
        "utilisation",
        _FULL_LINES,
        FacilityFacts(sanctioned_limit=Decimal("-10"), outstanding=Decimal("100")),
        NotComputableReason.SIGN_MEANINGLESS_DENOMINATOR,
        "sign-meaningless",
    ),
]

_FACILITY_FACTS_ABSENT_CASES: list[_NotComputableCase] = [
    (
        "utilisation",
        _FULL_LINES,
        None,
        NotComputableReason.FACILITY_FACTS_ABSENT,
        "facility-absent",
    ),
    (
        "drawing_power_headroom",
        _FULL_LINES,
        None,
        NotComputableReason.FACILITY_FACTS_ABSENT,
        "facility-absent",
    ),
    (
        "promoter_shareholding_floor",
        _FULL_LINES,
        FacilityFacts(),
        NotComputableReason.FACILITY_FACTS_ABSENT,
        "facility-absent",
    ),
]

_ALL_CASES: list[_NotComputableCase] = (
    _MISSING_LINE_CASES
    + _ZERO_DENOMINATOR_CASES
    + _SIGN_MEANINGLESS_CASES
    + _FACILITY_FACTS_ABSENT_CASES
)

_PARAMETRIZED_CASES = [
    pytest.param(code, lines, facility, reason, id=f"{mode}-{code}")
    for code, lines, facility, reason, mode in _ALL_CASES
]


def test_every_definition_is_exercised() -> None:
    exercised_codes = {code for code, *_ in _ALL_CASES}
    assert exercised_codes == set(LIBRARY)
    assert len(LIBRARY) == 24


@pytest.mark.parametrize(("code", "lines", "facility", "expected_reason"), _PARAMETRIZED_CASES)
def test_every_definition_uses_enumerated_reason(
    code: str,
    lines: dict[str, Decimal],
    facility: FacilityFacts | None,
    expected_reason: NotComputableReason,
) -> None:
    result = compute_ratio(LIBRARY[code], lines, facility)

    assert result.computable is False
    assert result.value is None
    assert isinstance(result.reason, NotComputableReason)
    assert result.reason is expected_reason
    assert result.reason_context, "a not-computable result must carry interpolation context"


def test_period_incomplete_short_circuits_before_any_formula_runs() -> None:
    # A bogus `lines`/`facility` pair that would otherwise compute cleanly —
    # proving the period-incomplete short-circuit runs before the formula
    # ever sees them, not merely that the formula itself happens to fail.
    result = compute_ratio(
        LIBRARY["leverage_ratio"], _FULL_LINES, _FULL_FACILITY, period_complete=False
    )

    assert result.computable is False
    assert result.value is None
    assert result.reason is NotComputableReason.PERIOD_INCOMPLETE
    assert result.inputs_used == {}


# ---------------------------------------------------------------------------
# The vocabulary itself: closed, and every member translated.
# ---------------------------------------------------------------------------


def test_every_reason_has_a_translation() -> None:
    catalogue = default_catalogue()
    for reason in NotComputableReason:
        key = translation_key(reason)
        assert catalogue.has(key), f"{reason!r} has no translation entry ({key!r})."
        # Every context placeholder this reason's callers supply must be
        # one the template can actually fill.
        assert catalogue.translate(key, names="x", denominator="x", value="x")


def test_reason_codes_are_stable_strings() -> None:
    # `NotComputableReason` mixes in `str` so a persisted code (e.g. on a
    # covenant test record) round-trips as plain text without a codec.
    for reason in NotComputableReason:
        assert isinstance(reason.value, str)
        assert reason.value == reason


# ---------------------------------------------------------------------------
# No free-text reason anywhere in the computation path.
# ---------------------------------------------------------------------------

_REASON_CONSTRUCTING_CALL_NAMES = frozenset({"FormulaOutcome", "RatioResult"})


def _iter_reason_keyword_values(module: ModuleType) -> Iterable[ast.expr]:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _REASON_CONSTRUCTING_CALL_NAMES:
            continue
        for keyword in node.keywords:
            if keyword.arg == "reason":
                yield keyword.value


def _assert_no_free_text_reason(module: ModuleType) -> None:
    for value_node in _iter_reason_keyword_values(module):
        if isinstance(value_node, ast.Constant) and value_node.value is None:
            continue  # `reason=None` — the computable case.
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            raise AssertionError(
                f"{module.__name__} passes a free-text string literal as `reason=` "
                f"({value_node.value!r}); use a NotComputableReason member instead."
            )
        if isinstance(value_node, ast.JoinedStr):
            raise AssertionError(
                f"{module.__name__} passes an f-string as `reason=`; "
                "use a NotComputableReason member instead."
            )


def test_no_free_text_reason_in_source() -> None:
    for module in (definitions, conditions, compute):
        _assert_no_free_text_reason(module)


def test_formula_outcome_rejects_free_text_reason() -> None:
    from covenant_radar.domain.ratios.definitions import FormulaOutcome

    with pytest.raises(TypeError):
        FormulaOutcome(
            value=None,
            computable=False,
            reason="tangible_net_worth is not positive (-50)",  # type: ignore[arg-type]
            inputs_used={},
        )
