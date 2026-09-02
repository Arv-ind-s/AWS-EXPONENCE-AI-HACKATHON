"""Column-shape validation, per-row quarantine, and totals-row
reconciliation — the DB-free half of statement import (`T-025`).

Mirrors `ingestion/signals/framework.py`'s separation of concerns: this
module reads and validates one already-parsed `RowBatch` (`readers.py`)
against one `ImportMappingSpec` (`mapping.py`) and the `Chart` (`T-024`),
and produces prepared/quarantined rows plus a reconciliation report. It has
no database dependency; `services/statements.py` is the only caller that
resolves a borrower key to a database id, persists anything, or commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.statements.chart import Chart, ChartError, NormalisationResult
from covenant_radar.ingestion.statements.mapping import ImportMappingSpec
from covenant_radar.ingestion.statements.normalise import (
    ResolvedRow,
    RowShapeError,
    apply_sign,
    extract_lines,
    resolve_row,
)
from covenant_radar.ingestion.statements.readers import Cell

_MAX_REASON_LENGTH = 500


class ColumnMismatchError(ValidationError):
    """A source file's header does not exactly match its mapping."""


@dataclass(frozen=True, slots=True)
class PreparedStatementRow:
    """One data row validated against its mapping and the chart."""

    row_number: int
    resolved: ResolvedRow
    normalisation: NormalisationResult
    raw: Mapping[str, Cell]


@dataclass(frozen=True, slots=True)
class QuarantinedStatementRow:
    """One row held out of the batch, and why."""

    row_number: int
    raw: Mapping[str, Cell] | None
    rule_failed: str
    message: str

    def __post_init__(self) -> None:
        if isinstance(self.row_number, bool) or self.row_number < 1:
            raise ValueError("Quarantined statement row_number must be a positive integer.")
        if not self.rule_failed.strip():
            raise ValueError("A quarantined statement row requires rule_failed.")
        if not self.message.strip():
            raise ValueError("A quarantined statement row requires a message.")
        object.__setattr__(self, "message", self.message[:_MAX_REASON_LENGTH])


@dataclass(frozen=True, slots=True)
class TotalsRow:
    """One row recognised as the portfolio total, never loaded as data."""

    row_number: int
    lines: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class TotalsDiscrepancy:
    """One chart line whose accepted-row sum disagrees with the totals row."""

    line_code: str
    expected: Decimal
    actual: Decimal
    difference: Decimal


@dataclass(frozen=True, slots=True)
class PreparedBatch:
    """The complete, DB-free outcome of validating one source file."""

    prepared: tuple[PreparedStatementRow, ...]
    quarantined: tuple[QuarantinedStatementRow, ...]
    totals: tuple[TotalsRow, ...]

    @property
    def received(self) -> int:
        return len(self.prepared) + len(self.quarantined) + len(self.totals)


def check_columns(available_columns: frozenset[str], spec: ImportMappingSpec) -> None:
    """Refuse a file whose header does not exactly match `spec`, before any
    row is processed or any batch/period/line row is written."""
    required = spec.required_raw_columns
    missing = required - available_columns
    extra = available_columns - required
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if extra:
            parts.append(f"unexpected {sorted(extra)}")
        raise ColumnMismatchError(
            f"Statement file columns do not match the mapping: {'; '.join(parts)}.",
            field="columns",
        )


