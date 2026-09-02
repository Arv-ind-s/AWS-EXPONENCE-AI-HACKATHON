"""Adapter for the canonical ``POST /api/v1/ingest/signals`` payload."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any
from uuid import UUID

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.signals import SignalEvent
from covenant_radar.ingestion.signals.sources import (
    SignalSourceConfigurationError,
    SignalSourceError,
    source_identity,
    validate_mapping,
    validate_source_row,
)

_MAX_ROWS = 10_000
_BATCH_FIELDS = frozenset({"events", "source_id", "idempotency_key"})
_IDENTITY_MAPPING = {
    "borrower_id": "borrower_id",
    "facility_id": "facility_id",
    "event_date": "event_date",
    "family": "family",
    "event_type": "event_type",
    "magnitude": "magnitude",
    "unit": "unit",
    "payload": "payload",
    "source_id": "source_id",
    "is_late": "is_late",
    "content_hash": "content_hash",
}


class ApiSignalSource:
    """Expose an API ingest payload through the ``SignalSource`` seam.

    API payloads already use canonical names, so a mapping is optional for
    this adapter.  Accepting a mapping as well keeps the adapter useful for a
    trusted API gateway that renames fields before forwarding them and makes
    its output identical to the file source's output.
    """

    def __init__(
        self,
        payload: object,
        mapping: Mapping[str, object] | None = None,
        *,
        source_reference: str = "api",
        source_id: UUID | str | None = None,
    ) -> None:
        self._custom_mapping = mapping is not None
        self.mapping = validate_mapping(_IDENTITY_MAPPING if mapping is None else mapping)
        self.source_reference, resolved_id = source_identity(source_reference, source_id)
        payload_source_id = _payload_source_id(payload)
        if source_id is None and payload_source_id is not None:
            _, resolved_id = source_identity(self.source_reference, payload_source_id)
        self.source_id = resolved_id
        self._payload = payload

    @property
    def name(self) -> str:
        """Compatibility alias used by generic source registries."""

        return self.source_reference

    @property
    def reference(self) -> str:
        """Short alias for the operator-facing source reference."""

        return self.source_reference

    def iter_events(self) -> Iterator[object]:
        """Yield canonical events in the API payload's order."""

        try:
            rows = _payload_rows(self._payload)
            for row_number, row in enumerate(rows, start=1):
                if row_number > _MAX_ROWS:
                    raise self._source_error(f"payload contains more than {_MAX_ROWS} events")
                yield self._validate_row(row)
        except SignalSourceError:
            raise
        except SignalSourceConfigurationError:
            raise
        except Exception as error:
            raise self._source_error(str(error) or type(error).__name__) from error

    def __iter__(self) -> Iterator[object]:
        """Allow a source to be passed directly to the ingestion framework."""

        return self.iter_events()

    events = iter_events
    read = iter_events

    def _source_error(self, reason: str) -> SignalSourceError:
        return SignalSourceError(f"Signal source {self.source_reference!r}: {reason}.")

    def _validate_row(self, row: object) -> object:
        if self._custom_mapping or not isinstance(row, Mapping):
            return validate_source_row(row, self.mapping, source_id=self.source_id)
        canonical = dict(row)
        canonical["source_id"] = self.source_id
        try:
            return SignalEvent.from_mapping(canonical)
        except (TypeError, ValueError, ValidationError):
            return canonical


def _payload_rows(payload: object) -> Iterable[object]:
    value = payload
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="python")

    if isinstance(value, Mapping):
        unknown = sorted(str(key) for key in set(value) - _BATCH_FIELDS)
        if "events" in value:
            if unknown:
                raise SignalSourceError(
                    f"API ingest payload contains unknown field(s): {', '.join(unknown)}."
                )
            rows = value["events"]
        else:
            # A single canonical row is useful for programmatic callers and
            # remains source-compatible with the batch shape.
            rows = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        rows = value
    else:
        events = getattr(value, "events", None)
        if events is None:
            raise SignalSourceError("API ingest payload must contain an events sequence.")
        rows = events

    if isinstance(rows, str | bytes | bytearray) or not isinstance(rows, Iterable):
        raise SignalSourceError("API ingest payload events must be an iterable.")
    return rows


def _payload_source_id(payload: object) -> UUID | str | None:
    model_dump = getattr(payload, "model_dump", None)
    value: Any = model_dump(mode="python") if callable(model_dump) else payload
    if isinstance(value, Mapping):
        candidate = value.get("source_id")
    else:
        candidate = getattr(value, "source_id", None)
    if candidate is None or isinstance(candidate, UUID | str):
        return candidate
    raise SignalSourceConfigurationError("payload.source_id must be a UUID.", field="source_id")


__all__ = ["ApiSignalSource"]
