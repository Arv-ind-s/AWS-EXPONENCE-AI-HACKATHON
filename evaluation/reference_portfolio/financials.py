"""Financial-period generation and independent ratio validation.

The generator produces primitive statement lines and asks the production
statement chart to derive totals.  It then runs every registered ratio
through the production ratio library.  This keeps the reference data from
quietly becoming a second implementation of the product's arithmetic.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from random import Random
from types import MappingProxyType
from typing import Final
from uuid import UUID

from covenant_radar.domain.ratios.compute import FacilityFacts, RatioResult, compute_ratio
from covenant_radar.domain.ratios.library import LIBRARY
from covenant_radar.domain.statements.chart import Chart, default_chart

MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")
DEFAULT_FIRST_FISCAL_YEAR: Final[int] = 2020
MIN_FISCAL_YEAR: Final[int] = 2000
MAX_FISCAL_YEAR: Final[int] = 2099


class FinancialGenerationError(ValueError):
    """Raised when generated statement data cannot satisfy its contract."""


@dataclass(frozen=True, slots=True)
class FinancialPeriodRecord:
    """One complete Indian financial year quarter for one borrower."""

    id: UUID
    borrower_id: UUID
    fy_label: str
    period_type: str
    period_start: date
    period_end: date
    is_complete: bool
    is_audited: bool
    lines: Mapping[str, Decimal]
    ratios: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if self.period_type != "quarterly":
            raise ValueError("Reference financial periods must be quarterly.")
        if self.period_start > self.period_end:
            raise ValueError("A financial period cannot end before it starts.")
        if not self.is_complete:
            raise FinancialGenerationError(
                f"Generated financial period {self.fy_label} is not complete."
            )
        if not self.lines or not self.ratios:
            raise FinancialGenerationError(
                f"Generated financial period {self.fy_label} has no statement data."
            )


def _amount(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _random_decimal(
    random_source: Random, lower: Decimal, upper: Decimal, *, places: int = 2
) -> Decimal:
    if lower > upper:
        raise ValueError("The lower random bound cannot exceed the upper bound.")
    scale = 10**places
    low_units = int(lower * scale)
    high_units = int(upper * scale)
    return Decimal(random_source.randint(low_units, high_units)) / Decimal(scale)


def _fiscal_quarter(first_fiscal_year: int, offset: int) -> tuple[str, date, date]:
    fiscal_year = first_fiscal_year + offset // 4
    quarter = offset % 4 + 1
    if quarter == 1:
        start_year, start_month, end_month = fiscal_year - 1, 4, 6
    elif quarter == 2:
        start_year, start_month, end_month = fiscal_year - 1, 7, 9
    elif quarter == 3:
        start_year, start_month, end_month = fiscal_year - 1, 10, 12
    else:
        start_year, start_month, end_month = fiscal_year, 1, 3
    end_year = start_year if end_month != 3 else start_year
    end_day = calendar.monthrange(end_year, end_month)[1]
    return (
        f"FY{fiscal_year % 100:02d}Q{quarter}",
        date(start_year, start_month, 1),
        date(end_year, end_month, end_day),
    )


def _period_lines(random_source: Random) -> dict[str, Decimal]:
    """Create a coherent raw statement whose identities add exactly."""
    revenue = _random_decimal(random_source, Decimal("120"), Decimal("950"))
    cogs = _amount(revenue * _random_decimal(random_source, Decimal("0.55"), Decimal("0.65")))
    operating_expenses = _amount(
        revenue * _random_decimal(random_source, Decimal("0.15"), Decimal("0.22"))
    )
    depreciation = _amount(
        revenue * _random_decimal(random_source, Decimal("0.02"), Decimal("0.05"))
    )
    finance_cost = _amount(
        revenue * _random_decimal(random_source, Decimal("0.02"), Decimal("0.07"))
    )
    ebit = revenue - cogs - operating_expenses - depreciation
    tax_expense = _amount(ebit * _random_decimal(random_source, Decimal("0.20"), Decimal("0.30")))
    profit_after_tax = (
        revenue - cogs - operating_expenses - depreciation - finance_cost - tax_expense
    )

    cash_and_bank = _amount(
        revenue * _random_decimal(random_source, Decimal("0.12"), Decimal("0.25"))
    )
    inventory_days = _random_decimal(random_source, Decimal("40"), Decimal("70"))
    receivable_days = _random_decimal(random_source, Decimal("45"), Decimal("75"))
    payable_days = _random_decimal(random_source, Decimal("40"), Decimal("70"))
    inventory = _amount(cogs * inventory_days / Decimal("365"))
    receivables = _amount(revenue * receivable_days / Decimal("365"))
    payables = _amount(cogs * payable_days / Decimal("365"))

    current_assets = _amount(
        revenue * _random_decimal(random_source, Decimal("0.85"), Decimal("0.95"))
    )
    other_current_assets = current_assets - cash_and_bank - inventory - receivables
    # These bounds make the current-ratio and quick-ratio bands meaningful
    # while retaining a positive other-current-assets line.
    if other_current_assets <= 0:
        other_current_assets = MONEY_QUANTUM
        current_assets = cash_and_bank + inventory + receivables + other_current_assets

    short_term_debt = _amount(
        revenue * _random_decimal(random_source, Decimal("0.14"), Decimal("0.18"))
    )
    other_current_liabilities = _amount(
        revenue * _random_decimal(random_source, Decimal("0.16"), Decimal("0.22"))
    )
    long_term_debt = _amount(
        revenue * _random_decimal(random_source, Decimal("0.26"), Decimal("0.38"))
    )
    total_debt = short_term_debt + long_term_debt
    total_assets = _amount(
        revenue * _random_decimal(random_source, Decimal("1.90"), Decimal("2.90"))
    )
    tangible_net_worth = _amount(
        revenue * _random_decimal(random_source, Decimal("1.15"), Decimal("1.55"))
    )
    total_liabilities = total_assets - tangible_net_worth
    if total_liabilities <= total_debt:
        total_liabilities = total_debt + _amount(revenue * Decimal("0.45"))
        total_assets = total_liabilities + tangible_net_worth

    ebitda = ebit + depreciation
    cash_flow_debt_service = _amount(
        ebitda * _random_decimal(random_source, Decimal("0.60"), Decimal("0.90"))
    )
    cash_flow_operations = _amount(ebitda - finance_cost)
    capex = _amount(revenue * _random_decimal(random_source, Decimal("0.04"), Decimal("0.12")))
    dividend_paid = _amount(revenue * _random_decimal(random_source, Decimal("0"), Decimal("0.04")))
    promoter_shareholding = _random_decimal(random_source, Decimal("45"), Decimal("85"), places=2)

    return {
        "revenue": revenue,
        "cost_of_goods_sold": cogs,
        "operating_expenses": operating_expenses,
        "depreciation": depreciation,
        "finance_cost": finance_cost,
        "tax_expense": tax_expense,
        "profit_after_tax": profit_after_tax,
        "cash_and_bank": cash_and_bank,
        "inventory": inventory,
        "receivables": receivables,
        "other_current_assets": other_current_assets,
        "payables": payables,
        "short_term_debt": short_term_debt,
        "other_current_liabilities": other_current_liabilities,
        "long_term_debt": long_term_debt,
        "total_liabilities": total_liabilities,
        "tangible_net_worth": tangible_net_worth,
        "total_assets": total_assets,
        "capex": capex,
        "cash_flow_operations": cash_flow_operations,
        "cash_flow_debt_service": cash_flow_debt_service,
        "dividend_paid": dividend_paid,
        "promoter_shareholding": promoter_shareholding,
    }


def _validate_ratios(
    chart: Chart,
    lines: Mapping[str, Decimal],
    facility: FacilityFacts,
) -> Mapping[str, Decimal]:
    normalised = chart.normalise(lines, unit="crore")
    if normalised.flags:
        details = ", ".join(flag.code for flag in normalised.flags)
        raise FinancialGenerationError(f"Generated statement contains rejected lines: {details}.")
    if normalised.discrepancies:
        details = ", ".join(item.code for item in normalised.discrepancies)
        raise FinancialGenerationError(
            f"Generated statement has inconsistent derived lines: {details}."
        )
    if not normalised.is_complete:
        failed = ", ".join(check.name for check in normalised.failing_identities)
        raise FinancialGenerationError(f"Generated statement fails identity checks: {failed}.")

    values: dict[str, Decimal] = {}
    for code, definition in LIBRARY.items():
        result: RatioResult = compute_ratio(definition, normalised.lines, facility)
        if not result.computable or result.value is None:
            raise FinancialGenerationError(
                f"Generated ratio {code!r} is not computable: {result.reason}."
            )
        if result.band_breached:
            raise FinancialGenerationError(
                f"Generated ratio {code!r} is outside its plausible band: {result.value}."
            )
        values[code] = result.value
    return MappingProxyType(values)


def generate_financial_periods(
    random_source: Random,
    *,
    borrower_id: UUID,
    period_ids: tuple[UUID, ...],
    first_fiscal_year: int,
    quarter_count: int,
    facility: FacilityFacts,
    chart: Chart | None = None,
) -> tuple[FinancialPeriodRecord, ...]:
    """Generate and validate a borrower's quarterly financial history."""
    if quarter_count < 1:
        raise ValueError("quarter_count must be positive.")
    if len(period_ids) != quarter_count:
        raise ValueError("period_ids must contain one id per requested quarter.")
    if not MIN_FISCAL_YEAR <= first_fiscal_year <= MAX_FISCAL_YEAR:
        raise ValueError("first_fiscal_year is outside the supported range.")

    statement_chart = chart or default_chart()
    records: list[FinancialPeriodRecord] = []
    for offset, period_id in enumerate(period_ids):
        fy_label, period_start, period_end = _fiscal_quarter(first_fiscal_year, offset)
        raw_lines = _period_lines(random_source)
        normalised = statement_chart.normalise(raw_lines, unit="crore")
        ratios = _validate_ratios(statement_chart, raw_lines, facility)
        records.append(
            FinancialPeriodRecord(
                id=period_id,
                borrower_id=borrower_id,
                fy_label=fy_label,
                period_type="quarterly",
                period_start=period_start,
                period_end=period_end,
                is_complete=normalised.is_complete,
                is_audited=offset == quarter_count - 1,
                lines=MappingProxyType(dict(normalised.lines)),
                ratios=ratios,
            )
        )
    return tuple(records)


__all__ = [
    "DEFAULT_FIRST_FISCAL_YEAR",
    "FinancialGenerationError",
    "FinancialPeriodRecord",
    "generate_financial_periods",
]
