"""CSV, XLSX and JSON readers behind one interface (`T-025`).

Every reader returns the same shape — a `RowBatch` of raw string/number/
`None` cell values keyed by raw column name, exactly as the file states
them, before `normalise.py` or `mapping.py` interpret a single cell. None
of the three readers know about the chart, a mapping, or the database.

**A numeric XLSX cell or JSON number is not, by itself, safe input for
`Chart.normalise`**, which flatly refuses a Python `float` (see `chart.py`'s
own docstring: a float cannot represent money exactly). `openpyxl` hands
back numeric cells as `float`, and `json.loads` hands back a JSON number
with a fractional part the same way, so both readers convert every numeric
cell to `Decimal` via `Decimal(str(value))` before it ever leaves this
module — the standard, well-understood bridge from "whatever the file
format's native numeric type gives us" into the exact-decimal input the
chart demands. This is a deliberate bridge at the ingestion boundary, not a
bypass of the chart's own no-float rule; a CSV cell is always text and
needs no such conversion, since `Chart.normalise` already parses a numeric
string directly.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

import openpyxl

from covenant_radar.core.errors import ValidationError

_MAX_ROWS: Final[int] = 10_000
_SOURCE_TYPES: Final[frozenset[str]] = frozenset({"csv", "xlsx", "json"})

Cell = str | int | Decimal | bool | None


class ReaderError(ValidationError):
    """A source file could not be parsed into rows."""


@dataclass(frozen=True, slots=True)
class RowBatch:
    """Every row of one source file, in file order, plus its header."""

    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Cell], ...]


def read_rows(source_type: str, content: bytes) -> RowBatch:
    """Parse `content` into a `RowBatch` according to `source_type`."""
    if source_type not in _SOURCE_TYPES:
        raise ReaderError(
            f"Unsupported statement source type {source_type!r}; expected one of "
            f"{sorted(_SOURCE_TYPES)}.",
            field="source_type",
        )
    if not isinstance(content, bytes | bytearray):
        raise ReaderError("Statement file content must be bytes.", field="content")
    if source_type == "csv":
        return read_csv(bytes(content))
    if source_type == "xlsx":
        return read_xlsx(bytes(content))
    return read_json(bytes(content))


def read_csv(content: bytes) -> RowBatch:
    """Parse a CSV extract. Every cell is text or `None` (an empty cell)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ReaderError(f"CSV content is not valid UTF-8: {error}.", field="content") from error
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ReaderError("CSV content has no header row.", field="content")
    if any(name is None for name in reader.fieldnames):
        raise ReaderError("CSV header row has a blank column name.", field="content")
    columns = tuple(reader.fieldnames)
    rows: list[Mapping[str, Cell]] = []
    try:
        for line_number, raw_row in enumerate(reader, start=2):
            if len(rows) >= _MAX_ROWS:
                raise ReaderError(f"CSV content exceeds {_MAX_ROWS} data rows.", field="content")
            if None in raw_row:
                raise ReaderError(
                    f"CSV row at line {line_number} has more fields than the header.",
                    field="content",
                )
            rows.append({key: (value if value != "" else None) for key, value in raw_row.items()})
    except csv.Error as error:
        raise ReaderError(f"CSV content could not be parsed: {error}.", field="content") from error
    return RowBatch(columns=columns, rows=tuple(rows))


def read_xlsx(content: bytes) -> RowBatch:
    """Parse the first worksheet of an XLSX extract; row 1 is the header."""
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as error:  # openpyxl raises a variety of concrete types
        raise ReaderError(f"XLSX content could not be opened: {error}.", field="content") from error
    try:
        worksheet = workbook.worksheets[0]
        row_iterator = worksheet.iter_rows(values_only=True)
        try:
            header = next(row_iterator)
        except StopIteration as error:
            raise ReaderError("XLSX worksheet has no header row.", field="content") from error
        if any(cell is None for cell in header):
            raise ReaderError("XLSX header row has a blank column name.", field="content")
        columns = tuple(str(cell) for cell in header)
        rows: list[Mapping[str, Cell]] = []
        for row_number, raw_row in enumerate(row_iterator, start=2):
            if all(cell is None for cell in raw_row):
                continue
            if len(rows) >= _MAX_ROWS:
                raise ReaderError(f"XLSX content exceeds {_MAX_ROWS} data rows.", field="content")
            if len(raw_row) != len(columns):
                raise ReaderError(
                    f"XLSX row {row_number} has {len(raw_row)} cells; expected {len(columns)}.",
                    field="content",
                )
            rows.append(
                {
                    column: _coerce_cell(value)
                    for column, value in zip(columns, raw_row, strict=True)
                }
            )
    finally:
        workbook.close()
    return RowBatch(columns=columns, rows=tuple(rows))


def read_json(content: bytes) -> RowBatch:
    """Parse a JSON array of flat row objects. Column order follows the
    first row's own key order; every row must carry exactly those keys."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReaderError(f"JSON content is not valid UTF-8: {error}.", field="content") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReaderError(f"JSON content is not valid JSON: {error}.", field="content") from error
    if not isinstance(payload, list) or not payload:
        raise ReaderError("JSON content must be a non-empty array of row objects.", field="content")
    if len(payload) > _MAX_ROWS:
        raise ReaderError(f"JSON content exceeds {_MAX_ROWS} data rows.", field="content")
    if not isinstance(payload[0], dict):
        raise ReaderError("JSON content row 1 must be an object.", field="content")
    columns = tuple(payload[0].keys())
    column_set = set(columns)
    rows: list[Mapping[str, Cell]] = []
    for row_number, raw_row in enumerate(payload, start=1):
        if not isinstance(raw_row, dict):
            raise ReaderError(f"JSON content row {row_number} must be an object.", field="content")
        if set(raw_row) != column_set:
            raise ReaderError(
                f"JSON content row {row_number} does not have the same fields as row 1.",
                field="content",
            )
        rows.append({column: _coerce_cell(raw_row[column]) for column in columns})
    return RowBatch(columns=columns, rows=tuple(rows))


def _coerce_cell(value: object) -> Cell:
    """Bridge one raw XLSX/JSON cell into a `Chart`-safe type — see the
    module docstring for why a `float` is converted here rather than
    passed through. A date-formatted XLSX cell comes back from `openpyxl`
    as a native `datetime`/`date`; it is rendered to an ISO string here so
    `normalise.py` always parses period dates the same way regardless of
    source format."""
    if value is None or isinstance(value, str | bool | int | Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ReaderError(f"Unsupported cell value type: {type(value).__name__}.", field="content")


__all__ = ["Cell", "ReaderError", "RowBatch", "read_csv", "read_json", "read_rows", "read_xlsx"]
