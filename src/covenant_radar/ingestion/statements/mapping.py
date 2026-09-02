"""The versioned import mapping (`plan.md §5.3`'s `import_mapping.spec`,
`T-025`).

A mapping declares, once and explicitly, everything a source's raw extract
needs translated into the chart of accounts (`domain/statements/chart.py`,
`T-024`): which raw column carries the borrower key, which raw columns
carry the period identity, which raw column maps to which chart line code,
the single unit and currency the whole extract is stated in, and whether
every value in the file needs its sign flipped before it reaches the
chart's own per-line sign-convention check.

That last point is deliberate and mirrors `chart.py`'s own discipline: a
sign correction is never *inferred* from a value at import time — it is
*declared*, once, in the mapping, by whoever configured the source. The
difference is only which layer the declaration lives at: `chart.py` refuses
to guess a sign for one line; this module refuses to guess a sign for a
whole source.

This module performs no I/O and no database access; it only parses and
validates a mapping's `spec` JSON into a typed `ImportMappingSpec`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.statements.chart import Chart, default_chart

#: Mirrors `chart.py`'s own (private) unit vocabulary. Not imported directly
#: because `chart.py` does not export it — `Chart.normalise` is the single
#: place a unit is authoritative, so this is only an early, friendlier
#: rejection at mapping-parse time; the chart itself still has the final
#: word at normalisation time.
_UNITS: Final[frozenset[str]] = frozenset({"actual", "thousand", "lakh", "crore"})
_SIGNS: Final[frozenset[str]] = frozenset({"as_reported", "negate"})
_CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{3}$")
_MAX_COLUMN_NAME_LENGTH: Final[int] = 200

_REQUIRED_SPEC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "borrower_key_column",
        "fy_label_column",
        "period_type_column",
        "period_start_column",
        "period_end_column",
        "is_audited_column",
        "unit",
        "currency",
        "sign",
        "columns",
        "totals_row",
    }
)


class MappingError(ValidationError):
    """An `import_mapping.spec` document is malformed or self-inconsistent."""


@dataclass(frozen=True, slots=True)
class TotalsRowSentinel:
    """Identifies the one row in an extract that is a portfolio total, not
    a borrower's own statement (`plan.md §5.3`: "a totals row ... used for
    reconciliation, not loaded as data")."""

    column: str
    value: str


@dataclass(frozen=True, slots=True)
class ImportMappingSpec:
    """One parsed, validated `import_mapping.spec` document."""

    borrower_key_column: str
    fy_label_column: str
    period_type_column: str
    period_start_column: str
    period_end_column: str
    is_audited_column: str | None
    unit: str
    currency: str
    sign: str
    columns: Mapping[str, str] = field(default_factory=dict)
    totals_row: TotalsRowSentinel | None = None

    @property
    def required_raw_columns(self) -> frozenset[str]:
        """Every raw column name this mapping reads from the source file."""
        names = {
            self.borrower_key_column,
            self.fy_label_column,
            self.period_type_column,
            self.period_start_column,
            self.period_end_column,
        }
        if self.is_audited_column is not None:
            names.add(self.is_audited_column)
        if self.totals_row is not None:
            names.add(self.totals_row.column)
        names.update(self.columns.keys())
        return frozenset(names)


