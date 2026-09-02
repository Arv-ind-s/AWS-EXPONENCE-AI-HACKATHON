"""CSV and JSON signal source adapter.

The adapter accepts only a configured, canonical-field mapping.  Mapping
validation happens in the constructor, before the path, stream, or bytes are
read.  Valid rows become immutable :class:`SignalEvent` values; row-level
validation failures remain as canonical mappings so the existing ingestion
framework can quarantine them and continue.  File and parser failures are
source failures and include the configured source reference.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from decimal import Decimal
from os import PathLike
from pathlib import Path
from typing import BinaryIO, Final, TextIO
from uuid import UUID

from covenant_radar.ingestion.signals.sources import (
    SignalSourceConfigurationError,
    SignalSourceError,
    source_identity,
    validate_mapping,
    validate_source_row,
)

_MAX_FILE_BYTES: Final[int] = 16 * 1024 * 1024
_MAX_ROWS: Final[int] = 10_000
_FORMATS: Final[frozenset[str]] = frozenset({"csv", "json"})


class FileSignalSource:
    """Read a mapped CSV or JSON signal source without performing writes."""

    def __init__(
        self,
        content: bytes | bytearray | str | PathLike[str] | BinaryIO | TextIO | None = None,
        mapping: Mapping[str, object] | None = None,
        *,
        path: str | PathLike[str] | None = None,
        source_reference: str = "file",
        source_id: UUID | str | None = None,
        file_format: str | None = None,
        format: str | None = None,
        encoding: str = "utf-8-sig",
    ) -> None:
        """Create a source from bytes, text, a path, or an open stream.

        ``mapping`` maps canonical signal field names to source columns.  A
        JSON object column may be mapped as ``payload``; alternatively,
        payload fields may be mapped with a nested ``{"payload": {..}}``
        mapping.  ``path`` is an explicit alternative to the positional
        ``content`` argument and cannot be supplied together with it.
        """

        # Validate the mapping before storing or inspecting the source.  This
        # ordering is part of the safety contract for an unconfigured source.
        self.mapping = validate_mapping(mapping)
        self.source_reference, self.source_id = source_identity(source_reference, source_id)
        self._content = _resolve_input(content, path)
        self._encoding = _validate_encoding(encoding)
        self._format = _resolve_format(file_format, format)

    @property
    def name(self) -> str:
        """Compatibility alias used by generic source registries."""

        return self.source_reference

    @property
    def reference(self) -> str:
        """Short alias for the operator-facing source reference."""

        return self.source_reference

    @property
    def source_type(self) -> str:
        """The selected file format (``csv`` or ``json``)."""

        return self._format or "auto"

    def iter_events(self) -> Iterator[object]:
        """Yield mapped events in source order."""

        try:
            content, suffix = self._read_content()
            file_format = self._format or _infer_format(content, suffix)
            if file_format == "csv":
                yield from self._iter_csv(content)
            elif file_format == "json":
                yield from self._iter_json(content)
            else:  # pragma: no cover - constructor validation makes this unreachable
                raise SignalSourceConfigurationError(
                    f"Unsupported signal file format {file_format!r}.", field="file_format"
                )
        except SignalSourceError:
            raise
        except SignalSourceConfigurationError:
            raise
        except (OSError, UnicodeError, csv.Error, ValueError, TypeError) as error:
            raise self._source_error(str(error) or type(error).__name__) from error

    def __iter__(self) -> Iterator[object]:
        """Allow a source to be passed directly to the ingestion framework."""

        return self.iter_events()

    events = iter_events
    read = iter_events

    def _read_content(self) -> tuple[bytes, str | None]:
        source = self._content
        suffix: str | None = None
        if isinstance(source, bytes | bytearray):
            content = bytes(source)
        elif isinstance(source, Path):
            suffix = source.suffix.lower().lstrip(".") or None
            try:
                if not source.is_file():
                    raise OSError(f"source path {str(source)!r} is not a regular file")
                if source.stat().st_size > _MAX_FILE_BYTES:
                    raise OSError(f"source exceeds {_MAX_FILE_BYTES} bytes")
                content = source.read_bytes()
            except OSError:
                raise
        elif isinstance(source, str):
            content = source.encode(self._encoding)
        elif isinstance(source, io.TextIOBase):
            content = source.read().encode(self._encoding)
        elif isinstance(source, io.BufferedIOBase | io.RawIOBase | io.BytesIO):
            content = source.read()
        else:  # pragma: no cover - _resolve_input constrains this
            raise TypeError("unsupported file signal source input")
        if not isinstance(content, bytes):
            raise TypeError("file signal source streams must return bytes or text")
        if len(content) > _MAX_FILE_BYTES:
            raise OSError(f"source exceeds {_MAX_FILE_BYTES} bytes")
        return content, suffix

    def _iter_csv(self, content: bytes) -> Iterator[object]:
        try:
            text = content.decode(self._encoding)
        except UnicodeDecodeError as error:
            raise self._source_error(f"CSV is not valid {self._encoding} text") from error

        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        fieldnames = reader.fieldnames
        if not fieldnames:
            raise self._source_error("CSV has no header row")
        if any(not isinstance(field, str) or not field.strip() for field in fieldnames):
            raise self._source_error("CSV header contains a blank column name")
        if len(set(fieldnames)) != len(fieldnames):
            raise self._source_error("CSV header contains duplicate column names")
        _check_columns(fieldnames, self.mapping, self._source_error)

        for row_number, raw_row in enumerate(reader, start=2):
            if row_number - 1 > _MAX_ROWS:
                raise self._source_error(f"CSV contains more than {_MAX_ROWS} data rows")
            if None in raw_row:
                # A row with too many cells is malformed data, not a source
                # schema failure.  The unknown key causes the framework to
                # quarantine this row while later rows continue.
                yield {"__source_extra_fields__": raw_row[None], "source_id": self.source_id}
                continue
            yield validate_source_row(
                raw_row,
                self.mapping,
                source_id=self.source_id,
            )

    def _iter_json(self, content: bytes) -> Iterator[object]:
        try:
            payload = json.loads(
                content.decode(self._encoding),
                parse_float=Decimal,
                parse_int=int,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self._source_error("JSON could not be decoded") from error
        if not isinstance(payload, list):
            raise self._source_error("JSON signal source must be an array of row objects")
        if not payload:
            return
        if len(payload) > _MAX_ROWS:
            raise self._source_error(f"JSON contains more than {_MAX_ROWS} data rows")

        first_row = payload[0]
        if not isinstance(first_row, Mapping):
            raise self._source_error("JSON row 1 must be an object")
        _check_columns(first_row.keys(), self.mapping, self._source_error)
        for raw_row in payload:
            yield validate_source_row(raw_row, self.mapping, source_id=self.source_id)

    def _source_error(self, reason: str) -> SignalSourceError:
        return SignalSourceError(f"Signal source {self.source_reference!r}: {reason}.")


def _resolve_input(
    content: bytes | bytearray | str | PathLike[str] | BinaryIO | TextIO | None,
    path: str | PathLike[str] | None,
) -> bytes | str | Path | BinaryIO | TextIO:
    if content is not None and path is not None:
        raise SignalSourceConfigurationError(
            "Provide either content or path, not both.", field="source"
        )
    if path is not None:
        return Path(path)
    if content is None:
        raise SignalSourceConfigurationError(
            "A file signal source requires content or path.", field="source"
        )
    if isinstance(content, PathLike):
        return Path(content)
    if isinstance(content, bytes | bytearray):
        return bytes(content)
    if isinstance(content, str):
        if "\n" not in content and "\r" not in content:
            try:
                candidate = Path(content)
                if candidate.is_file():
                    return candidate
            except OSError:
                pass
        return content
    if isinstance(content, io.TextIOBase):
        return content
    if isinstance(content, io.BufferedIOBase | io.RawIOBase):
        return content
    raise SignalSourceConfigurationError(
        "File signal source content must be bytes, text, a path, or an open stream.",
        field="source",
    )


def _resolve_format(file_format: str | None, format: str | None) -> str | None:
    if file_format is not None and format is not None and file_format != format:
        raise SignalSourceConfigurationError(
            "file_format and format must match when both are supplied.", field="file_format"
        )
    selected = file_format if file_format is not None else format
    if selected is None:
        return None
    if not isinstance(selected, str) or selected.strip().lower() not in _FORMATS:
        raise SignalSourceConfigurationError(
            "file_format must be 'csv' or 'json'.", field="file_format"
        )
    return selected.strip().lower()


def _validate_encoding(encoding: str) -> str:
    if not isinstance(encoding, str) or not encoding.strip():
        raise SignalSourceConfigurationError(
            "encoding must be a non-empty string.", field="encoding"
        )
    try:
        "".encode(encoding)
    except LookupError as error:
        raise SignalSourceConfigurationError(
            "encoding is not recognised.", field="encoding"
        ) from error
    return encoding


def _infer_format(content: bytes, suffix: str | None) -> str:
    if suffix in _FORMATS:
        return suffix
    stripped = content.lstrip()
    return "json" if stripped.startswith(b"[") else "csv"


def _check_columns(
    available: Iterable[object],
    mapping: Mapping[str, object],
    error_factory: Callable[[str], SignalSourceError],
) -> None:
    available_columns = set(available)
    required_columns = _mapped_columns(mapping)
    missing = sorted(required_columns - available_columns)
    if missing:
        raise error_factory(f"source is missing mapped column(s): {', '.join(missing)}")


def _mapped_columns(mapping: Mapping[str, object]) -> set[str]:
    columns: set[str] = set()
    for field_name, source_column in mapping.items():
        if field_name == "payload" and isinstance(source_column, Mapping):
            columns.update(str(value) for value in source_column.values())
        elif isinstance(source_column, str):
            columns.add(source_column)
    return columns


__all__ = ["FileSignalSource"]
