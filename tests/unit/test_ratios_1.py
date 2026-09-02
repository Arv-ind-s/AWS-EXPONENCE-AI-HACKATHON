"""Unit tests for the `T-027` ratio library part 1 — leverage, coverage and
liquidity (`plan.md §6`'s `C-30`)."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from types import ModuleType

import pytest

from covenant_radar.domain.ratios import compute, definitions, library
from covenant_radar.domain.ratios.compute import RatioResult, UnknownDefinition, compute_ratio
from covenant_radar.domain.ratios.definitions import ENTRIES, RatioDefinition
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason

pytestmark = pytest.mark.unit

_TWELVE_CODES = frozenset(
    {
        "leverage_ratio",
        "dscr",
        "interest_coverage_ratio",
        "fixed_charge_coverage_ratio",
        "current_ratio",
        "quick_ratio",
        "tol_tnw",
        "debt_to_ebitda",
        "net_debt_to_ebitda",
        "ebitda_margin",
        "tnw_floor",
        "minimum_net_worth",
    }
)

#: A healthy, mid-size borrower's normalised statement lines (₹ crore) —
#: exactly the `{code: Decimal}` shape `Chart.normalise` produces.
_FIXTURE_A: dict[str, Decimal] = {
    "revenue": Decimal("1000"),
    "finance_cost": Decimal("40"),
    "ebit": Decimal("210"),
    "ebitda": Decimal("250"),
    "current_assets": Decimal("400"),
    "current_liabilities": Decimal("200"),
    "inventory": Decimal("120"),
    "cash_and_bank": Decimal("80"),
    "total_debt": Decimal("400"),
    "total_liabilities": Decimal("500"),
    "tangible_net_worth": Decimal("200"),
    "total_assets": Decimal("760"),
    "cash_flow_debt_service": Decimal("180"),
}

_EXPECTED_A: dict[str, Decimal] = {
    "leverage_ratio": Decimal("2"),
    "dscr": Decimal("4.5"),
    "interest_coverage_ratio": Decimal("5.25"),
    "fixed_charge_coverage_ratio": Decimal("6.25"),
    "current_ratio": Decimal("2"),
    "quick_ratio": Decimal("1.4"),
    "tol_tnw": Decimal("2.5"),
    "debt_to_ebitda": Decimal("1.6"),
    "net_debt_to_ebitda": Decimal("1.28"),
    "ebitda_margin": Decimal("25"),
    "tnw_floor": Decimal("200"),
    "minimum_net_worth": Decimal("260"),
}

#: A smaller, more leveraged borrower — a second, independent hand-worked
#: case per definition.
_FIXTURE_B: dict[str, Decimal] = {
    "revenue": Decimal("500"),
    "finance_cost": Decimal("50"),
    "ebit": Decimal("150"),
    "ebitda": Decimal("200"),
    "current_assets": Decimal("300"),
    "current_liabilities": Decimal("250"),
    "inventory": Decimal("50"),
    "cash_and_bank": Decimal("40"),
    "total_debt": Decimal("600"),
    "total_liabilities": Decimal("825"),
    "tangible_net_worth": Decimal("150"),
    "total_assets": Decimal("1000"),
    "cash_flow_debt_service": Decimal("100"),
}

_EXPECTED_B: dict[str, Decimal] = {
    "leverage_ratio": Decimal("4"),
    "dscr": Decimal("2"),
    "interest_coverage_ratio": Decimal("3"),
    "fixed_charge_coverage_ratio": Decimal("4"),
    "current_ratio": Decimal("1.2"),
    "quick_ratio": Decimal("1"),
    "tol_tnw": Decimal("5.5"),
    "debt_to_ebitda": Decimal("3"),
    "net_debt_to_ebitda": Decimal("2.8"),
    "ebitda_margin": Decimal("40"),
    "tnw_floor": Decimal("150"),
    "minimum_net_worth": Decimal("175"),
}

_HAND_WORKED_CASES = [
    pytest.param(_FIXTURE_A, code, value, id=f"A-{code}") for code, value in _EXPECTED_A.items()
] + [pytest.param(_FIXTURE_B, code, value, id=f"B-{code}") for code, value in _EXPECTED_B.items()]


def test_twelve_definitions_present() -> None:
    # `ENTRIES` here is `definitions.py`'s own tuple, which `T-028` extends
    # with ten more; `LIBRARY` (`library.py`) merges every part of the
    # library, `conditions.py` included. Both are asserted to a minimum,
    # not an exact count, so this T-027 test keeps passing as the library
    # grows — `test_ratios_2.py`'s `test_twenty_four_definitions_total`
    # is what pins the library's full, final size.
    assert len(ENTRIES) >= 12
    assert _TWELVE_CODES.issubset(LIBRARY)
    for code in _TWELVE_CODES:
        assert isinstance(LIBRARY[code], RatioDefinition)
        assert LIBRARY[code].code == code


@pytest.mark.parametrize(("lines", "code", "expected"), _HAND_WORKED_CASES)
def test_each_ratio_hand_worked_exact(
    lines: dict[str, Decimal], code: str, expected: Decimal
) -> None:
    result = compute_ratio(LIBRARY[code], lines, None)

    assert result.computable is True
    assert result.value == expected
    assert result.reason is None
    assert result.band_breached is False


def test_missing_line_named() -> None:
    lines = {"total_debt": Decimal("400")}  # tangible_net_worth absent

    result = compute_ratio(LIBRARY["leverage_ratio"], lines, None)

    assert result.computable is False
    assert result.value is None
    assert result.reason is NotComputableReason.MISSING_LINE
    assert "tangible_net_worth" in result.reason_context["names"]
    assert result.inputs_used == {"total_debt": Decimal("400")}


def test_zero_denominator_not_computable() -> None:
    lines = {"current_assets": Decimal("400"), "current_liabilities": Decimal("0")}

    result = compute_ratio(LIBRARY["current_ratio"], lines, None)

    assert result.computable is False
    assert result.value is None
    assert result.reason is NotComputableReason.ZERO_DENOMINATOR
    assert result.reason_context == {"denominator": "current_liabilities"}


def test_sign_meaningless_denominator_not_computable() -> None:
    lines = {"total_debt": Decimal("100"), "tangible_net_worth": Decimal("-50")}

    result = compute_ratio(LIBRARY["leverage_ratio"], lines, None)

    assert result.computable is False
    assert result.value is None
    assert result.reason is NotComputableReason.SIGN_MEANINGLESS_DENOMINATOR
    assert result.reason_context == {"denominator": "tangible_net_worth", "value": "-50"}


def test_band_breach_still_computed_and_flagged() -> None:
    lines = {"total_debt": Decimal("1000"), "tangible_net_worth": Decimal("100")}

    result = compute_ratio(LIBRARY["leverage_ratio"], lines, None)

    assert result.computable is True
    assert result.value == Decimal("10")
    assert result.band_breached is True


def test_unknown_definition_raises() -> None:
    bogus = RatioDefinition(
        code="not_in_the_library",
        name="Not a real ratio",
        formula_text="revenue",
        required_lines=("revenue",),
        unit="x",
        plausible_min=None,
        plausible_max=None,
        direction_hint=None,
    )

    with pytest.raises(UnknownDefinition) as excinfo:
        compute_ratio(bogus, {"revenue": Decimal("1")}, None)

    assert excinfo.value.definition_code == "not_in_the_library"


def test_inputs_used_lists_every_line_read() -> None:
    result = compute_ratio(LIBRARY["quick_ratio"], _FIXTURE_A, None)

    assert result.inputs_used == {
        "current_assets": Decimal("400"),
        "inventory": Decimal("120"),
        "current_liabilities": Decimal("200"),
    }

    partial = compute_ratio(LIBRARY["current_ratio"], {"current_assets": Decimal("400")}, None)
    assert partial.inputs_used == {"current_assets": Decimal("400")}


def _assert_no_float_in_source(module: ModuleType) -> None:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            raise AssertionError(f"{module.__name__} contains a float literal: {node.value!r}")
        if isinstance(node, ast.Name) and node.id == "float":
            raise AssertionError(f"{module.__name__} references the float builtin.")
        if isinstance(node, ast.Attribute) and node.attr == "float":
            raise AssertionError(f"{module.__name__} references a `.float` attribute.")


def test_no_float_in_computation_path() -> None:
    for module in (definitions, library, compute):
        _assert_no_float_in_source(module)


def test_ratio_result_invariants_enforced() -> None:
    with pytest.raises(ValueError):
        RatioResult(
            code="x",
            value=None,
            computable=True,
            reason=None,
            inputs_used={},
            band_breached=False,
        )
    with pytest.raises(ValueError):
        RatioResult(
            code="x",
            value=Decimal("1"),
            computable=False,
            reason=NotComputableReason.MISSING_LINE,
            inputs_used={},
            band_breached=False,
        )


def test_ratio_result_rejects_free_text_reason() -> None:
    with pytest.raises(TypeError):
        RatioResult(
            code="x",
            value=None,
            computable=False,
            reason="tangible_net_worth is not positive (-50)",  # type: ignore[arg-type]
            inputs_used={},
            band_breached=False,
        )