def parse_mapping_spec(raw: object, *, chart: Chart | None = None) -> ImportMappingSpec:
    """Validate and parse one `import_mapping.spec` JSON document.

    `chart` defaults to the process-cached `default_chart()`; every value
    in `columns` is checked against it here so a mistyped line code fails
    at mapping-creation time, not partway through a real import.
    """
    if not isinstance(raw, Mapping):
        raise MappingError("import_mapping.spec must be a JSON object.", field="spec")
    keys = set(raw)
    if keys != _REQUIRED_SPEC_KEYS:
        missing = ", ".join(sorted(_REQUIRED_SPEC_KEYS - keys))
        extra = ", ".join(sorted(keys - _REQUIRED_SPEC_KEYS))
        parts = [
            part
            for part in (
                f"missing {missing}" if missing else "",
                f"unknown {extra}" if extra else "",
            )
            if part
        ]
        raise MappingError(
            f"import_mapping.spec has invalid fields ({'; '.join(parts)}).", field="spec"
        )

    borrower_key_column = _text(raw["borrower_key_column"], "spec.borrower_key_column")
    fy_label_column = _text(raw["fy_label_column"], "spec.fy_label_column")
    period_type_column = _text(raw["period_type_column"], "spec.period_type_column")
    period_start_column = _text(raw["period_start_column"], "spec.period_start_column")
    period_end_column = _text(raw["period_end_column"], "spec.period_end_column")
    is_audited_column = _optional_text(raw["is_audited_column"], "spec.is_audited_column")

    unit = _text(raw["unit"], "spec.unit")
    if unit not in _UNITS:
        raise MappingError(
            f"spec.unit must be one of {sorted(_UNITS)}, got {unit!r}.", field="spec.unit"
        )

    currency = _text(raw["currency"], "spec.currency")
    if not _CURRENCY_PATTERN.fullmatch(currency):
        raise MappingError(
            f"spec.currency must be a 3-letter uppercase ISO code, got {currency!r}.",
            field="spec.currency",
        )

    sign = _text(raw["sign"], "spec.sign")
    if sign not in _SIGNS:
        raise MappingError(
            f"spec.sign must be one of {sorted(_SIGNS)}, got {sign!r}.", field="spec.sign"
        )

    columns_raw = raw["columns"]
    if not isinstance(columns_raw, Mapping) or not columns_raw:
        raise MappingError("spec.columns must be a non-empty object.", field="spec.columns")
    resolved_chart = chart if chart is not None else default_chart()
    columns: dict[str, str] = {}
    seen_codes: set[str] = set()
    for raw_column, line_code in columns_raw.items():
        column_name = _text(raw_column, "spec.columns key")
        code = _text(line_code, f"spec.columns[{column_name!r}]")
        if code not in resolved_chart:
            raise MappingError(
                f"spec.columns[{column_name!r}] references unknown chart line {code!r}.",
                field="spec.columns",
            )
        if code in seen_codes:
            raise MappingError(
                f"spec.columns maps more than one raw column to chart line {code!r}.",
                field="spec.columns",
            )
        seen_codes.add(code)
        columns[column_name] = code

    totals_row = _parse_totals_row(raw["totals_row"])

    identity_columns = {
        borrower_key_column,
        fy_label_column,
        period_type_column,
        period_start_column,
        period_end_column,
    }
    if is_audited_column is not None:
        identity_columns.add(is_audited_column)
    overlap = identity_columns & set(columns)
    if overlap:
        raise MappingError(
            f"spec identity column(s) {sorted(overlap)} cannot also appear in spec.columns.",
            field="spec.columns",
        )

    return ImportMappingSpec(
        borrower_key_column=borrower_key_column,
        fy_label_column=fy_label_column,
        period_type_column=period_type_column,
        period_start_column=period_start_column,
        period_end_column=period_end_column,
        is_audited_column=is_audited_column,
        unit=unit,
        currency=currency,
        sign=sign,
        columns=columns,
        totals_row=totals_row,
    )


def _parse_totals_row(raw: object) -> TotalsRowSentinel | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"column", "value"}:
        raise MappingError(
            "spec.totals_row must be null or an object with exactly 'column' and 'value'.",
            field="spec.totals_row",
        )
    return TotalsRowSentinel(
        column=_text(raw["column"], "spec.totals_row.column"),
        value=_text(raw["value"], "spec.totals_row.value"),
    )


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MappingError(f"{field_name} must be a non-empty string.", field=field_name)
    if len(value) > _MAX_COLUMN_NAME_LENGTH:
        raise MappingError(
            f"{field_name} exceeds {_MAX_COLUMN_NAME_LENGTH} characters.", field=field_name
        )
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


__all__ = [
    "ImportMappingSpec",
    "MappingError",
    "TotalsRowSentinel",
    "parse_mapping_spec",
]
