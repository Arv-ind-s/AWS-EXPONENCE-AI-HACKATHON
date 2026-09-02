"""Unit tests for the `T-024` statement chart of accounts and normalisation model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_radar.domain.statements.chart import Chart, LineDiscrepancy, LineFlag

pytestmark = pytest.mark.unit

#: Every statement line the 24-definition ratio library (`T-027`, `T-028`)
#: reads: leverage, DSCR, interest coverage, fixed-charge coverage, current
#: ratio, quick ratio, TOL/TNW, debt/EBITDA, net debt/EBITDA, EBITDA margin,
#: TNW floor, minimum net worth, receivable/inventory/payable days, cash
#: conversion cycle, working-capital gap, promoter shareholding floor,
#: dividend restriction, asset-cover ratio, minimum liquidity and maximum
#: capex. Utilisation, drawing-power headroom and asset-cover ratio also
#: read facility and conduct facts, which are outside this chart's scope.
_RATIO_LIBRARY_REQUIRED_LINES = frozenset(
    {
        "revenue",
        "cost_of_goods_sold",
        "ebitda",
        "ebit",
        "finance_cost",
        "tax_expense",
        "current_assets",
        "cash_and_bank",
        "inventory",
        "receivables",
        "current_liabilities",
        "payables",
        "short_term_debt",
        "total_debt",
        "total_liabilities",
        "tangible_net_worth",
        "total_assets",
        "capex",
        "cash_flow_debt_service",
        "dividend_paid",
        "promoter_shareholding",
    }
)


def test_every_ratio_input_line_defined() -> None:
    chart = Chart.load()

    missing = _RATIO_LIBRARY_REQUIRED_LINES - chart.codes
    assert not missing, f"chart is missing lines the ratio library needs: {sorted(missing)}"


def test_normalise_to_crore() -> None:
    chart = Chart.load()

    from_rupees = chart.normalise({"revenue": "150000000"}, "actual")
    assert from_rupees.lines["revenue"] == Decimal("15")

    from_lakh = chart.normalise({"revenue": Decimal("1500")}, "lakh")
    assert from_lakh.lines["revenue"] == Decimal("15")

    from_thousand = chart.normalise({"revenue": Decimal("150000")}, "thousand")
    assert from_thousand.lines["revenue"] == Decimal("15")

    already_crore = chart.normalise({"revenue": Decimal("15")})
    assert already_crore.lines["revenue"] == Decimal("15")


def test_sign_convention_applied() -> None:
    chart = Chart.load()

    positive_line = chart.normalise({"revenue": Decimal("-10")})
    assert "revenue" not in positive_line.lines
    assert positive_line.flags == (
        LineFlag(
            code="revenue",
            reason="negative_value_on_forbidden_sign_line",
            value=Decimal("-10"),
        ),
    )

    signed_line = chart.normalise({"profit_after_tax": Decimal("-5")})
    assert signed_line.lines["profit_after_tax"] == Decimal("-5")
    assert signed_line.flags == ()


def test_identity_failure_marks_incomplete_and_names_it() -> None:
    chart = Chart.load()

    result = chart.normalise(
        {
            "total_assets": Decimal("100"),
            "total_liabilities": Decimal("60"),
            "tangible_net_worth": Decimal("30"),
        }
    )

    assert result.is_complete is False
    failing_names = {check.name for check in result.failing_identities}
    assert failing_names == {"balance_sheet_identity"}


def test_absent_line_is_absent_not_zero() -> None:
    chart = Chart.load()

    result = chart.normalise({"revenue": Decimal("10")})

    assert "capex" not in result.lines


def test_supplied_beats_derived_and_reports_discrepancy() -> None:
    chart = Chart.load()

    result = chart.normalise(
        {
            "short_term_debt": Decimal("10"),
            "long_term_debt": Decimal("40"),
            "total_debt": Decimal("55"),
        }
    )

    assert result.lines["total_debt"] == Decimal("55")
    assert result.discrepancies == (
        LineDiscrepancy(
            code="total_debt",
            supplied=Decimal("55"),
            derived=Decimal("50"),
            difference=Decimal("5"),
        ),
    )
