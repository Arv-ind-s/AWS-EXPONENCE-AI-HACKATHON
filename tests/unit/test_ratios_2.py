"""Unit tests for the `T-028` ratio library part 2 — conduct, working
capital and covenant conditions (`plan.md §6`'s `C-30`)."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from types import ModuleType

import pytest

from covenant_radar.domain.ratios import compute, conditions, definitions, library
from covenant_radar.domain.ratios.compute import FacilityFacts, RatioResult, compute_ratio
from covenant_radar.domain.ratios.definitions import RatioDefinition
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.ratios.reasons import NotComputableReason

pytestmark = pytest.mark.unit

_TWELVE_CODES = frozenset(
    {
        "utilisation",
        "drawing_power_headroom",
        "receivable_days",
        "inventory_days",
        "payable_days",
        "cash_conversion_cycle",
        "working_capital_gap",
        "promoter_shareholding_floor",
        "dividend_restriction",
        "asset_cover_ratio",
        "minimum_liquidity",
        "maximum_capex",
    }
)

#: A healthy, mid-size borrower's normalised statement lines (₹ crore) plus
#: the facility facts `T-028`'s definitions read — every denominator below
#: is chosen so its division terminates exactly in base ten, the same
#: discipline `test_ratios_1.py`'s fixtures follow.
_LINES_C: dict[str, Decimal] = {
    "revenue": Decimal("1000"),
    "cost_of_goods_sold": Decimal("500"),
    "receivables": Decimal("100"),
    "inventory": Decimal("100"),
    "payables": Decimal("50"),
    "current_assets": Decimal("400"),
    "other_current_liabilities": Decimal("90"),
    "total_assets": Decimal("800"),
    "total_debt": Decimal("200"),
    "cash_and_bank": Decimal("90"),
    "capex": Decimal("55"),
    "dividend_paid": Decimal("0"),
    "promoter_shareholding": Decimal("60"),
}
_FACILITY_C = FacilityFacts(
    sanctioned_limit=Decimal("500"),
    outstanding=Decimal("350"),
    drawing_power=Decimal("400"),
    promoter_shareholding_floor_pct=Decimal("51"),
)

#: A smaller, more stretched borrower — independently chosen numbers, not a
#: scaled copy of fixture C. Drawn beyond its sanctioned limit (utilisation
#: > 100%) and short of both conditions, on purpose: `test_ratios_1.py`
#: covers the "comfortable" side, this fixture covers the "in breach" side.
_LINES_D: dict[str, Decimal] = {
    "revenue": Decimal("800"),
    "cost_of_goods_sold": Decimal("400"),
    "receivables": Decimal("200"),
    "inventory": Decimal("100"),
    "payables": Decimal("80"),
    "current_assets": Decimal("350"),
    "other_current_liabilities": Decimal("60"),
    "total_assets": Decimal("900"),
    "total_debt": Decimal("300"),
    "cash_and_bank": Decimal("45"),
    "capex": Decimal("25"),
    "dividend_paid": Decimal("12"),
    "promoter_shareholding": Decimal("45"),
}
_FACILITY_D = FacilityFacts(
    sanctioned_limit=Decimal("250"),
    outstanding=Decimal("300"),
    drawing_power=Decimal("280"),
    promoter_shareholding_floor_pct=Decimal("51"),
)

_EXPECTED_C: dict[str, Decimal] = {
    "utilisation": Decimal("70"),
    "drawing_power_headroom": Decimal("50"),
    "receivable_days": Decimal("36.5"),
    "inventory_days": Decimal("73"),
    "payable_days": Decimal("36.5"),
    "cash_conversion_cycle": Decimal("73"),
    "working_capital_gap": Decimal("260"),
    "asset_cover_ratio": Decimal("4"),
    "minimum_liquidity": Decimal("90"),
    "maximum_capex": Decimal("55"),
    "dividend_restriction": Decimal("0"),
    "promoter_shareholding_floor": Decimal("60"),
}

_EXPECTED_D: dict[str, Decimal] = {
    "utilisation": Decimal("120"),
    "drawing_power_headroom": Decimal("-20"),
    "receivable_days": Decimal("91.25"),
    "inventory_days": Decimal("91.25"),
    "payable_days": Decimal("73"),
    "cash_conversion_cycle": Decimal("109.5"),
    "working_capital_gap": Decimal("210"),
    "asset_cover_ratio": Decimal("3"),
    "minimum_liquidity": Decimal("45"),
    "maximum_capex": Decimal("25"),
    "dividend_restriction": Decimal("12"),
    "promoter_shareholding_floor": Decimal("45"),
}

#: Both condition-type definitions' intrinsic outcome, per fixture —
#: fixture C is compliant on both, fixture D is in breach of both.
_EXPECTED_OUTCOME_C: dict[str, bool] = {
    "dividend_restriction": True,
    "promoter_shareholding_floor": True,
}
_EXPECTED_OUTCOME_D: dict[str, bool] = {
    "dividend_restriction": False,
    "promoter_shareholding_floor": False,
}

_HAND_WORKED_CASES = [
    pytest.param(_LINES_C, _FACILITY_C, code, value, id=f"C-{code}")
    for code, value in _EXPECTED_C.items()
] + [
    pytest.param(_LINES_D, _FACILITY_D, code, value, id=f"D-{code}")
    for code, value in _EXPECTED_D.items()
]


def test_twenty_four_definitions_total() -> None:
    assert len(LIBRARY) == 24
    assert _TWELVE_CODES.issubset(LIBRARY)
    for code in _TWELVE_CODES:
        assert isinstance(LIBRARY[code], RatioDefinition)
        assert LIBRARY[code].code == code


@pytest.mark.parametrize(("lines", "facility", "code", "expected"), _HAND_WORKED_CASES)
def test_each_hand_worked_exact(
    lines: dict[str, Decimal], facility: FacilityFacts, code: str, expected: Decimal
) -> None:
    result = compute_ratio(LIBRARY[code], lines, facility)

    assert result.computable is True
    assert result.value == expected
    assert result.reason is None


def test_condition_returns_value_and_outcome() -> None:
    for lines, facility, expected_value, expected_outcome in (
        (_LINES_C, _FACILITY_C, _EXPECTED_C, _EXPECTED_OUTCOME_C),
        (_LINES_D, _FACILITY_D, _EXPECTED_D, _EXPECTED_OUTCOME_D),
    ):
        for code in ("dividend_restriction", "promoter_shareholding_floor"):
            result = compute_ratio(LIBRARY[code], lines, facility)
            assert result.computable is True
            assert result.value == expected_value[code]
            assert result.outcome is expected_outcome[code]
            assert LIBRARY[code].kind == "condition"


def test_plain_ratio_outcome_is_none() -> None:
    for code in _TWELVE_CODES - {"dividend_restriction", "promoter_shareholding_floor"}:
        result = compute_ratio(LIBRARY[code], _LINES_C, _FACILITY_C)
        assert result.outcome is None
        assert LIBRARY[code].kind == "ratio"


def test_facility_facts_required_named_when_absent() -> None:
    utilisation_result = compute_ratio(LIBRARY["utilisation"], _LINES_C, None)
    assert utilisation_result.computable is False
    assert utilisation_result.value is None
    assert utilisation_result.reason is NotComputableReason.FACILITY_FACTS_ABSENT
    assert "sanctioned_limit" in utilisation_result.reason_context["names"]
    assert "outstanding" in utilisation_result.reason_context["names"]

    headroom_result = compute_ratio(
        LIBRARY["drawing_power_headroom"],
        _LINES_C,
        FacilityFacts(outstanding=Decimal("10")),
    )
    assert headroom_result.computable is False
    assert headroom_result.reason is NotComputableReason.FACILITY_FACTS_ABSENT
    assert "drawing_power" in headroom_result.reason_context["names"]

    floor_result = compute_ratio(LIBRARY["promoter_shareholding_floor"], _LINES_C, None)
    assert floor_result.computable is False
    assert floor_result.reason is NotComputableReason.FACILITY_FACTS_ABSENT
    assert "promoter_shareholding_floor_pct" in floor_result.reason_context["names"]


def test_zero_revenue_days_ratio_not_computable() -> None:
    lines = {**_LINES_C, "revenue": Decimal("0")}

    receivable_days_result = compute_ratio(LIBRARY["receivable_days"], lines, None)
    assert receivable_days_result.computable is False
    assert receivable_days_result.value is None
    assert receivable_days_result.reason is NotComputableReason.ZERO_DENOMINATOR
    assert receivable_days_result.reason_context == {"denominator": "revenue"}

    cash_conversion_cycle_result = compute_ratio(LIBRARY["cash_conversion_cycle"], lines, None)
    assert cash_conversion_cycle_result.computable is False
    assert cash_conversion_cycle_result.reason is NotComputableReason.ZERO_DENOMINATOR
    assert cash_conversion_cycle_result.reason_context == {"denominator": "revenue"}


def test_zero_cost_of_goods_sold_not_computable() -> None:
    lines = {**_LINES_C, "cost_of_goods_sold": Decimal("0")}

    inventory_days_result = compute_ratio(LIBRARY["inventory_days"], lines, None)
    assert inventory_days_result.computable is False
    assert inventory_days_result.reason is NotComputableReason.ZERO_DENOMINATOR
    assert inventory_days_result.reason_context == {"denominator": "cost_of_goods_sold"}

    cash_conversion_cycle_result = compute_ratio(LIBRARY["cash_conversion_cycle"], lines, None)
    assert cash_conversion_cycle_result.computable is False
    assert cash_conversion_cycle_result.reason is NotComputableReason.ZERO_DENOMINATOR
    assert cash_conversion_cycle_result.reason_context == {"denominator": "cost_of_goods_sold"}


def test_utilisation_above_100_returned_and_flagged() -> None:
    result = compute_ratio(LIBRARY["utilisation"], _LINES_D, _FACILITY_D)

    assert result.computable is True
    assert result.value == Decimal("120")
    assert result.band_breached is True


def test_drawing_power_headroom_negative_returned_uncapped() -> None:
    result = compute_ratio(LIBRARY["drawing_power_headroom"], _LINES_D, _FACILITY_D)

    assert result.computable is True
    assert result.value == Decimal("-20")


def test_missing_statement_line_named_for_condition() -> None:
    result = compute_ratio(LIBRARY["dividend_restriction"], {}, None)

    assert result.computable is False
    assert result.value is None
    assert result.reason is NotComputableReason.MISSING_LINE
    assert "dividend_paid" in result.reason_context["names"]


def test_inputs_used_lists_every_input_read() -> None:
    utilisation_result = compute_ratio(LIBRARY["utilisation"], _LINES_C, _FACILITY_C)
    assert utilisation_result.inputs_used == {
        "facility.sanctioned_limit": Decimal("500"),
        "facility.outstanding": Decimal("350"),
    }

    floor_result = compute_ratio(LIBRARY["promoter_shareholding_floor"], _LINES_C, _FACILITY_C)
    assert floor_result.inputs_used == {
        "promoter_shareholding": Decimal("60"),
        "facility.promoter_shareholding_floor_pct": Decimal("51"),
    }


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
    for module in (definitions, conditions, library, compute):
        _assert_no_float_in_source(module)


def test_ratio_result_outcome_invariant_enforced() -> None:
    with pytest.raises(ValueError):
        RatioResult(
            code="x",
            value=None,
            computable=False,
            reason=NotComputableReason.MISSING_LINE,
            inputs_used={},
            band_breached=False,
            outcome=True,
        )


def test_ratio_definition_kind_invariant_enforced() -> None:
    with pytest.raises(ValueError):
        RatioDefinition(
            code="x",
            name="X",
            formula_text="x",
            required_lines=("x",),
            unit="x",
            plausible_min=None,
            plausible_max=None,
            direction_hint=None,
            kind="not_a_real_kind",
        )
