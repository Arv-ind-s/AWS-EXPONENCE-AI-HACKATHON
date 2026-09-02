"""Statement import: mapping, readers, row normalisation and validation
(`T-025`, `plan.md §5.3`).

`services/statements.py` is the only caller that persists anything; every
module in this package is DB-free.
"""

from __future__ import annotations

from covenant_radar.ingestion.statements.mapping import (
    ImportMappingSpec,
    MappingError,
    TotalsRowSentinel,
    parse_mapping_spec,
)
from covenant_radar.ingestion.statements.normalise import (
    ResolvedRow,
    RowShapeError,
    apply_sign,
    extract_lines,
    resolve_row,
)
from covenant_radar.ingestion.statements.readers import (
    Cell,
    ReaderError,
    RowBatch,
    read_csv,
    read_json,
    read_rows,
    read_xlsx,
)
from covenant_radar.ingestion.statements.validate import (
    ColumnMismatchError,
    PreparedBatch,
    PreparedStatementRow,
    QuarantinedStatementRow,
    TotalsDiscrepancy,
    TotalsRow,
    check_columns,
    prepare,
    reconcile_totals,
)

__all__ = [
    "Cell",
    "ColumnMismatchError",
    "ImportMappingSpec",
    "MappingError",
    "PreparedBatch",
    "PreparedStatementRow",
    "QuarantinedStatementRow",
    "ReaderError",
    "ResolvedRow",
    "RowShapeError",
    "TotalsDiscrepancy",
    "TotalsRow",
    "TotalsRowSentinel",
    "apply_sign",
    "check_columns",
    "extract_lines",
    "parse_mapping_spec",
    "prepare",
    "read_csv",
    "read_json",
    "read_rows",
    "read_xlsx",
    "reconcile_totals",
    "resolve_row",
]
