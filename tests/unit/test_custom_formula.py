"""Unit tests for `T-029` — the custom formula parser, validator and
restricted evaluator (`plan.md §6`'s `C-31`, `spec §R-07.d`).

Every hostile construct the module's docstring names is driven through
`parse_custom_formula` here and must be refused with `FormulaRefused`,
naming the construct, before any evaluation is attempted. The property
test in `tests/property/test_custom_formula_safety.py` covers the same
ground with generated, rather than hand-picked, hostile input.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.ratios.custom import (
    MAX_FORMULA_DEPTH,
    MAX_FORMULA_LENGTH,
    FormulaRefused,
    parse_custom_formula,
)
from covenant_radar.domain.ratios.reasons import NotComputableReason

pytestmark = pytest.mark.unit

_ALLOWED_LINES = frozenset({"total_debt", "tangible_net_worth", "ebitda", "finance_cost"})


def test_valid_formula_evaluates() -> None:
    formula = parse_custom_formula("total_debt / tangible_net_worth", _ALLOWED_LINES)

    assert formula.required_lines == frozenset({"total_debt", "tangible_net_worth"})

    result = formula.evaluate({"total_debt": Decimal("120"), "tangible_net_worth": Decimal("40")})

    assert result.computable is True
    assert result.value == Decimal("3")
    assert result.reason is None
    assert result.inputs_used == {
        "total_debt": Decimal("120"),
        "tangible_net_worth": Decimal("40"),
    }


def test_valid_formula_with_every_operator_and_parentheses() -> None:
    # `(total_debt - finance_cost) * 2 / ebitda + 1` exercises `+ - * /`,
    # unary sign and explicit grouping together, and must evaluate exactly.
    formula = parse_custom_formula("(total_debt - finance_cost) * 2 / ebitda + 1", _ALLOWED_LINES)

    result = formula.evaluate(
        {
            "total_debt": Decimal("100"),
            "finance_cost": Decimal("20"),
            "ebitda": Decimal("40"),
        }
    )

    assert result.computable is True
    assert result.value == Decimal("5")


def test_unary_minus_evaluates() -> None:
    formula = parse_custom_formula("-total_debt + ebitda", _ALLOWED_LINES)

    result = formula.evaluate({"total_debt": Decimal("30"), "ebitda": Decimal("50")})

    assert result.computable is True
    assert result.value == Decimal("20")


def test_missing_line_is_not_computable_not_raised() -> None:
    formula = parse_custom_formula("total_debt / ebitda", _ALLOWED_LINES)

    result = formula.evaluate({"total_debt": Decimal("100")})

    assert result.computable is False
    assert result.value is None
    assert result.reason is NotComputableReason.MISSING_LINE
    assert result.reason_context == {"names": "ebitda"}


def test_call_refused_naming_construct() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula('__import__("os").system("echo hi")', _ALLOWED_LINES)

    assert excinfo.value.construct == "a function call"
    assert "function call" in str(excinfo.value)


def test_attribute_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("total_debt.real", _ALLOWED_LINES)

    assert excinfo.value.construct == "attribute access"


def test_subscript_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("total_debt[0]", _ALLOWED_LINES)

    assert excinfo.value.construct == "a subscript"


def test_import_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("import os", _ALLOWED_LINES)

    # `import` is a statement, not an expression, so the standard parser
    # itself refuses it in `mode="eval"` — still refused, before any
    # evaluation, which is the contract this test proves.
    assert excinfo.value.construct == "syntax_error"


def test_comprehension_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("[total_debt for _ in range(1)]", _ALLOWED_LINES)

    assert "comprehension" in excinfo.value.construct


def test_lambda_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("lambda: total_debt", _ALLOWED_LINES)

    assert excinfo.value.construct == "a lambda"


def test_walrus_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("(x := total_debt)", _ALLOWED_LINES)

    assert excinfo.value.construct == "a walrus assignment"


def test_string_literal_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("'total_debt'", _ALLOWED_LINES)

    assert excinfo.value.construct == "non_numeric_constant"


def test_boolean_literal_refused() -> None:
    # `bool` is an `int` subtype in Python, so it needs its own check —
    # `True` must not silently evaluate as `1`.
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("total_debt + True", _ALLOWED_LINES)

    assert excinfo.value.construct == "non_numeric_constant"


def test_unknown_line_refused_with_suggestions() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("total_debtt / ebitda", _ALLOWED_LINES)

    assert excinfo.value.construct == "unknown_line"
    assert "total_debtt" in str(excinfo.value)
    assert "total_debt" in str(excinfo.value)  # the near match is named


def test_unknown_line_with_no_near_match_still_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("zzz_completely_unrelated / ebitda", _ALLOWED_LINES)

    assert excinfo.value.construct == "unknown_line"
    assert "no similarly named line exists" in str(excinfo.value)


def test_division_by_zero_not_computable() -> None:
    formula = parse_custom_formula("total_debt / ebitda", _ALLOWED_LINES)

    result = formula.evaluate({"total_debt": Decimal("100"), "ebitda": Decimal("0")})

    assert result.computable is False
    assert result.value is None
    assert result.reason is NotComputableReason.FORMULA_NOT_COMPUTABLE
    assert result.reason_context == {"detail": "division by zero"}


def test_depth_and_length_limits() -> None:
    too_long = "total_debt" + " + 1" * MAX_FORMULA_LENGTH
    assert len(too_long) > MAX_FORMULA_LENGTH
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula(too_long, _ALLOWED_LINES)
    assert excinfo.value.construct == "formula_too_long"

    too_deep = "+".join(["total_debt"] * (MAX_FORMULA_DEPTH + 10))
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula(too_deep, _ALLOWED_LINES)
    assert excinfo.value.construct == "formula_too_deep"

    within_limits = "+".join(["total_debt"] * 3)
    formula = parse_custom_formula(within_limits, _ALLOWED_LINES)
    assert formula.required_lines == frozenset({"total_debt"})


def test_constant_only_formula_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("2 + 2 * 3", _ALLOWED_LINES)

    assert excinfo.value.construct == "no_line_referenced"


def test_empty_formula_refused() -> None:
    with pytest.raises(FormulaRefused):
        parse_custom_formula("", _ALLOWED_LINES)


def test_syntax_error_refused() -> None:
    with pytest.raises(FormulaRefused) as excinfo:
        parse_custom_formula("total_debt +", _ALLOWED_LINES)

    assert excinfo.value.construct == "syntax_error"


def test_formula_refused_is_a_validation_error() -> None:
    from covenant_radar.core.errors import ValidationError

    with pytest.raises(ValidationError):
        parse_custom_formula("total_debt()", _ALLOWED_LINES)
