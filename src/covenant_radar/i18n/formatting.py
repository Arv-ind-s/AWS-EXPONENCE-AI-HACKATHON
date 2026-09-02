"""Indian presentation formatting for values already validated by the domain."""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from numbers import Integral
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_LAKH = Decimal("100000")
_CRORE = Decimal("10000000")
_DISPLAY_QUANTUM = Decimal("0.01")


def format_indian_number(value: Decimal | Integral | str) -> str:
    """Format a number with Indian digit grouping without changing precision."""
    number = _decimal(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    whole, separator, fraction = format(number, "f").partition(".")
    grouped = _group_indian(whole)
    return f"{sign}{grouped}{separator}{fraction}" if separator else f"{sign}{grouped}"


def format_indian_currency(
    value: Decimal | Integral | str,
    *,
    symbol: str = "₹",
    currency: str = "INR",
    compact: bool = True,
) -> str:
    """Format INR using lakh/crore units for large values.

    Compact output is deliberately display-only; the underlying Decimal is
    never rounded or converted to a binary float.
    """
    if currency not in {"INR", "₹"}:
        raise ValueError(f"Unsupported currency for Indian formatting: {currency!r}.")
    number = _decimal(value)
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if compact and magnitude >= _CRORE:
        return f"{sign}{symbol}{_compact(magnitude / _CRORE)} crore"
    if compact and magnitude >= _LAKH:
        return f"{sign}{symbol}{_compact(magnitude / _LAKH)} lakh"
    # Displayed to a fixed two decimal places regardless of the stored
    # column's scale, so a numeric(18,4) value never reads as more precise
    # than the compact lakh/crore branches above it.
    displayed = magnitude.quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{sign}{symbol}{format_indian_number(displayed)}"


def format_ist_date(value: date | datetime, *, timezone: tzinfo = IST) -> str:
    """Render an aware instant or date in the deployment's Indian date form."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Datetime values must be timezone-aware before IST formatting.")
        rendered = value.astimezone(timezone).date()
    elif isinstance(value, date):
        rendered = value
    else:
        raise TypeError("Expected a date or timezone-aware datetime.")
    return rendered.strftime("%d %b %Y")


def format_fy_quarter(value: date | datetime, *, timezone: tzinfo = IST) -> str:
    """Render an Indian financial quarter as ``Qn FYyy``.

    The Indian financial year starts in April: April--June is Q1 and the
    financial year label is the year in which it ends.
    """
    rendered = _as_local_date(value, timezone=timezone)
    if rendered.month >= 4:
        start_year = rendered.year
        quarter = ((rendered.month - 4) // 3) + 1
    else:
        start_year = rendered.year - 1
        quarter = ((rendered.month - 1) // 3) + 4
    return f"Q{quarter} FY{(start_year + 1) % 100:02d}"


def format_fy_label(value: date | datetime, *, timezone: tzinfo = IST) -> str:
    """Render the compact persisted-period form, for example ``FY27Q2``."""
    rendered = _as_local_date(value, timezone=timezone)
    if rendered.month >= 4:
        start_year = rendered.year
        quarter = ((rendered.month - 4) // 3) + 1
    else:
        start_year = rendered.year - 1
        quarter = ((rendered.month - 1) // 3) + 4
    return f"FY{(start_year + 1) % 100:02d}Q{quarter}"


def _decimal(value: Decimal | Integral | str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("Boolean values are not valid financial numbers.")
    if isinstance(value, float):
        raise TypeError("Floating-point values are not valid financial numbers.")
    try:
        number = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise TypeError("Value must be a finite Decimal-compatible number.") from error
    if not number.is_finite():
        raise ValueError("Financial numbers must be finite.")
    return number


def _group_indian(whole: str) -> str:
    if len(whole) <= 3:
        return whole
    tail = whole[-3:]
    head = whole[:-3]
    groups: list[str] = []
    while head:
        groups.append(head[-2:])
        head = head[:-2]
    return ",".join((*reversed(groups), tail))


def _compact(value: Decimal) -> str:
    rounded = value.quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)
    text = format(rounded, "f").rstrip("0").rstrip(".")
    return text or "0"


def _as_local_date(value: date | datetime, *, timezone: tzinfo) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Datetime values must be timezone-aware before quarter formatting.")
        return value.astimezone(timezone).date()
    if isinstance(value, date):
        return value
    raise TypeError("Expected a date or timezone-aware datetime.")


# Names convenient for Jinja filters and callers migrating from core money/date helpers.
format_currency = format_indian_currency
format_money = format_indian_currency
format_indian_amount = format_indian_currency
format_currency_inr = format_indian_currency
format_date = format_ist_date
format_date_ist = format_ist_date
format_quarter = format_fy_quarter
format_fiscal_quarter = format_fy_quarter
indian_number = format_indian_number
indian_currency = format_indian_currency
ist_date = format_ist_date
fy_quarter = format_fy_quarter


__all__ = [
    "IST",
    "format_currency",
    "format_currency_inr",
    "format_date",
    "format_date_ist",
    "format_fiscal_quarter",
    "format_fy_label",
    "format_fy_quarter",
    "format_indian_amount",
    "format_indian_currency",
    "format_indian_number",
    "format_ist_date",
    "format_money",
    "format_quarter",
    "fy_quarter",
    "indian_currency",
    "indian_number",
    "ist_date",
]