def prepare(
    rows: tuple[Mapping[str, Cell], ...],
    spec: ImportMappingSpec,
    chart: Chart,
    *,
    tolerance: Decimal,
) -> PreparedBatch:
    """Validate every row of a file already checked by `check_columns`.

    Row numbers are 1-indexed over the file's data body (the header is
    never counted), and stay stable regardless of which rows turn out to
    be a totals row or a quarantined row — the same number a bank's own
    extract reviewer would count to.
    """
    prepared: list[PreparedStatementRow] = []
    quarantined: list[QuarantinedStatementRow] = []
    totals: list[TotalsRow] = []
    totals_seen = False

    for row_number, raw_row in enumerate(rows, start=1):
        if spec.totals_row is not None and _matches_totals(raw_row, spec):
            if totals_seen:
                quarantined.append(
                    QuarantinedStatementRow(
                        row_number=row_number,
                        raw=raw_row,
                        rule_failed="duplicate_totals_row",
                        message="More than one row matches the mapping's totals-row sentinel.",
                    )
                )
                continue
            totals_seen = True
            totals_row = _prepare_totals(row_number, raw_row, spec, chart, quarantined)
            if totals_row is not None:
                totals.append(totals_row)
            continue

        try:
            resolved = resolve_row(raw_row, spec)
        except RowShapeError as error:
            quarantined.append(
                QuarantinedStatementRow(
                    row_number=row_number,
                    raw=raw_row,
                    rule_failed="row_shape_invalid",
                    message=str(error),
                )
            )
            continue

        try:
            normalisation = chart.normalise(resolved.lines, spec.unit, tolerance=tolerance)
        except ChartError as error:
            quarantined.append(
                QuarantinedStatementRow(
                    row_number=row_number,
                    raw=raw_row,
                    rule_failed="line_normalisation_failed",
                    message=str(error),
                )
            )
            continue

        prepared.append(
            PreparedStatementRow(
                row_number=row_number, resolved=resolved, normalisation=normalisation, raw=raw_row
            )
        )

    return PreparedBatch(
        prepared=tuple(prepared), quarantined=tuple(quarantined), totals=tuple(totals)
    )


def reconcile_totals(
    prepared: tuple[PreparedStatementRow, ...],
    totals: tuple[TotalsRow, ...],
    *,
    tolerance: Decimal,
) -> tuple[TotalsDiscrepancy, ...]:
    """Compare the summed accepted-row lines against a totals row, when one
    was present. Empty when no totals row was recognised — nothing to
    reconcile is not a discrepancy."""
    if not totals:
        return ()
    totals_row = totals[0]
    discrepancies: list[TotalsDiscrepancy] = []
    for line_code, expected in totals_row.lines.items():
        actual = sum(
            (row.normalisation.lines.get(line_code, Decimal(0)) for row in prepared),
            start=Decimal(0),
        )
        if abs(actual - expected) > tolerance:
            discrepancies.append(
                TotalsDiscrepancy(
                    line_code=line_code,
                    expected=expected,
                    actual=actual,
                    difference=actual - expected,
                )
            )
    return tuple(discrepancies)


def _matches_totals(raw_row: Mapping[str, Cell], spec: ImportMappingSpec) -> bool:
    sentinel = spec.totals_row
    if sentinel is None:
        return False
    value = raw_row.get(sentinel.column)
    return isinstance(value, str) and value.strip().lower() == sentinel.value.strip().lower()


def _prepare_totals(
    row_number: int,
    raw_row: Mapping[str, Cell],
    spec: ImportMappingSpec,
    chart: Chart,
    quarantined: list[QuarantinedStatementRow],
) -> TotalsRow | None:
    try:
        lines = extract_lines(raw_row, spec)
        normalisation = chart.normalise(lines, spec.unit)
    except (RowShapeError, ChartError) as error:
        quarantined.append(
            QuarantinedStatementRow(
                row_number=row_number,
                raw=raw_row,
                rule_failed="totals_row_invalid",
                message=str(error),
            )
        )
        return None
    return TotalsRow(row_number=row_number, lines=normalisation.lines)


# `apply_sign` is re-exported for callers that only need `validate.py`'s
# surface without reaching into `normalise.py` directly.
__all__ = [
    "ColumnMismatchError",
    "PreparedBatch",
    "PreparedStatementRow",
    "QuarantinedStatementRow",
    "TotalsDiscrepancy",
    "TotalsRow",
    "apply_sign",
    "check_columns",
    "prepare",
    "reconcile_totals",
]
