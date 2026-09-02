"""Bridge one raw extract row into the shape `Chart.normalise` (`T-024`)
expects (`T-025`).

`resolve_row` reads a row's period identity (borrower key, FY label, period
type and dates, audited flag) out of its mapping-declared columns, and
`extract_lines` reads its statement-line values the same way. Neither
function touches the database or the chart's own identity checks — this
module only gets a row into `{chart_code: Decimal|int|str}` shape; calling
`Chart.normalise` on the result is `validate.py`'s job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.covenants.exceptions import normalise_period
from covenant_radar.ingestion.statements.mapping import ImportMappingSpec
from covenant_radar.ingestion.statements.readers import Cell

_PERIOD_TYPES: Final[frozenset[str]] = frozenset({"quarterly", "half_yearly", "annual"})
_TRUE_VALUES: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
_FALSE_VALUES: Final[frozenset[str]] = frozenset({"false", "0", "no"})


class RowShapeError(ValidationError):
    """One row's period identity or line values do not match its mapping."""


@dataclass(frozen=True, slots=True)
class ResolvedRow:
    """One data row's period identity plus its raw, sign-adjusted line
    values, ready for `Chart.normalise`."""

    borrower_key: str
    fy_label: str
    period_type: str
    period_start: date
    period_end: date
    is_audited: bool
    lines: Mapping[str, Cell]


def resolve_row(row: Mapping[str, Cell], spec: ImportMappingSpec) -> ResolvedRow:
    """Parse one data row's period identity and line values."""
    borrower_key = _required_text(row, spec.borrower_key_column)
    fy_label = _fy_label(row, spec.fy_label_column)
    period_type = _period_type(row, spec.period_type_column)
    period_start = _date_value(row, spec.period_start_column)
    period_end = _date_value(row, spec.period_end_column)
    if period_end <= period_start:
        raise RowShapeError(
            f"{spec.period_end_column} ({period_end}) must be after "
            f"{spec.period_start_column} ({period_start}).",
            field=spec.period_end_column,
        )
    is_audited = _is_audited(row, spec)
    lines = extract_lines(row, spec)
    return ResolvedRow(
        borrower_key=borrower_key,
        fy_label=fy_label,
        period_type=period_type,
        period_start=period_start,
        period_end=period_end,
        is_audited=is_audited,
        lines=lines,
    )


def extract_lines(row: Mapping[str, Cell], spec: ImportMappingSpec) -> dict[str, Cell]:
    """Read every mapped statement-line cell, applying `spec.sign`.

    A cell that is absent or blank (`None`) is omitted entirely — an
    absent line stays absent, never zero, the same contract
    `Chart.normalise` itself upholds for a line missing from its input.
    """
    lines: dict[str, Cell] = {}
    for raw_column, chart_code in spec.columns.items():
        value = row.get(raw_column)
        if value is None:
            continue
        lines[chart_code] = apply_sign(value, spec.sign, field=raw_column)
    return lines


def apply_sign(value: Cell, sign: str, *, field: str) -> Cell:
    """Return `value` unchanged for `"as_reported"`, or negated for
    `"negate"`. Negation parses `value` to `Decimal` first — flipping the
    sign of an arbitrary numeric *string* by text manipulation would be
    unsound (`"-500"` must not become `"--500"`, `"1,234"` is not safe to
    touch at all), so this is the one place in the ingestion layer that
    pre-parses an amount before handing it to `Chart.normalise`."""
    if sign == "as_reported":
        return value
    amount = _parse_amount(value, field=field)
    return -amount


def _parse_amount(value: Cell, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise RowShapeError(f"{field} must be numeric, not a boolean.", field=field)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise RowShapeError(
                f"{field} is not a valid number: {value!r}.", field=field
            ) from error
    raise RowShapeError(f"{field} must be numeric, got {type(value).__name__}.", field=field)


def _required_text(row: Mapping[str, Cell], column: str) -> str:
    value = row.get(column)
    if not isinstance(value, str) or not value.strip():
        raise RowShapeError(f"{column} is required and must be non-empty.", field=column)
    return value.strip()


def _fy_label(row: Mapping[str, Cell], column: str) -> str:
    raw = _required_text(row, column)
    try:
        return normalise_period(raw)
    except (TypeError, ValueError) as error:
        raise RowShapeError(
            f"{column} is not a valid FYyyQn label: {raw!r}.", field=column
        ) from error


def _period_type(row: Mapping[str, Cell], column: str) -> str:
    raw = _required_text(row, column).lower()
    if raw not in _PERIOD_TYPES:
        raise RowShapeError(
            f"{column} must be one of {sorted(_PERIOD_TYPES)}, got {raw!r}.", field=column
        )
    return raw


def _date_value(row: Mapping[str, Cell], column: str) -> date:
    raw = _required_text(row, column)
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise RowShapeError(f"{column} is not an ISO date: {raw!r}.", field=column) from error


def _is_audited(row: Mapping[str, Cell], spec: ImportMappingSpec) -> bool:
    if spec.is_audited_column is None:
        return False
    value = row.get(spec.is_audited_column)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in _TRUE_VALUES:
            return True
        if normalised in _FALSE_VALUES:
            return False
    raise RowShapeError(
        f"{spec.is_audited_column} must be a boolean-like value, got {value!r}.",
        field=spec.is_audited_column,
    )


__all__ = ["ResolvedRow", "RowShapeError", "apply_sign", "extract_lines", "resolve_row"]
